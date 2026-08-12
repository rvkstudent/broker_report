"""Parse Gazprombank (Ньютон Инвестиции) XLS broker reports and insert data into SQLite.

Формат XLS-отчёта брокера «Ньютон Инвестиции» (Газпромбанк):
  - Единый XLS файл со всеми секциями
  - Секция 5.1: Биржевые сделки
  - Секция 5.6: Сделки РЕПО
  - Секция 8: Неторговые операции

Структура строк сделок (C0-C18):
  C0:  Номер сделки (B-XXXX-XXXXXX)
  C1:  Дата сделки (DD.MM.YYYY)
  C2:  Время сделки (HH:MM:SS)
  C3:  Вид сделки (Покупка/Продажа)
  C4:  Цена одной ЦБ
  C5:  Валюта цены
  C6:  Количество ЦБ, шт.
  C7:  НКД
  C8:  Сумма сделки
  C9:  Валюта суммы сделки
  C10: Брокерская комиссия
  C11: Валюта брокерской комиссии
  C12: Комиссия ТС / Часть РЕПО (1-я часть/2-я часть)
  C13: Гербовый сбор
  C14: Дата оплаты плановая
  C15: Дата оплаты фактическая
  C16: Дата поставки плановая
  C17: Дата поставки фактическая
  C18: Место совершения сделки

Инструменты — строки перед сделками (только C0):
  'MS0003000000  ОФЗ 26248  26248RMFS  RUR'
  'АО ВИМ Инвестиции  LQDT ETF  3915  RUR'
"""

import re
import os
from datetime import datetime
from app.db import get_connection, init_db

try:
    import pandas as pd
except ImportError:
    pd = None


# ── Helpers ───────────────────────────────────────────────────

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
    if val is None or (isinstance(val, float) and pd is not None and pd.isna(val)):
        return ''
    if isinstance(val, datetime):
        return val.strftime('%d.%m.%Y')
    if hasattr(val, 'strftime'):
        return val.strftime('%d.%m.%Y')
    s = str(val).strip()
    m = re.match(r'(\d{2})\.(\d{2})\.(\d{4})', s)
    if m:
        return f'{m.group(1)}.{m.group(2)}.{m.group(3)}'
    m = re.match(r'(\d{4})-(\d{2})-(\d{2})', s)
    if m:
        return f'{m.group(3)}.{m.group(2)}.{m.group(1)}'
    return s[:10]


def _fmt_time(val):
    if val is None or (isinstance(val, float) and pd is not None and pd.isna(val)):
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


def _get_cell(df, row, col, default=None):
    """Safely get cell value, return default if out of bounds or NaN."""
    if row >= df.shape[0] or col >= df.shape[1]:
        return default
    v = df.iloc[row, col]
    if pd is not None and pd.isna(v):
        return default
    return v


def _is_trade_row(df, row):
    """Check if row is a trade row (starts with B-XXXX-XXXXXX)."""
    v = _get_cell(df, row, 0, '')
    if v is None:
        return False
    s = str(v).strip()
    return bool(re.match(r'^B-\d+-\d+', s))


def _is_instrument_header(df, row):
    """Check if row is an instrument header (non-empty C0, not a trade, not 'Итого')."""
    v = _get_cell(df, row, 0, '')
    if v is None:
        return False
    s = str(v).strip()
    if not s:
        return False
    if s.startswith('Итого'):
        return False
    if re.match(r'^B-\d+-\d+', s):
        return False
    if 'Номер сделки' in s or 'Дата сделки' in s:
        return False
    if s.startswith('5.') or s.startswith('8.'):
        return False
    if s.startswith(('1.', '2.', '3.', '4.')):
        return False
    return True


# ── Instrument parsing ────────────────────────────────────────

_GPX_TICKER_MAP = {
    '26241RMFS': 'SU26241RMFS8',
    '26243RMFS': 'SU26243RMFS4',
    '26248RMFS': '26248RMFS',
    'LQDT': 'LQDT',
    'LQDT ETF': 'LQDT',
    'МТС-ао': 'MTSS',
    'Атомэнергопром 001Р-06 (USD)': 'RU000A10C3M0',
    'Атомэнергопром 001Р-06': 'RU000A10C3M0',
}


