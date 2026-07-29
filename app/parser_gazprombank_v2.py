"""
Parser for new Gazprombank (Newton Investments) PDF broker report format.

Uses pypdf for text extraction. Two-pass approach:
  Pass 1: Find all instrument header positions in the full text
  Pass 2: For each trade, find the nearest instrument before it
"""

import re
import os
from app.db import get_connection, init_db

try:
    from pypdf import PdfReader
    HAS_PYPDF = True
except ImportError:
    HAS_PYPDF = False


# ── Helpers ───────────────────────────────────────────────────

def parse_float(s):
    if s is None:
        return 0.0
    if isinstance(s, (int, float)):
        return float(s)
    s = str(s).strip().replace('\xa0', '').replace('&nbsp;', '').replace(' ', '')
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
    if val is None:
        return ''
    s = str(val).strip()
    m = re.match(r'(\d{2})\.(\d{2})\.(\d{4})', s)
    if m:
        return f'{m.group(1)}.{m.group(2)}.{m.group(3)}'
    return s[:10]


def _fmt_time(val):
    if val is None:
        return ''
    s = str(val).strip()
    m = re.search(r'(\d{2}:\d{2}:\d{2})', s)
    if m:
        return m.group(1)
    m = re.search(r'(\d{2}:\d{2})', s)
    if m:
        return m.group(1) + ':00'
    return ''


def _extract_header_info(text):
    contract = ''
    investor = ''
    period_start = ''
    period_end = ''

    for line in text.split('\n'):
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

    return contract, investor, period_start, period_end


def _find_all_instruments(text):
    """Pass 1: Find all instrument header positions in the full text.

    Returns sorted list of (position, short_code, display_name).
    """
    positions = []

    # 1. MS...RMFS compact pattern: MS0003000000SU26241RMFS826241RMFSRUR
    for m in re.finditer(r'MS\d+.*?(\d{5})RMFS', text):
        code = m.group(1) + 'RMFS'
        # Try to determine if it's ОФЗ
        before = text[max(0, m.start()-30):m.start()]
        after = text[m.end():m.end()+30]
        if 'ОФЗ' in before or 'ОФЗ' in after:
            # Extract ОФЗ number
            m_ofz = re.search(r'ОФЗ\s*(\d{5})', text[max(0, m.start()-30):m.end()+30])
            name = f'ОФЗ {m_ofz.group(1)}' if m_ofz else code
        else:
            name = code
        positions.append((m.start(), code, name))

    # 2. ОФЗ with space: MS0003000000 ОФЗ 26248 26248RMFS RUR
    for m in re.finditer(r'ОФЗ\s+(\d{5})\s+(\d{5})RMFS', text):
        code = m.group(2) + 'RMFS'
        name = f'ОФЗ {m.group(1)}'
        positions.append((m.start(), code, name))

    # 3. Атомэнергопром
    for m in re.finditer(r'(?:MC030[^B]*?RUR|Атомэнергопром)', text):
        positions.append((m.start(), 'RU000A10C3M0', 'Атомэнергопром 001Р-06 (USD)'))

    # 4. МТС
    for m in re.finditer(r'(?:МТС-ао|ПАО\s*"МТС")', text):
        positions.append((m.start(), 'MTSS', 'МТС-ао'))

    # 5. LQDT ETF
    for m in re.finditer(r'(?:LQDT|АО ВИМ Инвестиции)', text):
        positions.append((m.start(), 'LQDT', 'LQDT ETF'))

    positions.sort(key=lambda x: x[0])
    return positions


# ── Main parser ──────────────────────────────────────────────

