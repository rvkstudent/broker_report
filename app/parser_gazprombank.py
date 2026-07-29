"""Parse Gazprombank (Ньютон Инвестиции) PDF broker reports and insert data into SQLite.

Формат PDF-отчёта брокера «Ньютон Инвестиции» (Газпромбанк):
  - Page 1: шапка (брокер, клиент, договор, период), секции 1-4
  - Pages 2+: секция 5 — сделки (биржевые, РЕПО, незавершённые, исполнение)
  - Last page: секция 8 — неторговые операции

Биржевые сделки (5.1) — таблица колонок:
  [Номер сделки, Дата сделки, Время сделки, Вид сделки,
   Цена одной ЦБ, Валюта цены, Количество ЦБ шт, НКД, Сумма сделки]

РЕПО (5.6) — та же структура, но одна сделка имеет 2 строки (Продажа + Покупка)
  с одинаковым номером сделки.

Неторговые операции (8) — таблица колонок:
  [Дата, Тип операции, Сумма, Валюта]
"""

import re
import os
from datetime import datetime
from app.db import get_connection, init_db

try:
    import pdfplumber
except ImportError:
    pdfplumber = None


def parse_float(s):
    if s is None:
        return 0.0
    if isinstance(s, (int, float)):
        return float(s)
    s = str(s).strip()
    if not s or s in ('', '-', '—', '–'):
        return 0.0
    s = s.replace('\xa0', '').replace('&nbsp;', '').replace(' ', '')
    s = s.replace(',', '.')
    s = s.lstrip('+')
    try:
        return float(s)
    except ValueError:
        return 0.0


def parse_int(s):
    if s is None:
        return 0
    if isinstance(s, (int, float)):
        return int(s)
    s = str(s).strip().replace('\xa0', '').replace('&nbsp;', '').replace(' ', '')
    try:
        return int(float(s))
    except ValueError:
        return 0


def _fmt_date(val):
    if val is None or (isinstance(val, float) and pdfplumber is not None):
        # pdfplumber tables return strings, but handle edge cases
        pass
    if isinstance(val, datetime):
        return val.strftime('%d.%m.%Y')
    if hasattr(val, 'strftime'):
        return val.strftime('%d.%m.%Y')
    s = str(val).strip()
    # DD.MM.YYYY
    m = re.match(r'(\d{2})\.(\d{2})\.(\d{4})', s)
    if m:
        return f'{m.group(1)}.{m.group(2)}.{m.group(3)}'
    # ISO YYYY-MM-DD
    m = re.match(r'(\d{4})-(\d{2})-(\d{2})', s)
    if m:
        return f'{m.group(3)}.{m.group(2)}.{m.group(1)}'
    return s[:10]


def _fmt_time(val):
    if val is None:
        return ''
    if isinstance(val, datetime):
        return val.strftime('%H:%M:%S')
    if hasattr(val, 'strftime'):
        return val.strftime('%H:%M:%S')
    s = str(val).strip()
    m = re.search(r'(\d{2}:\d{2}:\d{2})', s)
    if m:
        return m.group(1)
    m = re.search(r'(\d{2}:\d{2})', s)
    if m:
        return m.group(1) + ':00'
    return ''