def _lookup_ticker(sec_name, sec_code):
    for key, ticker in _GPX_TICKER_MAP.items():
        if key in sec_name or key in sec_code or sec_name in key or sec_code in key:
            return ticker
    return sec_code or sec_name


def _parse_instrument_header(text):
    """Parse instrument header string.

    Examples:
      'MS0003000000  SU26241RMFS8  26241RMFS  RUR' → ('SU26241RMFS8', '26241RMFS', 'RUR')
      'АО ВИМ Инвестиции  LQDT ETF  3915  RUR'    → ('LQDT ETF', '3915', 'RUR')
      'ПАО "МТС"  МТС-ао  1-01-04715-A  RUR'      → ('МТС-ао', '1-01-04715-A', 'RUR')
    """
    if not text:
        return '', '', ''

    parts = [p.strip() for p in text.split() if p.strip()]

    # Filter out MS/MC codes
    issuer_codes = {'MS0003000000', 'MC0302000000', 'MC0302000000'}
    parts = [p for p in parts if p not in issuer_codes]

    currency = ''
    if parts and parts[-1] in ('RUR', 'USD', 'RUB'):
        currency = parts.pop(-1)

    if not parts:
        return '', '', currency

    # Try to find the security name - after issuer code
    # Common patterns:
    # - ОФЗ 26248 26248RMFS → sec_name='ОФЗ 26248', sec_code='26248RMFS'
    # - SU26241RMFS8 26241RMFS → sec_name='SU26241RMFS8', sec_code='26241RMFS'
    # - LQDT ETF 3915 → sec_name='LQDT ETF', sec_code='3915'
    # - МТС-ао 1-01-04715-A → sec_name='МТС-ао', sec_code='1-01-04715-A'
    # - Атомэнергопром 001Р-06 (USD) 4B02-06-55319-E-001P → sec_name, sec_code

    # ОФЗ pattern
    text_joined = ' '.join(parts)
    m_ofz = re.match(r'ОФЗ\s+(\d{5})\s+(\d{5})RMFS', text_joined)
    if m_ofz:
        return f'ОФЗ {m_ofz.group(1)}', f'{m_ofz.group(2)}RMFS', currency

    # SU...RMFS8 pattern
    m_su = re.match(r'(SU\d+RMFS8)\s+(\d+RMFS)', text_joined)
    if m_su:
        return m_su.group(1), m_su.group(2), currency

    # LQDT ETF pattern
    m_lqdt = re.match(r'(LQDT ETF)\s+(\S+)', text_joined)
    if m_lqdt:
        return m_lqdt.group(1), m_lqdt.group(2), currency

    # Атомэнергопром pattern
    m_atom = re.match(r'(Атомэнергопром\s+\S+)\s+\(USD\)\s+(\S+)', text_joined)
    if m_atom:
        return m_atom.group(1) + ' (USD)', m_atom.group(2), currency

    # МТС-ао pattern  
    m_mts = re.match(r'(МТС-ао)\s+(\S+)', text_joined)
    if m_mts:
        return m_mts.group(1), m_mts.group(2), currency

    # Fallback: first word as name, last as code
    if len(parts) >= 2:
        sec_name = ' '.join(parts[:-1])
        sec_code = parts[-1]
        # Check if last part looks like a reg number
        if re.match(r'^[\dA-Z-]+$', sec_code):
            return sec_name, sec_code, currency

    return parts[0], parts[0], currency


# ── Main parser ──────────────────────────────────────────────

