"""Parse VTB (Bank VTB) broker XLSX reports and insert data into SQLite."""

import re
from datetime import datetime
from app.db import get_connection, init_db

try:
    import pandas as pd
except ImportError:
    pd = None


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
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return ''
    if isinstance(val, datetime):
        return val.strftime('%d.%m.%Y')
    if hasattr(val, 'strftime'):
        return val.strftime('%d.%m.%Y')
    s = str(val).strip()
    if len(s) == 10 and s[2] == '.' and s[5] == '.':
        return s
    m = re.match(r'(\d{4})-(\d{2})-(\d{2})', s)
    if m:
        return f'{m.group(3)}.{m.group(2)}.{m.group(1)}'
    return s[:10]


def _fmt_time(val):
    if val is None or (isinstance(val, float) and pd.isna(val)):
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


def _parse_sec_info(sec_str):
    if not sec_str or (isinstance(sec_str, float) and pd.isna(sec_str)):
        return '', '', ''
    parts = [p.strip() for p in str(sec_str).split(',')]
    sec_name = parts[0] if len(parts) > 0 else ''
    reg_number = parts[1] if len(parts) > 1 else ''
    isin = parts[2] if len(parts) > 2 else ''
    sec_code = _infer_sec_code(sec_name, reg_number, isin)
    return sec_name, sec_code, isin


_VTB_TICKER_MAP = {
    'LQDT ETF': 'LQDT',
    'CNYM ETF': 'CNYM',
    'МТС-ао': 'MTSS',
    'ВТБ ао': 'VTBR',
    'Русагро': 'RAGR',
    'НоваБев ао': 'BELU',
    'Атомэнп06 USD': 'RU000A10C3M0',
    'Атомэнп06': 'RU000A10C3M0',
    'iДЭНИКОЛБ1': 'RU000A100M47',
}


def _infer_sec_code(sec_name, reg_number, isin):
    if not sec_name:
        return reg_number or isin or ''
    for key, ticker in _VTB_TICKER_MAP.items():
        if key in sec_name or sec_name in key:
            return ticker
    if sec_name.startswith('ОФЗ'):
        return reg_number or sec_name
    if ' ETF' in sec_name:
        return sec_name.split()[0]
    return reg_number or sec_name


def parse_vtb_report(filepath):
    """Parse a VTB XLSX broker report and persist to DB. Returns report_id."""
    if pd is None:
        raise ImportError('pandas is required to parse XLSX files. Install with: pip install pandas openpyxl')

    init_db()

    df = pd.read_excel(filepath, header=None)
    max_row, max_col = df.shape

    conn = get_connection()
    cur = conn.cursor()

    # ── Extract header info ──────────────────────────────────
    filename = filepath.split('\\')[-1]
    contract = ''
    investor = ''
    period_start = ''
    period_end = ''

    title = str(df.iloc[0, 3] or '')
    m = re.search(r'за период с\s+(\S+)\s+по\s+(\S+)', title)
    if m:
        period_start = _fmt_date(m.group(1))
        period_end = _fmt_date(m.group(2))

    for r in range(min(15, max_row)):
        v1 = str(df.iloc[r, 1] or '').strip()
        v2 = str(df.iloc[r, 8] or '').strip() if max_col >= 9 else ''
        if 'Клиент' in v1 and v2:
            investor = v2
        if 'Соглашения' in v1 and v2:
            contract = v2

    cur.execute("SELECT id FROM report WHERE filename=?", (filename,))
    existing = cur.fetchone()
    if existing:
        report_id = existing['id']
        for tbl in ('trade', 'repo', 'cash_flow', 'portfolio', 'financial_result'):
            cur.execute(f"DELETE FROM {tbl} WHERE report_id=?", (report_id,))
        cur.execute("""UPDATE report SET contract=?, investor=?, period_start=?, period_end=?
                       WHERE id=?""", (contract, investor, period_start, period_end, report_id))
    else:
        cur.execute("""
            INSERT INTO report(filename, contract, investor, period_start, period_end)
            VALUES (?, ?, ?, ?, ?)
        """, (filename, contract, investor, period_start, period_end))
        cur.execute("SELECT id FROM report WHERE filename=?", (filename,))
        report_id = cur.fetchone()['id']

    # Pre-scan sections using column B (index 1)
    col_b = df.iloc[:, 1].astype(str).str.strip().tolist() if max_col >= 2 else []
    sections = _find_sections_pd(col_b)

    # ── Parse cash flow ──────────────────────────────────────
    _parse_vtb_cash_flow(df, cur, report_id, sections, max_row, max_col)

    # ── Parse trades ─────────────────────────────────────────
    for sec_title in ['Заключенные в отчетном периоде сделки с ценными бумагами',
                      'Завершенные в отчетном периоде сделки с ценными бумагами',
                      'Незавершенные в отчетном периоде сделки с ценными бумагами']:
        _parse_vtb_trade_section(df, cur, report_id, sec_title, sections, max_row, max_col)

    # ── Parse REPO ───────────────────────────────────────────
    for sec_title in ['Заключенные в отчетном периоде сделки по переносу открытой позиции Клиента',
                      'Завершенные в отчетном периоде сделки по переносу открытой позиции Клиента']:
        _parse_vtb_repo_section(df, cur, report_id, sec_title, sections, max_row, max_col)

    conn.commit()
    conn.close()
    return report_id


