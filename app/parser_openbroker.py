"""Parse Open Broker / BM-Bank (formerly Otkritie) XLSX reports.

Формат файла (отчёт брокера «Открытие» / АО «БМ-Банк»):
  Row 0: 'Отчет Филиал "Открытие" АО "БМ-Банк"'
  Rows 1-6: метаданные (клиент, договор, портфель)
  Row 112: "Заключенные в отчетном периоде сделки купли/продажи с ценными бумагами"
  Row 113: заголовки колонок
  Row 114+: данные
  Row 1063: "Завершенные в отчетном периоде сделки купли/продажи ценных бумаг"
  ...

Колонки секций (индексы могут различаться между файлами):
  C0:  Инструмент
  C7:  Гос. рег. номер
  C13: Номер заявки
  C20: Номер сделки
  C29: Дата заключения
  C35: Время заключения
  C46: Плановая дата исполнения
  C55: Куплено, шт.
  C63: Продано, шт.
  C77: Валюта цены
  C87: Цена сделки (% для облигаций)
  C100: Валюта расчётов
  C109: Объём сделки
  C121: НКД
  C129: Комиссия брокера
  C140: Валюта комиссии
  C149: Место заключения
  C163: Комментарий
  C174: Контрагент

Источник (source) = 'vtb' (по указанию пользователя — Открытие = ВТБ в Firebase).
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
    s = s.replace('\xa0', '').replace('&nbsp;', '').replace(' ', '').replace(',', '.')
    s = s.lstrip('+')
    try:
        return float(s)
    except ValueError:
        return 0.0


def parse_int(s):
    if s is None:
        return 0
    if isinstance(s, float):
        if pd is not None and pd.isna(s):
            return 0
        s = str(s)
    if isinstance(s, (int,)):
        return s
    s = str(s).strip().replace('\xa0', '').replace('&nbsp;', '').replace(' ', '')
    try:
        return int(float(s))
    except (ValueError, TypeError):
        return 0


def _fmt_date(val):
    if val is None or (isinstance(val, float) and pd is not None and pd.isna(val)):
        return ''
    if isinstance(val, datetime):
        return val.strftime('%d.%m.%Y')
    if hasattr(val, 'strftime'):
        return val.strftime('%d.%m.%Y')
    if hasattr(val, 'to_pydatetime'):
        return val.to_pydatetime().strftime('%d.%m.%Y')
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


def _find_section(headers, section_keywords, col_b):
    """Найти секцию в колонке B, вернуть (start_row, dict_of_column_indices).

    Сначала ищет заголовок секции (exact match), затем строку с заголовками колонок,
    парсит заголовки колонок для динамического определения индексов.
    """
    # Ищем заголовок секции
    section_start = None
    for kw in section_keywords:
        for i, v in enumerate(col_b):
            if v.startswith(kw) or v == kw:
                section_start = i
                break
        if section_start is not None:
            break

    if section_start is None:
        return None, None, None

    # Следующая строка — заголовки колонок
    header_row = section_start + 1
    if header_row >= len(col_b):
        return None, None, None

    # Парсим заголовки колонок
    col_map = {}
    if headers is not None:
        # Используем шаблон headers для поиска ключевых слов
        for target_col, keywords in headers.items():
            for c in range(len(col_b)):  # search all columns
                cell_val = str(col_b[c]) if c == 1 else ''  # we need the actual df value
            # Actually we need to read from the DataFrame
            pass
    
    return section_start, header_row, None


def _detect_columns(df, header_row):
    """Определить индексы колонок по строке заголовка.

    Returns: dict с ключами: instrument, regnum, deal_number, date, time,
             buy_qty, sell_qty, price, amount, nkd, fee, currency
    """
    col_map = {}
    targets = {
        'instrument': ['Инструмент'],
        'regnum': ['Гос. рег.', 'рег. номер', 'Гос.рег.'],
        'deal_number': ['Номер сделки', '№ сделки'],
        'date': ['Дата заключения', 'Дата'],
        'time': ['Время заключения', 'Время'],
        'buy_qty': ['Куплено'],
        'sell_qty': ['Продано'],
        'price': ['Цена сделки', 'Цена'],
        'amount': ['Объем сделки', 'Объём сделки', 'Сумма сделки'],
        'nkd': ['НКД'],
        'fee': ['Комиссия Брокера', 'Комиссия'],
        'currency': ['Валюта расчётов', 'Валюта расчетов', 'Валюта'],
    }

    for key, keywords in targets.items():
        for c in range(df.shape[1]):
            cell = str(df.iloc[header_row, c]).strip().lower().replace('\n', ' ').replace('\r', '')
            for kw in keywords:
                if kw.lower() in cell:
                    col_map[key] = c
                    break
            if key in col_map:
                break

    return col_map


def _parse_trade_section(df, cur, report_id, section_start, header_row, section_end, col_map):
    """Распарсить секцию сделок."""
    count = 0
    data_start = header_row + 1

    for r in range(data_start, min(section_end, df.shape[0])):
        # Определяем инструмент: заголовок может быть в C0, но данные в C1 (merged cells)
        inst_col = col_map.get('instrument', 0)
        inst_val = df.iloc[r, inst_col]
        if pd.isna(inst_val) or str(inst_val).strip() == '':
            # Пробуем следующую колонку (данные часто со смещением от заголовка)
            inst_val = df.iloc[r, inst_col + 1] if inst_col + 1 < df.shape[1] else None
        if pd.isna(inst_val):
            continue
        inst_str = str(inst_val).strip()

        # Пропускаем итоговые строки
        if inst_str.startswith('Итого'):
            continue

        # Номер сделки
        deal_col = col_map.get('deal_number', 20)
        deal_val = df.iloc[r, deal_col] if deal_col < df.shape[1] else None
        if pd.isna(deal_val):
            continue
        deal_num = str(deal_val).strip()

        # Дата
        date_col = col_map.get('date', 29)
        date_val = df.iloc[r, date_col] if date_col < df.shape[1] else None
        trade_date = _fmt_date(date_val)
        if not trade_date:
            continue

        # Время
        time_col = col_map.get('time', 35)
        time_val = df.iloc[r, time_col] if time_col < df.shape[1] else None
        trade_time = _fmt_time(time_val)

        # Куплено / Продано
        buy_col = col_map.get('buy_qty', 55)
        sell_col = col_map.get('sell_qty', 63)
        buy_qty = parse_int(df.iloc[r, buy_col]) if buy_col < df.shape[1] else 0
        sell_qty = parse_int(df.iloc[r, sell_col]) if sell_col < df.shape[1] else 0

        if buy_qty == 0 and sell_qty == 0:
            continue

        side = 'Покупка' if buy_qty > 0 else 'Продажа'
        qty = buy_qty or sell_qty

        # Цена
        price_col = col_map.get('price', 87)
        price = parse_float(df.iloc[r, price_col]) if price_col < df.shape[1] else 0

        # Сумма
        amt_col = col_map.get('amount', 109)
        amount = parse_float(df.iloc[r, amt_col]) if amt_col < df.shape[1] else 0

        # НКД
        nkd_col = col_map.get('nkd', 121)
        nkd = parse_float(df.iloc[r, nkd_col]) if nkd_col < df.shape[1] else 0

        # Комиссия
        fee_col = col_map.get('fee', 129)
        fee = parse_float(df.iloc[r, fee_col]) if fee_col < df.shape[1] else 0

        # Определяем код инструмента
        reg_col = col_map.get('regnum', 7)
        reg_num = str(df.iloc[r, reg_col]).strip() if reg_col < df.shape[1] and not pd.isna(df.iloc[r, reg_col]) else ''
        sec_name = inst_str
        sec_code = reg_num or sec_name

        # Используем маппинг тикеров из VTB парсера
        from app.parser_vtb import _VTB_TICKER_MAP, _infer_sec_code
        for key, ticker in _VTB_TICKER_MAP.items():
            if key in sec_name or sec_name in key:
                sec_code = ticker
                break

        if not sec_code:
            sec_code = reg_num or sec_name

        cur.execute("""
            INSERT OR IGNORE INTO trade
                (report_id, trade_date, settle_date, trade_time,
                 security_name, security_code, currency, side, quantity, price,
                 amount, nkd, broker_fee, exchange_fee, deal_number, comment, status, source)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            report_id, trade_date, trade_date, trade_time,
            sec_name, sec_code, 'RUB', side, qty, price,
            amount, nkd, fee, 0, deal_num, '', 'executed', 'vtb'
        ))
        count += 1

    return count


