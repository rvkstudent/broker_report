"""Parse VTB tax reports (налоговые отчёты) from reports/nalog/.

Формат: отчёт о расчёте налогооблагаемой базы (ВТБ).
Структура:
  Row 0: "Отчет о расчете налогооблагаемой базы..."
  Rows 1-6: метаданные (брокер, клиент, период)
  Row 68+: секции "Финансовый результат" по каждому инструменту
    C1: ID записи (сумма дохода)
    C3: Дата продажи
    C6: № сделки
    C11: Код ФНС
    C14: Цена продажи
    C18: Кол-во
    C22: Сумма продажи
    C29: Валюта
    ... последняя строка: C1=Наименование: <инструмент>
"""

import re
import os
import sqlite3
from datetime import datetime
from app.db import get_connection

try:
    import pandas as pd
except ImportError:
    pd = None


def parse_float(s):
    if s is None:
        return 0.0
    if isinstance(s, (int, float)):
        return float(s)
    s = str(s).strip().replace('\xa0', '').replace(' ', '').replace(',', '.')
    s = s.lstrip('+')
    try:
        return float(s)
    except ValueError:
        return 0.0


def _fmt_date(val):
    if val is None or (pd and pd.isna(val)):
        return ''
    if isinstance(val, datetime):
        return val.strftime('%d.%m.%Y')
    if hasattr(val, 'strftime'):
        return val.strftime('%d.%m.%Y')
    s = str(val).strip()[:10]
    m = re.match(r'(\d{4})-(\d{2})-(\d{2})', s)
    if m:
        return f'{m.group(3)}.{m.group(2)}.{m.group(1)}'
    return s


def _fmt_year(val):
    """Извлечь год из значения."""
    s = str(val).strip()
    m = re.search(r'(\d{4})', s)
    return m.group(1) if m else ''