def _parse_instrument_block(text_line):
    """Parse instrument header line like 'ЭмитентИнструментТикерВалюта' or
    'MS0003000000SU26241RMFS826241RMFSRUR'.

    Returns (sec_name, sec_code, isin, currency).
    """
    if not text_line:
        return '', '', '', ''

    s = text_line.strip()

    # Pattern 1: MS0003000000SU26241RMFS826241RMFSRUR
    #   → эмитент='MS0003000000', инструмент='SU26241RMFS8', код='26241RMFS', isin/валюта='RUR'
    m = re.match(r'^MS\d+RMFS(\d+)([A-Z]+)$', s)
    if m:
        return s, m.group(1), '', m.group(2)

    # Pattern 2: MS0003000000ОФЗ 2624326243RMFSRUR
    m = re.match(r'^(MS\d+)(ОФЗ\s+\S+)(\S+)([A-Z]+)$', s)
    if m:
        sec_name = m.group(2).strip()
        sec_code = m.group(3).strip()
        currency = m.group(4).strip()
        return sec_name, sec_code, '', currency

    # Pattern 3: MS0003000000ОФЗ 2624826248RMFSRUR
    m = re.match(r'^(MS\d+)(ОФЗ\s+\S+)(\d+RMFS)([A-Z]+)$', s)
    if m:
        return m.group(2).strip(), m.group(3).strip(), '', m.group(4).strip()

    # Pattern 4: MC0302000000Атомэнергопром 001Р-06 (USD)4B02-06-55319-E-001PRUR
    m = re.match(r'^(MC\d+)(.+?)(\([A-Z]+\))?(\S+)([A-Z]+)$', s)
    if m:
        sec_name = (m.group(2) + ' ' + (m.group(3) or '')).strip()
        sec_code = m.group(4).strip()
        currency = m.group(5).strip()
        return sec_name, sec_code, '', currency

    # Pattern 5: АО ВИМ ИнвестицииLQDT ETF3915RUR
    m = re.match(r'^(.+?)([A-Z][A-Z0-9\s]+?)(\d+)([A-Z]+)$', s)
    if m:
        issuer = m.group(1).strip()
        name_code = m.group(2).strip()
        regnum = m.group(3).strip()
        currency = m.group(4).strip()
        return name_code, regnum, '', currency

    # Pattern 6: ПАО "МТС"МТС-ао1-01-04715-ARUR
    m = re.match(r'^(.+?"[^"]*")(.+?)(\S+)([A-Z]+)$', s)
    if m:
        sec_name = (m.group(1).strip() + ' ' + m.group(2).strip()).strip()
        sec_code = m.group(3).strip()
        currency = m.group(4).strip()
        return sec_name, sec_code, '', currency

    # Fallback: try to find RUR/USD at end
    m = re.search(r'(RUR|USD)$', s)
    if m:
        currency = m.group(1)
        rest = s[:m.start()].strip()
        return rest, rest, '', currency

    return s, s, '', ''


# Map GP codes to tickers (using existing VTB mapping where applicable)
_GP_TICKER_MAP = {
    '26241RMFS': 'SU26241RMFS8',
    '26243RMFS': 'SU26243RMFS4',
    '26248RMFS': 'SU26248RMFS8',
    'LQDT ETF': 'LQDT',
    'МТС-ао': 'MTSS',
    'Атомэнергопром 001Р-06 (USD)': 'RU000A10C3M0',
    'Атомэнергопром 001Р-06': 'RU000A10C3M0',
}


def _lookup_ticker(sec_name, sec_code):
    """Try to map to a known ticker."""
    for key, ticker in _GP_TICKER_MAP.items():
        if key in sec_name or key in sec_code or sec_name in key or sec_code in key:
            return ticker
    return sec_code or sec_name


def _text_between(page_words, x0, x1, top, bottom):
    """Get text in a region between x0 and x1."""
    words = [w for w in page_words
             if w['x0'] >= x0 and w['x1'] <= x1
             and w['top'] >= top and w['top'] <= bottom]
    words.sort(key=lambda w: (w['top'], w['x0']))
    return ' '.join(w['text'] for w in words)


def _collect_text(pdf):
    """Collect full text from all pages."""
    text_parts = []
    for page in pdf.pages:
        text_parts.append(page.extract_text())
    return '\n'.join(text_parts)


def _find_section_boundaries(full_text):
    """Find page line boundaries for each section in the report.

    Returns dict: section_title -> (start_line, end_line)
    """
    lines = full_text.split('\n')
    section_starts = {}
    section_patterns = [
        r'^5\.1\s+Биржевые сделки',
        r'^5\.6\s+Сделки РЕПО',
        r'^5\.9\s+Незавершенные сделки',
        r'^5\.10\s+Исполнение обязательств',
        r'^8\.\d',
        r'^8\s+Неторговые операции',
    ]
    for i, line in enumerate(lines):
        line_s = line.strip()
        for pat in section_patterns:
            if re.match(pat, line_s):
                if pat not in section_starts:
                    section_starts[pat] = i

    # Build boundaries
    named_sections = [
        ('trades_5.1', r'^5\.1\s+Биржевые сделки'),
        ('repo_5.6', r'^5\.6\s+Сделки РЕПО'),
        ('unfinished_5.9', r'^5\.9\s+Незавершенные сделки'),
        ('execution_5.10', r'^5\.10\s+Исполнение обязательств'),
        ('non_trading', r'^8(\.|\s+Неторговые операции)'),
    ]

    boundaries = {}
    for name, pat in named_sections:
        if pat in section_starts:
            start_idx = section_starts[pat]
            # Find end: next section or EOF
            end_idx = len(lines)
            for other_pat, other_idx in sorted(section_starts.items(), key=lambda x: x[1]):
                if other_idx > start_idx:
                    end_idx = other_idx
                    break
            boundaries[name] = (start_idx, end_idx)

    return boundaries, lines