def parse_gazprombank_v2_report(filepath):
    if not HAS_PYPDF:
        raise ImportError('pypdf is required. Install: pip install pypdf')

    init_db()

    filename = os.path.basename(filepath)
    conn = get_connection()
    try:
        cur = conn.cursor()

        # Upsert report
        cur.execute("SELECT id FROM report WHERE filename=?", (filename,))
        existing = cur.fetchone()
        if existing:
            report_id = existing['id']
            for tbl in ('trade', 'repo', 'cash_flow'):
                cur.execute(f"DELETE FROM {tbl} WHERE report_id=?", (report_id,))
            cur.execute("""
                UPDATE report SET contract='', investor='', period_start='', period_end='',
                                  source_type='broker_report', broker='gazprombank'
                WHERE id=?
            """, (report_id,))
        else:
            cur.execute("""
                INSERT INTO report(filename, contract, investor, period_start, period_end,
                                   source_type, broker)
                VALUES (?, '', '', '', '', 'broker_report', 'gazprombank')
            """, (filename,))
            cur.execute("SELECT id FROM report WHERE filename=?", (filename,))
            report_id = cur.fetchone()['id']

        # Extract text
        reader = PdfReader(filepath)
        all_text = ''
        for page in reader.pages:
            all_text += page.extract_text()

        # Header info
        contract, investor, period_start, period_end = _extract_header_info(all_text)
        cur.execute("""
            UPDATE report SET contract=?, investor=?, period_start=?, period_end=?
            WHERE id=?
        """, (contract, investor, period_start, period_end, report_id))

        # PASS 1: Find instrument headers (after filtering portfolio section)
        trade_pattern = re.compile(
            r'B-(\d+-\d+)\s+'
            r'(\d{2}\.\d{2}\.\d{4})\s+'
            r'(\d{2}:\d{2}:\d{2})\s+'
            r'(Покупка|Продажа)\s+'
            r'([\d\s,]+\.\d+)\s+RUR\s+'
            r'(\d[\d\s]*?)\s+'
            r'([\d\s,]+\.\d{2})\s+'
            r'([\d\s,]+\.\d{2})\s+RUR\s+'
            r'(-?[\d\s,]+\.\d{2})'
        )

        trade_matches = list(trade_pattern.finditer(all_text))

        # Get all instrument positions, but filter out those before the first trade
        # (they belong to the portfolio section on page 1)
        all_inst_positions = _find_all_instruments(all_text)
        first_trade_pos = trade_matches[0].start() if trade_matches else 0
        inst_positions = [(p, c, n) for p, c, n in all_inst_positions
                          if p >= first_trade_pos - 200]

        # PASS 2: Map trades to instruments
        trade_count = 0
        inst_idx = 0
        last_name = ''
        last_code = ''

        for m in trade_matches:
            trade_pos = m.start()

            # Advance instrument index to find nearest instrument BEFORE this trade
            while inst_idx < len(inst_positions) and inst_positions[inst_idx][0] < trade_pos:
                last_code = inst_positions[inst_idx][1]
                last_name = inst_positions[inst_idx][2]
                inst_idx += 1

            deal_num = 'B-' + m.group(1)
            trade_date = _fmt_date(m.group(2))
            trade_time = _fmt_time(m.group(3))
            side = m.group(4)
            price = parse_float(m.group(5))
            qty = parse_int(m.group(6).strip())
            nkd = parse_float(m.group(7))
            amount = parse_float(m.group(8))
            broker_fee = parse_float(m.group(9))

            # Skip REPO trades
            context = all_text[max(0, trade_pos - 50):trade_pos + 200]
            if 'часть' in context or 'РЕПО' in context:
                continue

            cur.execute("""
                INSERT OR IGNORE INTO trade
                (report_id, trade_date, settle_date, trade_time,
                 security_name, security_code, currency, side, quantity, price,
                 amount, nkd, broker_fee, exchange_fee, deal_number, comment, status, source)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                report_id, trade_date, trade_date, trade_time,
                last_name, last_code, 'RUR',
                side, qty, price,
                amount, nkd, broker_fee, 0, deal_num, '', 'executed', 'gazprombank_v2'
            ))
            trade_count += 1

        if trade_count > 0:
            print(f'  [gazprombank_v2] Биржевых сделок: {trade_count}')

        # Parse non-trading operations
        cash_count = 0
        for marker in ['8. Неторговые операции', '8.1.1', 'Неторговые операции']:
            idx = all_text.find(marker)
            if idx >= 0:
                break

        if idx >= 0:
            section = all_text[idx:]
            end = section.find('от Брокера')
            if end > 0:
                section = section[:end]

            section_flat = ' '.join(section.split())
            cash_pattern = re.finditer(
                r'(\d{2}\.\d{2}\.\d{4})\s+'
                r'(Ввод\s+ДС|Вывод\s+ДС)\s+'
                r'(-?[\d\s]+\.\d{2})\s*'
                r'(?:RUR|RUB)\s+'
                r'([^0-9]+?)(?=\d{2}\.\d{2}\.\d{4}|\Z)',
                section_flat
            )

            for m in cash_pattern:
                date_val = _fmt_date(m.group(1))
                op_type = m.group(2).strip()
                amount = parse_float(m.group(3))
                credit = amount if amount >= 0 else 0
                debit = abs(amount) if amount < 0 else 0
                cur.execute("""
                    INSERT INTO cash_flow(report_id, date, description, currency, credit, debit, source)
                    VALUES (?,?,?,?,?,?,?)
                """, (report_id, date_val, op_type[:200], 'RUR', credit, debit, 'gazprombank_v2'))
                cash_count += 1

            if cash_count > 0:
                print(f'  [gazprombank_v2] Неторговых операций: {cash_count}')

        conn.commit()
        print(f'[gazprombank_v2] Загружен {filename} (report_id={report_id})')
        return report_id

    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