def parse_gazprombank_xls_report(filepath):
    """Parse Gazprombank XLS broker report. Returns report_id."""
    if pd is None:
        raise ImportError('pandas is required. Install: pip install pandas openpyxl')

    init_db()

    filename = os.path.basename(filepath)
    conn = get_connection()
    try:
        cur = conn.cursor()

        # ── Read file ──────────────────────────────────────────────
        df = pd.read_excel(filepath, header=None)
        max_row = df.shape[0]
        max_col = df.shape[1]

        # ── Extract metadata from header ───────────────────────────
        contract = ''
        investor = ''
        period_start = ''
        period_end = ''

        for r in range(min(15, max_row)):
            c0 = str(_get_cell(df, r, 0, '') or '')
            c2 = str(_get_cell(df, r, 2, '') or '')
            c6 = str(_get_cell(df, r, 6, '') or '')

            m = re.search(r'за период с\s+(\S+)\s+по\s+(\S+)', c0)
            if m:
                period_start = _fmt_date(m.group(1))
                period_end = _fmt_date(m.group(2))

            if 'Наименование Клиента' in c0 and c2:
                investor = c2

            if 'Договор на брокерское обслуживание' in c0 and c2:
                contract = c2.strip()

        # ── Upsert report ──────────────────────────────────────────
        cur.execute("SELECT id FROM report WHERE filename=?", (filename,))
        existing = cur.fetchone()
        if existing:
            report_id = existing['id']
            for tbl in ('trade', 'repo', 'cash_flow', 'portfolio', 'financial_result'):
                cur.execute(f"DELETE FROM {tbl} WHERE report_id=?", (report_id,))
            cur.execute("""UPDATE report SET contract=?, investor=?, period_start=?, period_end=?,
                           source_type='broker_report', broker='gazprombank'
                           WHERE id=?""", (contract, investor, period_start, period_end, report_id))
        else:
            cur.execute("""INSERT INTO report(filename, contract, investor, period_start, period_end,
                           source_type, broker)
                           VALUES (?, ?, ?, ?, ?, 'broker_report', 'gazprombank')""",
                        (filename, contract, investor, period_start, period_end))
            cur.execute("SELECT id FROM report WHERE filename=?", (filename,))
            report_id = cur.fetchone()['id']

        # ── Find sections ──────────────────────────────────────────
        col_0 = [str(_get_cell(df, r, 0, '') or '') for r in range(max_row)]

        # Find section 5.1 (биржевые сделки)
        sec_51_start = None
        sec_51_header = None
        for r, val in enumerate(col_0):
            if '5.1' in val and 'Биржевые' in val:
                sec_51_start = r
                # Header row is next, then sub-header, then instrument data
                sec_51_header = r + 1
                break

        # Find section 5.6 (РЕПО)
        sec_56_start = None
        sec_56_header = None
        for r, val in enumerate(col_0):
            if '5.6' in val and 'РЕПО' in val:
                sec_56_start = r
                sec_56_header = r + 1
                break

        # Find section 8 (неторговые операции)
        sec_8_start = None
        for r, val in enumerate(col_0):
            if '8. Неторговые операции' in val or (val.startswith('8.') and 'Неторговые' in val):
                sec_8_start = r
                break

        # Find section 8.1.1 (cash operations)
        sec_811_start = None
        sec_811_header = None
        for r, val in enumerate(col_0):
            if '8.1.1' in val and 'ДС' in val:
                sec_811_start = r
                sec_811_header = r + 1
                break

        # ── Parse trades (section 5.1) ─────────────────────────────
        trade_count = 0
        if sec_51_start is not None:
            trade_count = _parse_trades(df, cur, report_id, sec_51_start, sec_56_start or sec_8_start or max_row, max_col)

        if trade_count > 0:
            print(f'  [gazprombank_xls] Биржевых сделок: {trade_count}')

        # ── Parse REPO trades (section 5.6) ────────────────────────
        repo_count = 0
        if sec_56_start is not None:
            repo_count = _parse_repo(df, cur, report_id, sec_56_start, sec_8_start or max_row, max_col)

        if repo_count > 0:
            print(f'  [gazprombank_xls] Сделок РЕПО: {repo_count}')

        # ── Parse non-trading operations (section 8.1.1) ───────────
        cash_count = 0
        if sec_811_start is not None:
            cash_count = _parse_cash_flow(df, cur, report_id, sec_811_start, max_row, max_col)

        if cash_count > 0:
            print(f'  [gazprombank_xls] Неторговых операций: {cash_count}')

        conn.commit()
        print(f'[gazprombank_xls] Загружен {filename} (report_id={report_id})')
        return report_id

    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _parse_trades(df, cur, report_id, sec_start, sec_end, max_col):
    """Parse exchange trades from section 5.1."""
    count = 0
    current_sec_name = ''
    current_sec_code = ''

    data_start = sec_start + 3  # Skip header + sub-header rows

    for r in range(data_start, min(sec_end, df.shape[0])):
        c0 = str(_get_cell(df, r, 0, '') or '').strip()

        # Check for instrument header
        if c0 and not c0.startswith('B-') and not c0.startswith('Итого'):
            # Skip empty, section headers etc.
            if not re.match(r'^(5\.|8\.)', c0):
                sec_name, sec_code, _ = _parse_instrument_header(c0)
                if sec_name:
                    current_sec_name = sec_name
                    current_sec_code = _lookup_ticker(sec_name, sec_code)
            continue

        # Skip total rows
        if c0.startswith('Итого'):
            continue

        # Parse trade row
        if not re.match(r'^B-\d+-\d+', c0):
            continue

        deal_num = c0
        trade_date = _fmt_date(_get_cell(df, r, 1))
        trade_time = _fmt_time(_get_cell(df, r, 2))
        side = str(_get_cell(df, r, 3, '') or '').strip()
        price = parse_float(_get_cell(df, r, 4))
        price_currency = str(_get_cell(df, r, 5, 'RUR') or 'RUR').strip()
        qty = parse_int(_get_cell(df, r, 6))
        nkd = parse_float(_get_cell(df, r, 7))
        amount = parse_float(_get_cell(df, r, 8))
        amount_currency = str(_get_cell(df, r, 9, 'RUR') or 'RUR').strip()
        broker_fee = parse_float(_get_cell(df, r, 10))
        venue = str(_get_cell(df, r, 18, '') or '').strip()

        if not trade_date or not side:
            continue
        if side not in ('Покупка', 'Продажа'):
            continue

        # Determine currency
        currency = amount_currency if amount_currency else price_currency

        # Skip REPO trades (1-я часть / 2-я часть)
        part_col = str(_get_cell(df, r, 12, '') or '')
        if 'часть' in part_col:
            continue

        cur.execute("""INSERT OR IGNORE INTO trade
            (report_id, trade_date, settle_date, trade_time,
             security_name, security_code, currency, side, quantity, price,
             amount, nkd, broker_fee, exchange_fee, deal_number, comment, status, source)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
            report_id, trade_date, trade_date, trade_time,
            current_sec_name, current_sec_code, currency,
            side, qty, price,
            amount, nkd, broker_fee, 0, deal_num,
            venue[:200], 'executed', 'gazprombank'
        ))
        if cur.rowcount > 0:
            count += 1

    return count


def _parse_repo(df, cur, report_id, sec_start, sec_end, max_col):
    """Parse REPO trades from section 5.6.

    REPO trades have pairs: (Продажа + Покупка) with same deal number.
    1-я часть = Продажа (первая часть), 2-я часть = Покупка (вторая часть).
    """
    count = 0
    current_sec_name = ''
    current_sec_code = ''

    data_start = sec_start + 3

    for r in range(data_start, min(sec_end, df.shape[0])):
        c0 = str(_get_cell(df, r, 0, '') or '').strip()

        # Instrument header
        if c0 and not c0.startswith('B-') and not c0.startswith('Итого'):
            if not re.match(r'^(5\.|8\.)', c0):
                sec_name, sec_code, _ = _parse_instrument_header(c0)
                if sec_name:
                    current_sec_name = sec_name
                    current_sec_code = _lookup_ticker(sec_name, sec_code)
            continue

        if c0.startswith('Итого'):
            continue

        if not re.match(r'^B-\d+-\d+', c0):
            continue

        deal_num = c0
        trade_date = _fmt_date(_get_cell(df, r, 1))
        trade_time = _fmt_time(_get_cell(df, r, 2))
        side = str(_get_cell(df, r, 3, '') or '').strip()
        price = parse_float(_get_cell(df, r, 4))
        qty = parse_int(_get_cell(df, r, 6))
        amount = parse_float(_get_cell(df, r, 8))
        broker_fee = parse_float(_get_cell(df, r, 10))
        part_type = str(_get_cell(df, r, 12, '') or '').strip()

        venue = str(_get_cell(df, r, 18, '') or '').strip()
        settle_date = _fmt_date(_get_cell(df, r, 14)) or trade_date
        delivery_date = _fmt_date(_get_cell(df, r, 16)) or trade_date

        if not trade_date or not side:
            continue
        if side not in ('Покупка', 'Продажа'):
            continue

        is_part1 = '1-я часть' in part_type or '1-я' in part_type
        is_part2 = '2-я часть' in part_type or '2-я' in part_type

        if is_part1:
            cur.execute("""INSERT OR IGNORE INTO repo
                (report_id, trade_date, trade_time, security_name,
                 security_code, currency, side, quantity, price_part1, nkd_part1,
                 amount_part1, date_part1, repo_rate, repo_interest, price_part2,
                 nkd_part2, amount_part2, date_part2, broker_fee, exchange_fee,
                 deal_number, status, source)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                report_id, trade_date, trade_time,
                current_sec_name, current_sec_code, 'RUR',
                side, qty, price, 0,
                amount, delivery_date, 0, 0, 0,
                0, 0, '', broker_fee, 0,
                deal_num, f'РЕПО 1-я часть: {venue[:100]}', 'gazprombank'
            ))
            if cur.rowcount > 0:
                count += 1

        elif is_part2:
            # Need to find matching part1 by deal_number
            cur.execute("""UPDATE repo SET
                price_part2=?, amount_part2=?, date_part2=?
                WHERE deal_number=? AND source='gazprombank'
                AND (price_part2 IS NULL OR price_part2=0)""",
                        (price, amount, delivery_date, deal_num))

            if cur.rowcount == 0:
                # Insert as standalone part2
                cur.execute("""INSERT OR IGNORE INTO repo
                    (report_id, trade_date, trade_time, security_name,
                     security_code, currency, side, quantity, price_part1, nkd_part1,
                     amount_part1, date_part1, repo_rate, repo_interest, price_part2,
                     nkd_part2, amount_part2, date_part2, broker_fee, exchange_fee,
                     deal_number, status, source)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                    report_id, trade_date, trade_time,
                    current_sec_name, current_sec_code, 'RUR',
                    side, qty, 0, 0,
                    0, '', 0, 0, price,
                    0, amount, delivery_date, broker_fee, 0,
                    deal_num, f'РЕПО 2-я часть: {venue[:100]}', 'gazprombank'
                ))
                if cur.rowcount > 0:
                    count += 1
        else:
            # REPO without part marking - still insert
            cur.execute("""INSERT OR IGNORE INTO repo
                (report_id, trade_date, trade_time, security_name,
                 security_code, currency, side, quantity, price_part1, nkd_part1,
                 amount_part1, date_part1, repo_rate, repo_interest, price_part2,
                 nkd_part2, amount_part2, date_part2, broker_fee, exchange_fee,
                 deal_number, status, source)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                report_id, trade_date, trade_time,
                current_sec_name, current_sec_code, 'RUR',
                side, qty, price, 0,
                amount, delivery_date, 0, 0, 0,
                0, 0, '', broker_fee, 0,
                deal_num, f'РЕПО: {venue[:100]}', 'gazprombank'
            ))
            if cur.rowcount > 0:
                count += 1

    return count