def parse_gazprombank_report(filepath):
    """Parse a Gazprombank PDF broker report and persist to DB. Returns report_id."""
    if pdfplumber is None:
        raise ImportError('pdfplumber is required to parse PDF files. Install with: pip install pdfplumber')

    init_db()

    filename = os.path.basename(filepath)
    conn = get_connection()
    try:
        cur = conn.cursor()

        # ── Extract metadata ──────────────────────────────────────
        contract = ''
        investor = ''
        period_start = ''
        period_end = ''
        broker_name = 'gazprombank'

        with pdfplumber.open(filepath) as pdf:
            # Get full text of first page for metadata
            first_text = pdf.pages[0].extract_text()

            for line in first_text.split('\n'):
                line = line.strip()
                m = re.search(r'за период с\s+(\S+)\s+по\s+(\S+)', line)
                if m:
                    period_start = _fmt_date(m.group(1))
                    period_end = _fmt_date(m.group(2))

                m = re.search(r'Наименование Клиента:\s*(.+?)$', line)
                if m:
                    investor = m.group(1).strip()

                m = re.search(r'Договор на брокерское обслуживание:\s*(\S+)', line)
                if m:
                    contract = m.group(1).strip()

                m = re.search(r'Брокер:\s*(.+?)$', line)
                if m:
                    broker_val = m.group(1).strip()
                    if 'Ньютон' in broker_val or 'Газпром' in broker_val:
                        broker_name = 'gazprombank'

            # Upsert report
            cur.execute("SELECT id FROM report WHERE filename=?", (filename,))
            existing = cur.fetchone()
            if existing:
                report_id = existing['id']
                for tbl in ('trade', 'repo', 'cash_flow', 'portfolio', 'financial_result'):
                    cur.execute(f"DELETE FROM {tbl} WHERE report_id=?", (report_id,))
                cur.execute("""UPDATE report SET contract=?, investor=?, period_start=?, period_end=?,
                               source_type='broker_report', broker=?
                               WHERE id=?""", (contract, investor, period_start, period_end, broker_name, report_id))
            else:
                cur.execute("""
                    INSERT INTO report(filename, contract, investor, period_start, period_end, source_type, broker)
                    VALUES (?, ?, ?, ?, ?, 'broker_report', ?)
                """, (filename, contract, investor, period_start, period_end, broker_name))
                cur.execute("SELECT id FROM report WHERE filename=?", (filename,))
                report_id = cur.fetchone()['id']

            # ── Collect all data ──────────────────────────────────
            full_text = _collect_text(pdf)

            # ── Parse Section 1: Cash Flow State ──────────────────
            _parse_cash_state(full_text, cur, report_id)

            # ── Find section boundaries ───────────────────────────
            boundaries, all_lines = _find_section_boundaries(full_text)

            # ── Parse trades from tables ──────────────────────────
            trade_start = boundaries.get('trades_5.1', (None, None))[0]
            trade_end = boundaries.get('trades_5.1', (None, None))[1]
            repo_start = boundaries.get('repo_5.6', (None, None))[0]

            # Collect all tables with their page context
            all_table_data = []
            for page in pdf.pages:
                page_text = page.extract_text()
                page_lines = page_text.split('\n')
                words = page.extract_words()
                tables = page.find_tables()
                for table in tables:
                    data = table.extract()
                    if data:
                        all_table_data.append({
                            'data': data,
                            'page_text': page_text,
                            'page_lines': page_lines,
                            'words': words,
                            'bbox': table.bbox,
                        })

            # Find the first line number of section 5.1 in the full text
            # to distinguish trade tables from REPO tables
            first_trade_line = trade_start if trade_start is not None else 0
            first_repo_line = repo_start if repo_start is not None else len(all_lines)

            # Parse trade data from tables (before REPO section)
            _parse_trade_tables(all_table_data, all_lines, first_trade_line, first_repo_line,
                                cur, report_id)

            # Parse REPO data from tables (after REPO section starts)
            _parse_repo_tables(all_table_data, all_lines, first_repo_line, len(all_lines),
                               cur, report_id)

            # ── Parse non-trading operations ──────────────────────
            _parse_non_trading(pdf, cur, report_id)

        conn.commit()
        print(f'[gazprombank] Загружен {filename} (report_id={report_id})')
        return report_id

    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _parse_cash_state(full_text, cur, report_id):
    """Parse Section 1 cash state from full text.
    Extracts cash flow items as pseudo-cash_flow entries.
    """
    # Find the cash section
    m = re.search(r'1\.\s*Состояние денежных средств на счете\s*\n(.*?)(?=\n\s*\d+\.\s|\Z)', full_text, re.DOTALL)
    if not m:
        return

    section = m.group(1)
    lines = section.split('\n')

    in_flow = False
    in_outflow = False
    in_fees = False
    inflow_lines = []
    outflow_lines = []
    fee_lines = []

    for line in lines:
        line = line.strip()
        if 'Сальдо расчетов (операции на фондовом и валютном рынках)' in line:
            in_flow = True
            in_outflow = False
            in_fees = False
            continue
        if 'Сальдо расчетов (неторговые операции)' in line:
            in_flow = False
            in_outflow = True
            in_fees = False
            continue
        if 'Уплаченная комиссия и сборы' in line:
            in_flow = False
            in_outflow = False
            in_fees = True
            continue
        if 'Исходящий остаток' in line or 'Входящий остаток' in line:
            in_flow = False
            in_outflow = False
            in_fees = False
            continue

        if in_flow and 'зачислено' in line.lower():
            inflow_lines.append(line)
        elif in_flow and 'списано' in line.lower():
            outflow_lines.append(line)
        elif in_fees and 'комиссия' in line.lower():
            fee_lines.append(line)

    # Insert cash flow entries for fee items
    for line in fee_lines:
        parts = line.rsplit(None, 1)
        if len(parts) == 2:
            desc = parts[0][:200]
            amount = parse_float(parts[1])
            if amount != 0:
                cur.execute("""
                    INSERT INTO cash_flow(report_id, date, description, currency, credit, debit, source)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (report_id, '', desc, 'RUR', 0, abs(amount), 'gazprombank'))


def _is_trade_row(row):
    """Check if a table row looks like a trade data row (has deal number, date, side)."""
    if not row or len(row) < 9:
        return False
    deal_num = str(row[0] or '').strip()
    date_val = str(row[1] or '').strip()
    side_val = str(row[3] or '').strip()
    currency_val = str(row[5] or '').strip() if len(row) > 5 else ''
    # Skip summary rows
    if not deal_num or not date_val:
        return False
    if currency_val in ('оборот', 'изменение'):
        return False
    if 'Покупка' in side_val or 'Продажа' in side_val:
        return True
    return False


def _is_instrument_header_line(text_line):
    """Check if a text line looks like an instrument header (not a deal number or date)."""
    s = text_line.strip()
    if not s:
        return False
    # Skip deal number lines
    if re.match(r'^[A-Z]-\d+', s):
        return False
    # Skip date lines
    if re.match(r'^\d{2}\.\d{2}\.\d{4}', s):
        return False
    # Skip skip lines
    if re.match(r'^\d+\.\d+', s):
        return False
    # Skip "Итого" lines
    if s.startswith('Итого'):
        return False
    # Skip "Общий итог"
    if s.startswith('Общий'):
        return False
    # Skip header row of table
    if 'Номер сделки' in s:
        return False
    # Should contain RUR/USD or look like concatenated issuer+security
    if 'RUR' in s or 'USD' in s:
        return True
    # Should look like a non-numeric non-deal string
    if re.match(r'^[А-Яа-яA-Z]', s) and not re.match(r'^\d', s):
        return True
    return False


def _extract_instrument_from_words(words, table_bbox):
    """Extract instrument info from words just above a table."""
    if not table_bbox:
        return None, None, None
    table_top = table_bbox[1]
    candidates = [w for w in words
                  if w['top'] < table_top and w['top'] > table_top - 35]
    if not candidates:
        return None, None, None
    candidates.sort(key=lambda w: (w['top'], w['x0']))
    inst_line = ' '.join(w['text'] for w in candidates)
    if _is_instrument_header_line(inst_line):
        sec_name, sec_code, _, currency = _parse_instrument_block(inst_line)
        return sec_name or None, sec_code or None, currency or None
    return None, None, None


def _has_paired_deals(rows):
    """Check if a set of rows has paired deal numbers (same deal with both buy and sell)."""
    deal_sides = {}
    for row in rows:
        if not _is_trade_row(row):
            continue
        deal_num = str(row[0] or '').strip()
        side_val = str(row[3] or '').strip()
        if deal_num:
            if deal_num not in deal_sides:
                deal_sides[deal_num] = set()
            deal_sides[deal_num].add('Покупка' if 'Покупка' in side_val else 'Продажа')
    paired = sum(1 for sides in deal_sides.values() if len(sides) > 1)
    return paired > 0


def _parse_trade_tables(all_table_data, all_lines, section_start, section_end, cur, report_id):
    """Parse trade data from tables within section boundaries."""
    trade_count = 0
    current_sec_name = ''
    current_sec_code = ''
    current_currency = 'RUR'

    for td in all_table_data:
        data = td['data']
        words = td.get('words', [])
        bbox = td.get('bbox')

        # Skip tables with paired deal numbers (those are REPO)
        if _has_paired_deals(data):
            continue

        first_row = data[0]
        first_row_text = ' '.join(str(c or '') for c in first_row)

        # If this is a header-only table, skip it (instrument info comes from text)
        if 'Номер сделки' in first_row_text and 'Дата сделки' in first_row_text:
            if len(data) <= 1:
                continue
            for row in data[1:]:
                if _is_trade_row(row):
                    _insert_trade_row(row, cur, report_id,
                                      current_sec_name, current_sec_code, current_currency)
                    trade_count += 1
            continue

        # Check if first row looks like trade data directly (no header)
        if _is_trade_row(first_row):
            sn, sc, cu = _extract_instrument_from_words(words, bbox)
            if sn:
                current_sec_name = sn
                current_sec_code = _lookup_ticker(sn, sc or '')
                current_currency = cu or 'RUR'

            for row in data:
                if _is_trade_row(row):
                    _insert_trade_row(row, cur, report_id,
                                      current_sec_name, current_sec_code, current_currency)
                    trade_count += 1

    if trade_count > 0:
        print(f'  [gazprombank] Биржевых сделок: {trade_count}')


def _insert_trade_row(row, cur, report_id, sec_name, sec_code, currency):
    """Insert a single trade row into the trade table."""
    deal_num = str(row[0] or '').strip()
    date_val = str(row[1] or '').strip()
    time_val = str(row[2] or '').strip()
    side_val = str(row[3] or '').strip()
    price_val = str(row[4] or '').strip()
    currency_val = str(row[5] or '').strip()
    qty_val = str(row[6] or '').strip()
    nkd_val = str(row[7] or '').strip()
    amount_val = str(row[8] or '').strip()

    side_str = 'Покупка' if 'Покупка' in side_val else 'Продажа'
    qty = parse_int(qty_val)
    price = parse_float(price_val)
    amount = parse_float(amount_val)
    nkd = parse_float(nkd_val)
    trade_date = _fmt_date(date_val)
    trade_time = _fmt_time(time_val)
    curr = currency_val or currency or 'RUR'

    cur.execute("""
        INSERT OR IGNORE INTO trade(report_id, trade_date, settle_date, trade_time,
            security_name, security_code, currency, side, quantity, price,
            amount, nkd, broker_fee, exchange_fee, deal_number, comment, status, source)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (report_id, trade_date, trade_date, trade_time,
          sec_name or '', sec_code or '', curr,
          side_str, qty, price,
          amount, nkd, 0, 0, deal_num, '', 'executed', 'gazprombank'))