def _find_sections_pd(col_b):
    section_keywords = [
        'Движение денежных средств',
        'Движение ценных бумаг',
        'Заключенные в отчетном периоде сделки с ценными бумагами',
        'Завершенные в отчетном периоде сделки с ценными бумагами',
        'Незавершенные в отчетном периоде сделки с ценными бумагами',
        'Заключенные в отчетном периоде сделки по переносу открытой позиции Клиента',
        'Завершенные в отчетном периоде сделки по переносу открытой позиции Клиента',
        'Отчёт об остатках денежных средств',
        'Отчёт об остатках ценных бумаг',
    ]
    found = {}
    for idx, val in enumerate(col_b):
        for kw in section_keywords:
            # Match section title at the beginning of the cell value (exact section header)
            if val.startswith(kw) or val == kw:
                # Only take the FIRST occurrence of each keyword
                if kw not in found:
                    found[kw] = idx
                break
    all_starts = sorted(found.items(), key=lambda x: x[1])
    result = {}
    for i, (kw, start) in enumerate(all_starts):
        end = len(col_b)
        if i + 1 < len(all_starts):
            end = all_starts[i + 1][1]
        result[kw] = (start, end)
    return result


def _find_section(sections, section_title):
    for kw, (start, end) in sections.items():
        if kw.startswith(section_title) or section_title in kw:
            return start, end
    return None, None


def _parse_vtb_cash_flow(df, cur, report_id, sections, max_row, max_col):
    start, end = _find_section(sections, 'Движение денежных средств')
    if start is None:
        return

    for r in range(start + 1, min(end, max_row)):
        date_val = df.iloc[r, 1]
        amount_val = df.iloc[r, 2]
        currency = df.iloc[r, 6] if max_col >= 7 else None
        desc = df.iloc[r, 9] if max_col >= 10 else None
        desc2 = df.iloc[r, 15] if max_col >= 16 else None

        if pd.isna(date_val) or pd.isna(amount_val):
            continue

        date_str = _fmt_date(date_val)
        if not date_str:
            continue

        amount = parse_float(amount_val)
        currency_str = str(currency or 'RUR').strip()
        desc_str = str(desc or '').strip()
        desc2_str = str(desc2 or '').strip()
        full_desc = desc_str
        if desc2_str and desc2_str != 'nan':
            full_desc = f'{desc_str} — {desc2_str}' if desc_str else desc2_str

        if not full_desc:
            continue

        credit = amount if amount > 0 else 0
        debit = abs(amount) if amount < 0 else 0

        cur.execute("""
            INSERT INTO cash_flow(report_id, date, description, currency, credit, debit, source)
            VALUES (?,?,?,?,?,?,?)
        """, (report_id, date_str, full_desc[:200], currency_str, credit, debit, 'vtb'))