def _parse_cash_flow(df, cur, report_id, sec_start, max_row, max_col):
    """Parse non-trading cash operations from section 8.1.1."""
    count = 0

    # Find header row with 'Дата', 'Тип операции', 'Сумма', 'Валюта'
    header_row = sec_start + 1
    data_start = header_row + 1

    # Check if header row is actually the one with column names
    if data_start < df.shape[0]:
        check_val = str(_get_cell(df, data_start, 0, '') or '')
        if not re.match(r'\d{2}\.\d{2}\.\d{4}', check_val):
            # Try next row
            data_start += 1

    for r in range(data_start, min(max_row, df.shape[0])):
        c0 = str(_get_cell(df, r, 0, '') or '').strip()

        # Stop if we hit another section
        if c0.startswith(('8.2', '9.', '10.')):
            break
        if not c0:
            continue

        # Skip non-data rows
        if not re.match(r'\d{2}\.\d{2}\.\d{4}', c0):
            continue

        date_val = _fmt_date(c0)
        op_type = str(_get_cell(df, r, 1, '') or '').strip()
        amount_str = _get_cell(df, r, 2)
        currency = str(_get_cell(df, r, 3, 'RUR') or 'RUR').strip()
        comment = str(_get_cell(df, r, 4, '') or '').strip()

        if not date_val or not op_type:
            continue

        amount = parse_float(amount_str)
        credit = amount if amount >= 0 else 0
        debit = abs(amount) if amount < 0 else 0

        desc = f'{op_type} — {comment}' if comment else op_type

        cur.execute("""INSERT INTO cash_flow(report_id, date, description, currency, credit, debit, source)
            VALUES (?,?,?,?,?,?,?)""",
                    (report_id, date_val, desc[:200], currency, credit, debit, 'gazprombank'))
        count += 1

    return count