def _parse_repo_tables(all_table_data, all_lines, section_start, section_end, cur, report_id):
    """Parse REPO trades from tables within the REPO section."""
    repo_count = 0
    current_sec_name = ''
    current_sec_code = ''
    current_currency = 'RUR'

    for td in all_table_data:
        data = td['data']
        words = td.get('words', [])
        bbox = td.get('bbox')

        # REPO tables have the same format but with paired Продажа+Покупка rows
        first_row = data[0]
        first_row_text = ' '.join(str(c or '') for c in first_row)

        if 'Номер сделки' in first_row_text and 'Дата сделки' in first_row_text:
            if len(data) <= 1:
                continue
            cnt = _process_repo_rows(data[1:], cur, report_id,
                                     current_sec_name, current_sec_code, current_currency)
            repo_count += cnt
            continue

        if _is_trade_row(first_row):
            deal_nums = [str(r[0] or '').strip() for r in data if _is_trade_row(r)]
            unique = set(deal_nums)
            if len(deal_nums) != len(unique):
                sn, sc, cu = _extract_instrument_from_words(words, bbox)
                if sn:
                    current_sec_name = sn
                    current_sec_code = _lookup_ticker(sn, sc or '')
                    current_currency = cu or 'RUR'
                cnt = _process_repo_rows(data, cur, report_id,
                                         current_sec_name, current_sec_code, current_currency)
                repo_count += cnt

    if repo_count > 0:
        print(f'  [gazprombank] Сделок РЕПО: {repo_count}')