def _parse_vtb_trade_section(df, cur, report_id, section_title, sections, max_row, max_col):
    start, end = _find_section(sections, section_title)
    if start is None:
        return

    data_start = start + 2
    if data_start >= min(end, max_row):
        return

    for r in range(data_start, min(end, max_row)):
        sec_info = df.iloc[r, 1]
        date_val = df.iloc[r, 2]
        side = df.iloc[r, 5] if max_col >= 6 else None
        qty = df.iloc[r, 7] if max_col >= 8 else None
        price = df.iloc[r, 9] if max_col >= 10 else None
        settle_currency = df.iloc[r, 11] if max_col >= 12 else None
        amount = df.iloc[r, 12] if max_col >= 13 else None
        nkd = df.iloc[r, 14] if max_col >= 15 else None
        broker_fee = df.iloc[r, 15] if max_col >= 16 else None
        exchange_fee = df.iloc[r, 17] if max_col >= 18 else None
        settle_date = df.iloc[r, 18] if max_col >= 19 else None
        deal_number = df.iloc[r, 25] if max_col >= 26 else None
        deal_number2 = df.iloc[r, 28] if max_col >= 29 else None
        venue = df.iloc[r, 38] if max_col >= 39 else None
        comment = df.iloc[r, 43] if max_col >= 44 else None

        if pd.isna(sec_info) or pd.isna(date_val):
            continue

        side_str = str(side or '').strip()
        if side_str not in ('Покупка', 'Продажа'):
            continue

        sec_name, sec_code, isin = _parse_sec_info(sec_info)
        qty_int = parse_int(qty)
        price_f = parse_float(price)
        amount_f = parse_float(amount)
        nkd_f = parse_float(nkd)
        broker_fee_f = parse_float(broker_fee)
        exchange_fee_f = parse_float(exchange_fee)

        trade_date_str = _fmt_date(date_val)
        trade_time_str = _fmt_time(date_val)
        settle_date_str = _fmt_date(settle_date) or trade_date_str
        currency_str = str(settle_currency or '').strip() or 'RUR'
        deal_num_str = str(deal_number or deal_number2 or '').strip()
        comment_str = str(comment or '').strip()
        venue_str = str(venue or '').strip()

        if not trade_date_str or not sec_name:
            continue

        cur.execute("""
            INSERT OR IGNORE INTO trade(report_id, trade_date, settle_date, trade_time,
                security_name, security_code, currency, side, quantity, price,
                amount, nkd, broker_fee, exchange_fee, deal_number, comment, status, source)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (report_id, trade_date_str, settle_date_str, trade_time_str,
              sec_name, sec_code, currency_str, side_str, qty_int, price_f,
              amount_f, nkd_f, broker_fee_f, exchange_fee_f, deal_num_str,
              comment_str[:200], venue_str[:200], 'vtb'))


def _parse_vtb_repo_section(df, cur, report_id, section_title, sections, max_row, max_col):
    start, end = _find_section(sections, section_title)
    if start is None:
        return

    data_start = start + 2
    if data_start >= min(end, max_row):
        return

    for r in range(data_start, min(end, max_row)):
        trade_type = df.iloc[r, 1]
        date_val = df.iloc[r, 2]
        sec_info = df.iloc[r, 5] if max_col >= 6 else None
        side = df.iloc[r, 9] if max_col >= 10 else None
        qty = df.iloc[r, 11] if max_col >= 12 else None
        price = df.iloc[r, 12] if max_col >= 13 else None
        price_currency = df.iloc[r, 14] if max_col >= 15 else None
        settle_currency = df.iloc[r, 15] if max_col >= 16 else None
        amount = df.iloc[r, 17] if max_col >= 18 else None
        fee = df.iloc[r, 18] if max_col >= 19 else None
        deal_number = df.iloc[r, 19] if max_col >= 20 else None
        delivery_date = df.iloc[r, 27] if max_col >= 28 else None

        if pd.isna(trade_type) or pd.isna(date_val):
            continue

        type_str = str(trade_type or '').strip()
        side_str = str(side or '').strip()
        if side_str not in ('Покупка', 'Продажа'):
            continue

        sec_name, sec_code, isin = _parse_sec_info(sec_info)
        if not sec_name and 'CNYRUB' in type_str:
            sec_name = 'CNYRUB'
            sec_code = 'CNYRUB'

        qty_int = parse_int(qty)
        price_f = parse_float(price)
        amount_f = parse_float(amount)
        fee_f = parse_float(fee)

        trade_date_str = _fmt_date(date_val)
        trade_time_str = _fmt_time(date_val)
        currency_str = str(settle_currency or price_currency or 'RUR').strip()
        deal_num_str = str(deal_number or '').strip()
        delivery_str = _fmt_date(delivery_date)

        if not trade_date_str:
            continue

        is_part1 = '1ч' in type_str
        is_part2 = '2ч' in type_str

        if is_part1:
            cur.execute("""
                INSERT OR IGNORE INTO repo(report_id, trade_date, trade_time, security_name,
                    security_code, currency, side, quantity, price_part1, nkd_part1,
                    amount_part1, date_part1, repo_rate, repo_interest, price_part2,
                    nkd_part2, amount_part2, date_part2, broker_fee, exchange_fee,
                    deal_number, status, source)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (report_id, trade_date_str, trade_time_str, sec_name, sec_code,
                  currency_str, side_str, qty_int, price_f, 0,
                  amount_f, delivery_str, 0, 0, 0,
                  0, 0, '', fee_f, 0,
                  deal_num_str, type_str[:200], 'vtb'))
        else:
            base_deal_part1 = ''
            if deal_num_str.endswith('-3'):
                base_deal_part1 = deal_num_str[:-2] + '-1'

            updated = False
            if base_deal_part1:
                cur.execute("""
                    UPDATE repo SET
                        price_part2=?, amount_part2=?, date_part2=?,
                        broker_fee=broker_fee+?
                    WHERE deal_number=? AND source='vtb'
                      AND (date_part2 IS NULL OR date_part2='')
                """, (price_f, amount_f, delivery_str, fee_f, base_deal_part1))
                if cur.rowcount > 0:
                    updated = True

            if not updated:
                cur.execute("""
                    INSERT OR IGNORE INTO repo(report_id, trade_date, trade_time, security_name,
                        security_code, currency, side, quantity, price_part1, nkd_part1,
                        amount_part1, date_part1, repo_rate, repo_interest, price_part2,
                        nkd_part2, amount_part2, date_part2, broker_fee, exchange_fee,
                        deal_number, status, source)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, (report_id, trade_date_str, trade_time_str, sec_name, sec_code,
                      currency_str, side_str, qty_int, 0, 0,
                      0, '', 0, 0, price_f,
                      0, amount_f, delivery_str, fee_f, 0,
                      deal_num_str, type_str[:200], 'vtb'))