def parse_openbroker_report(filepath):
    """Parse Open Broker / BM-Bank XLSX report and persist to DB. Returns report_id."""
    if pd is None:
        raise ImportError('pandas is required. Install with: pip install pandas openpyxl')

    init_db()

    df = pd.read_excel(filepath, header=None)
    max_row = df.shape[0]

    # Колонка A (индекс 0) — ищем секции (в ЕБС отчётах текст секций в колонке A)
    col_a = [str(v).strip() if not isinstance(v, str) else v.strip() for v in df.iloc[:, 0].tolist()]

    conn = get_connection()
    try:
        cur = conn.cursor()

        filename = os.path.basename(filepath)

        # Извлекаем период из метаданных
        period_start = ''
        period_end = ''
        for r in range(min(10, max_row)):
            txt = str(df.iloc[r, 0]).strip()
            m = re.search(r'за период\s+(\S+)\s+[-–]\s+(\S+)', txt)
            if m:
                period_start = _fmt_date(m.group(1))
                period_end = _fmt_date(m.group(2))
                break

        # Upsert report
        cur.execute("SELECT id FROM report WHERE filename=?", (filename,))
        existing = cur.fetchone()
        if existing:
            report_id = existing['id']
            for tbl in ('trade', 'repo', 'cash_flow', 'portfolio', 'financial_result'):
                cur.execute(f"DELETE FROM {tbl} WHERE report_id=?", (report_id,))
            cur.execute("""UPDATE report SET period_start=?, period_end=? WHERE id=?""",
                       (period_start, period_end, report_id))
        else:
            cur.execute("""INSERT INTO report(filename, period_start, period_end) VALUES (?,?,?)""",
                       (filename, period_start, period_end))
            cur.execute("SELECT id FROM report WHERE filename=?", (filename,))
            report_id = cur.fetchone()['id']

        # Определяем секции
        trade_sections = [
            'Заключенные в отчетном периоде сделки купли/продажи с ценными бумагами',
            'Завершенные в отчетном периоде сделки купли/продажи ценных бумаг',
        ]

        total_count = 0
        for section_kw in trade_sections:
            # Находим начало секции
            section_start = None
            for i, v in enumerate(col_a):
                if v.startswith(section_kw) or v == section_kw:
                    section_start = i
                    break

            if section_start is None:
                continue

            header_row = section_start + 1

            # Определяем конец секции
            section_end = max_row
            for j in range(section_start + 2, min(section_start + 2000, max_row)):
                v = str(df.iloc[j, 0]).strip() if not pd.isna(df.iloc[j, 0]) else ''
                for next_kw in ['Заключенные в отчетном периоде сделки',
                                'Завершенные в отчетном периоде сделки',
                                'Срочные сделки',
                                'Движение средств',
                                'Движение ценных бумаг',
                                'Отчёт об остатках']:
                    if v.startswith(next_kw) or v == next_kw:
                        section_end = j
                        break
                if section_end != max_row:
                    break

            # Определяем колонки
            col_map = _detect_columns(df, header_row)
            if 'instrument' not in col_map or 'date' not in col_map:
                print(f'  [open] {section_kw[:30]}: не удалось определить колонки')
                continue

            # Парсим
            cnt = _parse_trade_section(df, cur, report_id, section_start, header_row, section_end, col_map)
            total_count += cnt
            print(f'  [open] {section_kw[:40]}: {cnt} сделок')

        conn.commit()
        print(f'[open] Загружено {total_count} сделок из {filename} (report_id={report_id})')
        return report_id
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