def _process_repo_rows(rows, cur, report_id, sec_name, sec_code, currency):
    """Process REPO rows by pairing Продажа+Покупка with the same deal number.
    Returns count of REPO pairs inserted.
    """
    repo_rows = {}
    for row in rows:
        if not _is_trade_row(row):
            continue
        deal_num = str(row[0] or '').strip()
        if not deal_num:
            continue
        side_val = str(row[3] or '').strip()
        if deal_num not in repo_rows:
            repo_rows[deal_num] = {}
        if 'Продажа' in side_val:
            repo_rows[deal_num]['sell'] = row
        else:
            repo_rows[deal_num]['buy'] = row

    count = 0
    for deal_num, parts in repo_rows.items():
        sell_row = parts.get('sell')
        buy_row = parts.get('buy')
        if not sell_row or not buy_row:
            continue

        sell_qty = parse_int(str(sell_row[6] or '0').strip())
        sell_price = parse_float(str(sell_row[4] or '0').strip())
        sell_amount = parse_float(str(sell_row[8] or '0').strip())
        sell_nkd = parse_float(str(sell_row[7] or '0').strip())
        sell_date = _fmt_date(str(sell_row[1] or '').strip())

        buy_qty = parse_int(str(buy_row[6] or '0').strip())
        buy_price = parse_float(str(buy_row[4] or '0').strip())
        buy_amount = parse_float(str(buy_row[8] or '0').strip())
        buy_nkd = parse_float(str(buy_row[7] or '0').strip())
        buy_date = _fmt_date(str(buy_row[1] or '').strip())
        buy_time = _fmt_time(str(buy_row[2] or '').strip())

        amount_part1 = sell_amount if sell_amount != 0 else buy_amount
        amount_part2 = buy_amount if buy_amount != 0 else sell_amount
        curr = str(sell_row[5] or '').strip() or str(buy_row[5] or '').strip() or currency or 'RUR'

        cur.execute("""
            INSERT OR IGNORE INTO repo(report_id, trade_date, trade_time, security_name,
                security_code, currency, side, quantity, price_part1, nkd_part1,
                amount_part1, date_part1, repo_rate, repo_interest, price_part2,
                nkd_part2, amount_part2, date_part2, broker_fee, exchange_fee,
                deal_number, status, source)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (report_id, sell_date or buy_date, buy_time,
              sec_name or '', sec_code or '', curr,
              'Продажа', sell_qty or buy_qty,
              sell_price, sell_nkd, amount_part1, sell_date,
              0, 0,
              buy_price, buy_nkd, amount_part2, buy_date,
              0, 0, deal_num, 'РЕПО', 'gazprombank'))
        count += 1
    return count


def _parse_non_trading(pdf, cur, report_id):
    """Parse non-trading operations (section 8) — cash in/out and securities in/out."""
    cash_count = 0
    for page in pdf.pages:
        page_text = page.extract_text()
        if 'Неторговые операции' not in page_text:
            continue

        tables = page.find_tables()
        for table in tables:
            data = table.extract()
            if not data or len(data) < 2:
                continue

            header = data[0]
            header_text = ' '.join(str(h or '') for h in header)

            # Cash operations: [Дата, Тип операции, Сумма, Валюта]
            if 'Дата' in header_text and 'Тип операции' in header_text and 'Сумма' in header_text:
                for row in data[1:]:
                    if len(row) < 3:
                        continue
                    date_val = str(row[0] or '').strip()
                    op_type = str(row[1] or '').strip()
                    amount_val = str(row[2] or '').strip()
                    currency_val = str(row[3] or '').strip() if len(row) > 3 else 'RUR'

                    if not date_val or not op_type:
                        continue

                    trade_date = _fmt_date(date_val)
                    amount = parse_float(amount_val)

                    if amount >= 0:
                        credit = amount
                        debit = 0
                    else:
                        credit = 0
                        debit = abs(amount)

                    cur.execute("""
                        INSERT INTO cash_flow(report_id, date, description, currency, credit, debit, source)
                        VALUES (?,?,?,?,?,?,?)
                    """, (report_id, trade_date, op_type[:200], currency_val, credit, debit, 'gazprombank'))
                    cash_count += 1

    if cash_count > 0:
        print(f'  [gazprombank] Неторговых операций: {cash_count}')