def init_nalog_table():
    """Создать таблицу для налоговых данных."""
    conn = get_connection()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS nalog (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            year            TEXT NOT NULL,
            instrument_name TEXT,
            instrument_code TEXT,
            side            TEXT,
            deal_date       TEXT,
            deal_number     TEXT,
            fnc_code        TEXT,
            price           REAL,
            quantity        INTEGER,
            amount          REAL,
            currency        TEXT DEFAULT 'RUB',
            income          REAL DEFAULT 0,
            expense         REAL DEFAULT 0,
            source_file     TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_nalog_year ON nalog(year)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_nalog_code ON nalog(instrument_code)")
    
    conn.execute("""
        CREATE TABLE IF NOT EXISTS nalog_tax_summary (
            year                TEXT PRIMARY KEY,
            broker_income       REAL DEFAULT 0,
            broker_taxable      REAL DEFAULT 0,
            broker_tax_calc     REAL DEFAULT 0,
            broker_tax_paid     REAL DEFAULT 0,
            broker_tax_due      REAL DEFAULT 0,
            depositary_income   REAL DEFAULT 0,
            depositary_taxable  REAL DEFAULT 0,
            total_income        REAL DEFAULT 0,
            total_taxable       REAL DEFAULT 0,
            dividend_income     REAL DEFAULT 0,
            broker_result       REAL DEFAULT 0,
            source_file         TEXT
        )
    """)
    conn.commit()
    # Добавляем колонки для существующих БД
    for col in ('dividend_income', 'broker_result'):
        try:
            conn.execute(f"ALTER TABLE nalog_tax_summary ADD COLUMN {col} REAL DEFAULT 0")
            conn.commit()
        except Exception:
            pass
    conn.close()


def _parse_tax_summary(df, year, filename, cur):
    """Извлечь сводку по налогам из шапки отчёта (Информация по Брокеру / Депозитарию)."""
    max_row = min(50, df.shape[0])
    
    # Суммируем налоги по всем ставкам (13%, 15% и т.д.)
    broker_tax_calc = 0.0
    broker_tax_paid = 0.0
    broker_tax_due = 0.0
    broker_income = 0.0
    broker_taxable = 0.0
    depositary_income = 0.0
    depositary_taxable = 0.0
    total_income = 0.0
    total_taxable = 0.0
    
    in_broker = False
    in_depositary = False
    val_col = 33  # колонка со значениями
    
    for r in range(max_row):
        cell1 = str(df.iloc[r, 1]).strip() if not pd.isna(df.iloc[r, 1]) else ''
        
        if 'Информация по Брокеру' in cell1:
            in_broker = True
            in_depositary = False
            continue
        if 'Сводная информация Депозитария' in cell1:
            in_broker = False
            in_depositary = True
            continue
        if 'Расчет финансового результата' in cell1:
            in_broker = False
            in_depositary = False
            continue
        
        val = parse_float(df.iloc[r, val_col]) if not pd.isna(df.iloc[r, val_col]) else None
        
        if in_broker:
            if 'Налогооблагаемый доход' in cell1 and val is not None:
                broker_taxable = val
            elif 'Начисленный налог' in cell1 and val is not None:
                broker_tax_calc += val
            elif 'Уплаченный налог' in cell1 and val is not None:
                broker_tax_paid += val
            elif 'Налог к уплате' in cell1 and val is not None:
                broker_tax_due += val
            elif 'Общая сумма дохода' in cell1 and val is not None:
                broker_income = val
                
        elif in_depositary:
            if 'Налогооблагаемый доход' in cell1 and val is not None:
                depositary_taxable = val
            elif 'Общая сумма дохода' in cell1 and val is not None:
                depositary_income = val
    
    # Итоговая информация — ищем ИТОГО общая/налогообл.
    for r in range(max_row):
        cell1 = str(df.iloc[r, 1]).strip() if not pd.isna(df.iloc[r, 1]) else ''
        val = parse_float(df.iloc[r, 26]) if not pd.isna(df.iloc[r, 26]) else None
        
        if 'ИТОГО общая сумма дохода' in cell1 and val is not None:
            total_income = val
        elif 'ИТОГО налогооблагаемый доход' in cell1 and val is not None:
            total_taxable = val
    
    # Вычисляем финансовый результат из сводных строк (РЕПО + ЦБ)
    # Строки 10-11: col12=Доходы, col19=Расходы, col28=Транзактные, col37=Нетранзактные
    broker_result = 0.0
    for r in range(max_row):
        cell1 = str(df.iloc[r, 1]).strip() if not pd.isna(df.iloc[r, 1]) else ''
        if cell1.startswith('ИТОГО ПО ОПЕРАЦИЯМ РЕПО') or cell1.startswith('ИТОГО ПО ЦЕННЫМ БУМАГАМ'):
            inc = parse_float(df.iloc[r, 12]) if not pd.isna(df.iloc[r, 12]) else 0
            exp = parse_float(df.iloc[r, 19]) if not pd.isna(df.iloc[r, 19]) else 0
            tx = parse_float(df.iloc[r, 28]) if not pd.isna(df.iloc[r, 28]) else 0
            ntx = parse_float(df.iloc[r, 37]) if not pd.isna(df.iloc[r, 37]) else 0
            broker_result += inc - exp - tx - ntx

    cur.execute("""
        INSERT OR REPLACE INTO nalog_tax_summary
            (year, broker_income, broker_taxable, broker_tax_calc, broker_tax_paid, broker_tax_due,
             depositary_income, depositary_taxable, total_income, total_taxable, broker_result, source_file)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (year, broker_income, broker_taxable, broker_tax_calc, broker_tax_paid, broker_tax_due,
          depositary_income, depositary_taxable, total_income, total_taxable, broker_result, filename))


def _parse_dividends(df, year, filename, cur):
    """Извлечь дивиденды/купоны из секции 'Отчет о выплатах по ценным бумагам'.

    Структура секции (проверено на отчётах ВТБ 2024-2026):
      Header: col1='№ п/п', col2='Депозитарий', col4='Эмитент', col6='Период выплаты',
              col8='Дата фиксации', col13='Вид цб', col16='Рег № / ISIN',
              col20='Кол-во цб', col24='Выплата на 1 цб', col30='Начислено', col32='НОБ (руб.)'
      Data:   col1=номер, col4=эмитент, col6=период, col13=обл./акции,
              col16=ISIN, col20=кол-во, col24=выплата, col30=начислено
      Total:  col1='Итого по RUB', col30=сумма
    """
    max_row = df.shape[0]
    in_payments_section = False
    dividend_total = 0.0

    for r in range(max_row):
        cell1 = str(df.iloc[r, 1]).strip() if not pd.isna(df.iloc[r, 1]) else ''

        # Начало секции выплат
        if 'Отчет о выплатах по ценным бумагам' in cell1:
            in_payments_section = True
            continue

        if not in_payments_section:
            continue

        # Конец секции — строка Итого по RUB
        if 'Итого по RUB' in cell1:
            val = parse_float(df.iloc[r, 30]) if not pd.isna(df.iloc[r, 30]) else 0
            if val:
                dividend_total = val
            break

        # Пропускаем заголовки
        if not cell1 or cell1 == '№ п/п':
            continue
        # Пропускаем строки счёт депо / банк
        if cell1.startswith('Счет депо') or cell1.startswith('Банк'):
            continue

        # Проверяем, что это строка данных (колонка 1 содержит номер)
        try:
            int(cell1)
        except ValueError:
            continue

        # Читаем данные о выплате
        issuer = str(df.iloc[r, 4]).strip() if not pd.isna(df.iloc[r, 4]) else ''
        period = str(df.iloc[r, 6]).strip() if not pd.isna(df.iloc[r, 6]) else ''
        sec_type = str(df.iloc[r, 13]).strip() if not pd.isna(df.iloc[r, 13]) else ''
        isin_full = str(df.iloc[r, 16]).strip() if not pd.isna(df.iloc[r, 16]) else ''
        isin = isin_full.split('/')[0].strip() if '/' in isin_full else isin_full
        accrued = parse_float(df.iloc[r, 30]) if not pd.isna(df.iloc[r, 30]) else 0

        # Определяем тип: coupon (обл.) или dividend (акции)
        income_type = 'coupon' if 'обл' in sec_type else 'dividend'
        instrument_name = f'{issuer} / {period}'

        if accrued == 0:
            continue

        # Сохраняем в таблицу nalog для отображения в детальной таблице
        cur.execute("""
            INSERT INTO nalog
                (year, instrument_name, instrument_code, side, amount, income, source_file)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (year, instrument_name, isin, income_type, accrued, accrued, filename))

    # Обновляем dividend_income в сводке
    cur.execute("""
        UPDATE nalog_tax_summary
        SET dividend_income = ?
        WHERE year = ?
    """, (dividend_total, year))

    if dividend_total:
        print(f'[nalog] {filename}: дивиденды/купоны={dividend_total:.2f}')


def parse_nalog_report(filepath):
    """Parse a VTB tax report XLSX and insert into nalog table."""
    if pd is None:
        raise ImportError('pandas is required')

    init_nalog_table()

    df = pd.read_excel(filepath, header=None)
    max_row = df.shape[0]
    filename = os.path.basename(filepath)

    # Извлекаем год
    year = ''
    for r in range(min(10, max_row)):
        for c in range(min(30, df.shape[1])):
            v = str(df.iloc[r, c])
            m = re.search(r'(\d{4})', v)
            if m and 2020 < int(m.group(1)) < 2030:
                year = m.group(1)
                break
        if year:
            break

    if not year:
        m = re.search(r'(\d{4})', filename)
        year = m.group(1) if m else '0000'

    conn = get_connection()
    cur = conn.cursor()

    # Удаляем старые данные за этот год
    cur.execute("DELETE FROM nalog WHERE year=? AND source_file=?", (year, filename))
    cur.execute("DELETE FROM nalog_tax_summary WHERE year=?", (year,))

    # Парсим сводку по налогам (шапка отчёта)
    _parse_tax_summary(df, year, filename, cur)

    # Парсим дивиденды/купоны из секции "Отчет о выплатах"
    _parse_dividends(df, year, filename, cur)

    # Парсим секции "Финансовый результат"
    # Ищем строки вида "C1=Наименование: ..." 
    current_name = ''
    current_code = ''

    for r in range(max_row):
        cell1 = str(df.iloc[r, 1]).strip() if not pd.isna(df.iloc[r, 1]) else ''

        if cell1.startswith('Наименование:'):
            parts = cell1.replace('Наименование:', '').strip().split(',')
            current_name = parts[0].strip() if parts else ''
            current_code = parts[1].strip() if len(parts) > 1 else ''
            continue

        # Пропускаем заголовки и не-строки с данными
        if not cell1 or cell1.startswith('Наименование') or cell1.startswith('Финансовый'):
            continue

        # Пытаемся прочитать сумму (должна быть число в C1)
        income = parse_float(df.iloc[r, 1])

        # Читаем остальные колонки
        deal_date = _fmt_date(df.iloc[r, 3]) if not pd.isna(df.iloc[r, 3]) else _fmt_date(df.iloc[r, 1])
        deal_number = str(df.iloc[r, 6]).strip() if not pd.isna(df.iloc[r, 6]) else ''
        price = parse_float(df.iloc[r, 14]) if not pd.isna(df.iloc[r, 14]) else 0
        qty = parse_float(df.iloc[r, 18]) if not pd.isna(df.iloc[r, 18]) else 0
        amount = parse_float(df.iloc[r, 22]) if not pd.isna(df.iloc[r, 22]) else 0
        currency = str(df.iloc[r, 29]).strip() if not pd.isna(df.iloc[r, 29]) else 'RUB'

        if not deal_date and amount == 0:
            continue

        # Определяем сторону: по C3
        side = str(df.iloc[r, 3]).strip() if not pd.isna(df.iloc[r, 3]) else ''
        # Если side содержит 'Продажа' или 'Покупка' — это заголовок секции
        if side in ('Продажа', 'Покупка', 'Дата продажи'):
            continue

        # Пропускаем строки без номера сделки (РЕПО и итоговые строки)
        if not deal_number:
            continue

        cur.execute("""
            INSERT INTO nalog 
                (year, instrument_name, instrument_code, deal_date, deal_number,
                 fnc_code, price, quantity, amount, currency, income, source_file)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            year, current_name, current_code, deal_date, deal_number,
            str(df.iloc[r, 11]).strip() if not pd.isna(df.iloc[r, 11]) else '',
            price, int(qty) if qty == int(qty) else qty, amount, currency,
            income, filename
        ))

    conn.commit()
    conn.close()

    # Сводка
    conn = get_connection()
    cnt = conn.execute("SELECT COUNT(*) FROM nalog WHERE year=? AND source_file=?", (year, filename)).fetchone()[0]
    total_income = conn.execute("SELECT COALESCE(SUM(income),0) FROM nalog WHERE year=? AND source_file=?", (year, filename)).fetchone()[0]
    conn.close()

    print(f'[nalog] {filename}: {cnt} записей, доход={total_income:.2f}')
    return year


def get_nalog_summary():
    """Get all summary data flat — instrument × year × metrics.
    
    Returns list of dicts, one per (instrument, year) combination.
    """
    conn = get_connection()
    rows = conn.execute("""
        SELECT n.instrument_name, n.instrument_code, n.year,
               COUNT(*) as deals,
               SUM(n.income) as total_income,
               SUM(n.amount) as total_amount,
               COUNT(CASE WHEN n.income > 0 THEN 1 END) as positive_deals,
               COUNT(CASE WHEN n.income < 0 THEN 1 END) as negative_deals
        FROM nalog n
        WHERE n.instrument_name IS NOT NULL AND n.instrument_name != ''
        GROUP BY n.instrument_code, n.instrument_name, n.year
        ORDER BY n.instrument_name
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_nalog_years():
    """Get list of available years."""
    conn = get_connection()
    rows = conn.execute("SELECT DISTINCT year FROM nalog ORDER BY year DESC").fetchall()
    conn.close()
    return sorted(set(r[0] for r in rows))


def get_nalog_instruments():
    """Get distinct instruments with tax data (for pivot rows)."""
    conn = get_connection()
    rows = conn.execute("""
        SELECT DISTINCT instrument_name, instrument_code
        FROM nalog
        WHERE instrument_name IS NOT NULL AND instrument_name != ''
        ORDER BY instrument_name
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_nalog_tax_summary_by_year():
    """Get tax summary per year from nalog_tax_summary table."""
    conn = get_connection()
    rows = conn.execute("""
        SELECT * FROM nalog_tax_summary ORDER BY year
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_nalog_group_summary():
    """Get totals grouped by ОФЗ / Прочие per year."""
    conn = get_connection()
    rows = conn.execute("""
        SELECT 
            CASE WHEN n.instrument_name LIKE 'ОФЗ%' THEN 'ОФЗ' ELSE 'Прочие' END as grp,
            n.year,
            COUNT(*) as deals,
            ROUND(SUM(n.income), 2) as total_income,
            ROUND(SUM(CASE WHEN n.income > 0 THEN n.income ELSE 0 END), 2) as total_profit,
            ROUND(SUM(CASE WHEN n.income < 0 THEN n.income ELSE 0 END), 2) as total_loss
        FROM nalog n
        WHERE n.instrument_name IS NOT NULL AND n.instrument_name != ''
        GROUP BY grp, n.year
        ORDER BY grp, n.year
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]
