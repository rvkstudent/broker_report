"""Parse my_trades.xlsx — custom export of all trades across brokers and periods.

Формат файла (создан вручную, не стандартный отчёт брокера):
  Row 0: Header
  Col 0: Инструмент (может быть "Name, RegNumber, ISIN" или просто "Name")
  Col 1: Гос. рег. номер
  Col 2: Тикер
  Col 3: Номер сделки
  Col 4: DateTime
  Col 5: Кол-во
  Col 6: Направление (buy/sell)
  Col 7: Валюта цены
  Col 8: Цена сделки (% для облигаций)
  Col 9: Валюта расчётов
  Col 10: Объём сделки
  Col 11: НКД
  Col 12: Комиссия брокера
"""

import re
import os
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
    """Convert various date formats to DD.MM.YYYY."""
    if val is None or (isinstance(val, float) and pd is not None and pd.isna(val)):
        return ''
    if isinstance(val, datetime):
        return val.strftime('%d.%m.%Y')
    if hasattr(val, 'strftime'):
        return val.strftime('%d.%m.%Y')
    # Pandas Timestamp
    if hasattr(val, 'to_pydatetime'):
        return val.to_pydatetime().strftime('%d.%m.%Y')
    s = str(val).strip()
    # Already DD.MM.YYYY
    if len(s) >= 10 and s[2] == '.' and s[5] == '.':
        return s[:10]
    # ISO YYYY-MM-DD
    m = re.match(r'(\d{4})-(\d{2})-(\d{2})', s)
    if m:
        return f'{m.group(3)}.{m.group(2)}.{m.group(1)}'
    # DD.MM.YYYY HH:MM:SS
    m = re.match(r'(\d{2})\.(\d{2})\.(\d{4})', s)
    if m:
        return f'{m.group(1)}.{m.group(2)}.{m.group(3)}'
    # Excel serial date number (float as string like "44562")
    try:
        serial = float(s.split()[0]) if ' ' in s else float(s)
        if 40000 < serial < 60000:  # plausible Excel date serial
            from datetime import timedelta
            epoch = datetime(1899, 12, 30)
            d = epoch + timedelta(days=int(serial))
            return d.strftime('%d.%m.%Y')
    except (ValueError, TypeError, OverflowError):
        pass
    return s[:10]


def _fmt_time(val):
    """Extract time HH:MM:SS from datetime or string."""
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


def _parse_instrument(inst_str):
    """Parse instrument string into (name, ticker, isin).

    Formats:
      - "ОФЗ 26219, 26219RMFS, RU000A0JWM07"  (name, ticker, isin)
      - "Система ао" (name only)
      - "ВТБ ао, 10401000B, RU000A0JP5V6" (name, reg_number, isin)
    """
    if not inst_str or (isinstance(inst_str, float) and pd is not None and pd.isna(inst_str)):
        return '', '', ''
    s = str(inst_str).strip()
    parts = [p.strip() for p in s.split(',')]
    name = parts[0] if len(parts) > 0 else ''
    isin = ''
    ticker = ''
    for p in parts:
        p = p.strip()
        # ISIN: 12 chars, starts with 2 letters
        if re.match(r'^[A-Za-z]{2}[A-Za-z0-9]{10}$', p):
            isin = p
        # Ticker: all-caps 3-6 chars (like VTBR, MTSS, AFKS, TRMK)
        # But reg numbers look like "1-05-01669-A" or "10401000B"
        # Tickers from column 2 are more reliable
    return name, ticker, isin


def parse_mytrades(filepath):
    """Parse my_trades.xlsx and persist to DB. Returns report_id."""
    if pd is None:
        raise ImportError('pandas is required. Install with: pip install pandas openpyxl')

    init_db()

    df = pd.read_excel(filepath, header=None)
    max_row = df.shape[0]

    conn = get_connection()
    try:
        cur = conn.cursor()

        filename = os.path.basename(filepath)

        # Determine date range
        min_date = ''
        max_date = ''
        for r in range(1, max_row):
            dt_val = df.iloc[r, 4]
            d = _fmt_date(dt_val)
            if d:
                if not min_date or d < min_date:
                    min_date = d
                if not max_date or d > max_date:
                    max_date = d

        # Upsert report
        cur.execute("SELECT id FROM report WHERE filename=?", (filename,))
        existing = cur.fetchone()
        if existing:
            report_id = existing['id']
            # Clear old data (0 trades from failed previous parse)
            for tbl in ('trade', 'repo', 'cash_flow', 'portfolio', 'financial_result'):
                cur.execute(f"DELETE FROM {tbl} WHERE report_id=?", (report_id,))
            cur.execute("""
                UPDATE report SET contract='', investor='',
                       period_start=?, period_end=?
                WHERE id=?
            """, (min_date, max_date, report_id))
        else:
            cur.execute("""
                INSERT INTO report(filename, contract, investor, period_start, period_end)
                VALUES (?, '', '', ?, ?)
            """, (filename, min_date, max_date))
            cur.execute("SELECT id FROM report WHERE filename=?", (filename,))
            report_id = cur.fetchone()['id']

        # Parse trades
        count = 0
        for r in range(1, max_row):
            inst_val = df.iloc[r, 0]
            dt_val = df.iloc[r, 4]
            qty_val = df.iloc[r, 5]
            side_val = df.iloc[r, 6]
            price_val = df.iloc[r, 8]
            amount_val = df.iloc[r, 10]
            nkd_val = df.iloc[r, 11] if df.shape[1] > 11 else None
            fee_val = df.iloc[r, 12] if df.shape[1] > 12 else None
            ticker_val = df.iloc[r, 2] if df.shape[1] > 2 else None
            deal_val = df.iloc[r, 3] if df.shape[1] > 3 else None

            if pd.isna(inst_val) or pd.isna(dt_val) or pd.isna(qty_val):
                continue

            side_str = str(side_val or '').strip().lower()
            if side_str not in ('buy', 'sell', 'покупка', 'продажа'):
                continue

            # Map side
            if side_str in ('buy', 'покупка'):
                side = 'Покупка'
            else:
                side = 'Продажа'

            # Parse instrument
            inst_name, _, isin = _parse_instrument(inst_val)
            ticker = str(ticker_val or '').strip() if not pd.isna(ticker_val) else ''
            sec_code = ticker or isin or inst_name

            qty_int = parse_int(qty_val)
            price_f = parse_float(price_val)
            amount_f = parse_float(amount_val)
            nkd_f = parse_float(nkd_val)
            fee_f = parse_float(fee_val)

            trade_date = _fmt_date(dt_val)
            trade_time = _fmt_time(dt_val)
            deal_num = str(deal_val or '').strip() if not pd.isna(deal_val) else ''

            if not trade_date or qty_int <= 0:
                continue

            cur.execute("""
                INSERT OR IGNORE INTO trade
                    (report_id, trade_date, settle_date, trade_time,
                     security_name, security_code, currency, side, quantity, price,
                     amount, nkd, broker_fee, exchange_fee, deal_number, comment, status, source)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                report_id, trade_date, trade_date, trade_time,
                inst_name, sec_code, 'RUB', side, qty_int, price_f,
                amount_f, nkd_f, fee_f, 0, deal_num, '', 'executed', 'vtb'
            ))
            count += 1

        conn.commit()
        print(f'[mytrades] Загружено {count} сделок из {filename} (report_id={report_id})')
        return report_id
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
