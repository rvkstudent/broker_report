"""Снимки размера активов из брокерских отчётов.

Нужны для «Роста портфеля»: восстановление стоимости по годам напрямую из
отчётов надёжнее, чем пересчёт из сделок — в отчёте уже есть оценка активов
на дату (бумаги по рынку + деньги), а корпоративные действия (сплиты) учтены
брокером.

Возвращаем словарь: ``{'date': 'YYYY-MM-DD', 'assets': float, 'cash': float}``.
Поддерживаются XLSX ВТБ/Открытия и HTML Сбера.
"""

from __future__ import annotations

import os
import re

try:  # XLSX
    import pandas as pd
except ImportError:  # pragma: no cover
    pd = None

try:  # HTML
    from bs4 import BeautifulSoup
except ImportError:  # pragma: no cover
    BeautifulSoup = None


def _to_float(value) -> float:
    if value is None:
        return 0.0
    s = str(value).strip().replace('\xa0', '').replace(' ', '').replace('&nbsp;', '')
    if not s or s.lower() in ('nan', 'none', '-'):
        return 0.0
    s = s.replace(',', '.')
    try:
        return float(s)
    except ValueError:
        return 0.0


def _iso(day: str) -> str:
    d = (day or '').strip().rstrip(',')[:10]
    if len(d) == 10 and d[2] == '.' and d[5] == '.':
        return f'{d[6:10]}-{d[3:5]}-{d[0:2]}'
    return d


def _fmt_iso(value) -> str:
    """Дата из ячейки Excel (строка, datetime или Timestamp) → 'YYYY-MM-DD'."""
    if isinstance(value, str):
        return _iso(value)
    try:
        return value.strftime('%Y-%m-%d')
    except Exception:  # noqa: BLE001
        return ''


def _period(text: str) -> tuple[str, str]:
    m = re.search(r'за период с\s+(\S+)\s+по\s+(\S+)', text)
    if not m:
        return '', ''
    return _iso(m.group(1)), _iso(m.group(2))


def extract_xlsx(path: str) -> dict | None:
    """Снимок из XLSX: «Оценка активов» (ВТБ, Открытие/ЕБС) + остаток денег."""
    if pd is None:
        return None
    df = pd.read_excel(path, header=None)
    max_row, max_col = df.shape
    width = min(max_col, 200)          # у ЕБС значения уезжают далеко вправо

    title = ' '.join(str(df.iloc[r, c]) for r in range(min(5, max_row))
                     for c in range(min(10, max_col))
                     if not pd.isna(df.iloc[r, c]))
    title = title or (str(df.iloc[0, 3]) if max_col > 3 else '')
    start, end = _period(title)

    assets = 0.0
    cash = 0.0
    in_cash = False
    for r in range(min(max_row, 400)):
        row = [str(df.iloc[r, c]).strip() if not pd.isna(df.iloc[r, c]) else ''
               for c in range(width)]
        label = ' '.join(row[:3]).lower()
        if 'оценка активов' in label:
            # у ЕБС несколько таких строк (по портфелям) — берём максимум
            vals = [_to_float(v) for v in row[3:] if _to_float(v)]
            if vals:
                assets = max(assets, max(vals))
            continue
        if 'исходящий остаток денежных' in label or label.startswith('исходящий остаток (факт)'):
            in_cash = True
            continue
        if in_cash:
            if not label.strip() or 'оценка активов' in label:
                in_cash = False
                continue
            vals = [_to_float(v) for v in row[3:] if _to_float(v)]
            if vals:
                cash += vals[0]

    if not assets:
        return None
    return {'date': end, 'assets': round(assets, 2), 'cash': round(cash, 2)}


def extract_html(path: str) -> dict | None:
    """Снимок из HTML-отчёта Сбера: «Оценка активов» → строка «Итого»."""
    if BeautifulSoup is None:
        return None
    with open(path, 'r', encoding='utf-8', errors='ignore') as fh:
        soup = BeautifulSoup(fh.read(), 'lxml')

    text = soup.get_text(' ')
    text = re.sub(r'&nbsp;', ' ', text)
    text = re.sub(r'[ \t\u00a0]+', ' ', text)
    start, end = _period(text)

    num = r'(-?[\d ]+[.,]\d+)'
    assets = 0.0
    m = re.search(r'Оценка активов.*?Итого\s+' + num + r'\s+' + num + r'\s+[+\-]?' + num, text, re.S)
    if m:
        # «Итого <всего на начало> <всего на конец> <изменение>»
        assets = _to_float(m.group(2))
    if not assets:
        m = re.search(r'Оценка активов.*?Итого\s+' + num + r'\s+' + num, text, re.S)
        if m:
            assets = _to_float(m.group(2))

    cash = 0.0
    m = re.search(r'Исходящий остаток\s+' + num, text)
    if m:
        cash = _to_float(m.group(1))

    if not assets:
        return None
    return {'date': end, 'assets': round(assets, 2), 'cash': round(cash, 2)}


def extract_flows(path: str) -> list[dict]:
    """Вводы/выводы денег (внешние потоки) из отчёта: [{'date','amount'}]."""
    ext = os.path.splitext(path)[1].lower()
    try:
        if ext in ('.xlsx', '.xls'):
            return _flows_xlsx(path)
        if ext in ('.html', '.htm'):
            return _flows_html(path)
        if ext == '.pdf':
            return _flows_pdf(path)
    except Exception:  # noqa: BLE001
        return []
    return []


def _flows_pdf(path: str) -> list[dict]:
    """Потоки из PDF-отчёта (Ньютон/Газпромбанк): «<дата> Ввод/Вывод ДС <сумма>RUR»."""
    try:
        import pypdf
    except ImportError:
        return []
    reader = pypdf.PdfReader(path)
    text = '\n'.join((page.extract_text() or '') for page in reader.pages)
    out = []
    for m in re.finditer(r'(\d{2}\.\d{2}\.\d{4})\s+(Ввод ДС|Вывод ДС)\s+(-?[\d\s]+[.,]\d{2})', text):
        day = _iso(m.group(1))
        amount = _to_float(m.group(3))
        if not day or not amount:
            continue
        sign = 1 if m.group(2).startswith('Ввод') else -1
        out.append({'date': day, 'amount': sign * abs(amount),
                    'description': m.group(2)})
    return out


_DEPOSIT_KEYS = ('зачисление денежных средств', 'поставлены на торги средства',
                 'пополнение счета', 'пополнение счёта', 'зачисление д/с')
_WITHDRAW_KEYS = ('вывод денежных средств', 'вывод средств', 'списаны средства',
                 'списание д/с')


def _is_deposit(text: str) -> bool:
    if any(k in text for k in _DEPOSIT_KEYS):
        return True
    return False


def _is_portfolio_transfer(text: str) -> bool:
    """Перевод между портфелями одного клиента (СР СП ↔ ФР МБ) — не ввод/вывод.

    Деньги остаются у того же клиента, поэтому в расчёте доходности это
    внутренняя операция: иначе одна и та же сумма считается вложенной дважды.
    """
    return 'перевод' in text and 'портфель' in text


def _is_withdraw(text: str) -> bool:
    return any(k in text for k in _WITHDRAW_KEYS) and 'перевод' not in text


def _flows_xlsx(path: str) -> list[dict]:
    """Вводы/выводы из секции «Движение денежных средств» (ВТБ/Открытие).

    Берём именно датированные строки: в сводке «по субсчёту» суммы приведены
    за период целиком и без дат — для доходности (XIRR) нужны даты.
    """
    if pd is None:
        return []
    df = pd.read_excel(path, header=None)
    max_row, max_col = df.shape
    if max_col < 10:
        return []

    col_b = [str(v).strip() if not isinstance(v, str) else v.strip()
             for v in df.iloc[:, 1].tolist()]
    col_a = [str(v).strip() if not isinstance(v, str) else v.strip()
             for v in df.iloc[:, 0].tolist()]
    try:
        from app.parser_vtb import _find_sections_pd, _fmt_date
    except Exception:  # noqa: BLE001
        return []

    sections = _find_sections_pd(col_b)
    start, end = sections.get('Движение денежных средств', (None, None))
    if start is None:
        # Открытие/ЕБС: секция «Прочие зачисления/списания денежных средств».
        # Колонки широкие (Дата | … | Валюта | … | Сумма | … | Описание),
        # поэтому индексы ищем по строке заголовка.
        cash_start = None
        for i, (va, vb) in enumerate(zip(col_a, col_b)):
            if va.startswith('Прочие зачисления/списания денежных средств') or \
               vb.startswith('Прочие зачисления/списания денежных средств'):
                cash_start = i
                break
        if cash_start is None:
            return []
        header = cash_start + 1
        cols = {}
        for c in range(min(max_col, 80)):
            cell = str(df.iloc[header, c]).strip().lower()
            if not cell or cell == 'nan':
                continue
            if 'дата' in cell and 'date' not in cols:
                cols['date'] = c
            elif cell.startswith('сумма') and 'amount' not in cols:
                cols['amount'] = c
            elif cell.startswith('описан') and 'desc' not in cols:
                cols['desc'] = c
        if not {'date', 'amount', 'desc'} <= set(cols):
            return []
        out = []
        for r in range(header + 1, max_row):
            raw_date = df.iloc[r, cols['date']]
            if pd.isna(raw_date):
                continue
            day = _fmt_iso(raw_date)
            if len(day) != 10:
                continue
            amount = _to_float(df.iloc[r, cols['amount']])
            desc = str(df.iloc[r, cols['desc']]).strip()
            if not amount or not desc or desc == 'nan':
                continue
            text = desc.lower()
            if _is_portfolio_transfer(text):
                continue
            if _is_income(text):
                out.append({'date': day, 'amount': _income_signed(text, amount), 'kind': 'income',
                            'description': desc[:120]})
            elif _is_deposit(text):
                out.append({'date': day, 'amount': abs(amount), 'kind': 'deposit',
                            'description': desc[:120]})
            elif _is_withdraw(text):
                out.append({'date': day, 'amount': -abs(amount), 'kind': 'withdraw',
                            'description': desc[:120]})
        return out

    out = []
    for r in range(start + 1, min(end, max_row)):
        date_val = df.iloc[r, 1]
        amount_val = df.iloc[r, 2]
        if pd.isna(date_val) or pd.isna(amount_val):
            continue
        date_str = _fmt_date(date_val)
        if len(date_str) != 10:
            continue
        amount = _to_float(amount_val)
        if not amount:
            continue
        desc = ' '.join(str(df.iloc[r, c]) for c in range(7, max_col)
                        if c != 9 and str(df.iloc[r, c]) not in ('nan', ''))
        desc = f"{df.iloc[r, 9] if max_col > 9 else ''} {desc}"
        text = desc.lower()
        if 'перевод' in text:
            continue                       # перевод между счетами — не поток
        if _is_deposit(text):
            out.append({'date': _iso(date_str), 'amount': abs(amount), 'kind': 'deposit',
                        'description': desc.strip()[:120]})
        elif _is_withdraw(text):
            out.append({'date': _iso(date_str), 'amount': -abs(amount), 'kind': 'withdraw',
                        'description': desc.strip()[:120]})
        elif _is_income(text):
            out.append({'date': _iso(date_str), 'amount': _income_signed(text, amount),
                        'kind': 'income', 'description': desc.strip()[:120]})
    return out


def _flows_html(path: str) -> list[dict]:
    """Потоки и доход из HTML-отчёта Сбера.

    Строки выглядят так: ``27.05.2026 | Основной рынок | Выплата купонов ОФЗ… |
    RUB | 46 469.97 | 0.00`` — поэтому ищем ключевые слова по всей строке, а
    сумму берём как первое число после валюты.
    """
    if BeautifulSoup is None:
        return []
    with open(path, 'r', encoding='utf-8', errors='ignore') as fh:
        soup = BeautifulSoup(fh.read(), 'lxml')
    text = soup.get_text(' ')
    text = re.sub(r'&nbsp;', ' ', text)
    _start, end = _period(text)

    num_re = re.compile(r'^-?\d[\d\s]*(?:[.,]\d+)?$')
    out = []
    for tr in soup.find_all('tr'):
        cells = [c.get_text(' ', strip=True) for c in tr.find_all(['td', 'th'])]
        if not cells:
            continue
        m = re.match(r'(\d{2}\.\d{2}\.\d{4})', cells[0].strip())
        if not m:
            continue
        joined = ' '.join(cells).lower()
        if not (_is_income(joined) or _is_deposit(joined) or _is_withdraw(joined)):
            continue
        if 'вариационн' in joined or 'участнику акции' in joined:
            continue
        if 'налог' in joined and not _is_income(joined):
            continue
        nums = [_to_float(c) for c in cells if num_re.match(c.strip().replace('\u00a0', ' '))]
        nums = [n for n in nums if n]
        if not nums:
            continue
        amount = abs(nums[0])
        day = _iso(m.group(1))
        if not day or not amount:
            continue
        if _is_income(joined):
            kind, signed = 'income', _income_signed(joined, amount)
        elif _is_deposit(joined):
            kind, signed = 'deposit', amount
        else:
            kind, signed = 'withdraw', -amount
        out.append({'date': day, 'amount': signed, 'kind': kind,
                    'description': ' '.join(cells)[:120]})
    return out


def extract(path: str) -> dict | None:
    """Снимок по файлу отчёта (XLSX или HTML)."""
    ext = os.path.splitext(path)[1].lower()
    try:
        if ext in ('.xlsx', '.xls'):
            return extract_xlsx(path)
        if ext in ('.html', '.htm'):
            return extract_html(path)
    except Exception:  # noqa: BLE001 — битый файл не должен ломать проход
        return None
    return None


def _is_income(text: str) -> bool:
    """Дивиденды и купоны — доход, а не ввод/вывод денег."""
    return ('дивиденд' in text or 'купон' in text
            or 'выплата дохода' in text or 'перечисление дохода' in text)


def _is_tax(text: str) -> bool:
    """Удержанный налог с дохода — уменьшает доход, а не увеличивает его."""
    return ('удержан налог' in text or 'налог на доход' in text
            or 'налог на купонный доход' in text or 'ндфл' in text)


def _income_signed(text: str, amount: float) -> float:
    return -abs(amount) if _is_tax(text) else abs(amount)


def save_income(rows: list[dict]) -> int:
    """Сохранить дивиденды/купоны (для таблицы роста портфеля)."""
    if not rows:
        return 0
    conn = _conn()
    conn.execute('CREATE TABLE IF NOT EXISTS income_flow ('
                 'flow_date TEXT NOT NULL, amount REAL NOT NULL, kind TEXT DEFAULT "", '
                 'PRIMARY KEY (flow_date, amount))')
    saved = 0
    for r in rows:
        cur = conn.execute('INSERT OR IGNORE INTO income_flow(flow_date, amount, kind) VALUES (?,?,?)',
                           (r['date'], r['amount'], r.get('kind', '')))
        saved += cur.rowcount
    conn.commit()
    conn.close()
    return saved


def all_income() -> list[dict]:
    """``[{'date','amount','kind'}]`` — дивиденды и купоны по датам."""
    conn = _conn()
    try:
        rows = conn.execute('SELECT flow_date, amount, kind FROM income_flow '
                            'ORDER BY flow_date').fetchall()
    except Exception:  # noqa: BLE001
        rows = []
    conn.close()
    # Отчёты за период перекрываются — одну выплату считаем один раз.
    out, seen = [], set()
    for r in rows:
        key = (r['flow_date'], round(r['amount'], 2))
        if key in seen:
            continue
        seen.add(key)
        out.append({'date': r['flow_date'], 'amount': r['amount'], 'kind': r['kind'] or ''})
    return out


def save_flows(filename: str, flows: list[dict]) -> int:
    """Сохранить внешние потоки (пополнения +, выводы −) с именем отчёта."""
    if not flows:
        return 0
    flows = [f for f in flows if f.get('kind') != 'income']
    if not flows:
        return 0
    conn = _conn()
    conn.execute('CREATE TABLE IF NOT EXISTS capital_flow ('
                 'flow_date TEXT NOT NULL, amount REAL NOT NULL, '
                 'filename TEXT DEFAULT "", description TEXT DEFAULT "", '
                 'PRIMARY KEY (flow_date, amount))')
    saved = 0
    for f in flows:
        cur = conn.execute(
            'INSERT OR IGNORE INTO capital_flow(flow_date, amount, filename, description) '
            'VALUES (?,?,?,?)',
            (f['date'], f['amount'], os.path.basename(filename), f.get('description', '')[:120]))
        saved += cur.rowcount
    conn.commit()
    conn.close()
    return saved


def all_flows() -> list[dict]:
    """Потоки: ``[{'date','amount','filename'}]`` по возрастанию даты."""
    conn = _conn()
    try:
        rows = conn.execute('SELECT flow_date, amount, filename FROM capital_flow '
                            'ORDER BY flow_date').fetchall()
    except Exception:  # noqa: BLE001
        rows = []
    conn.close()
    return [{'date': r['flow_date'], 'amount': r['amount'], 'filename': r['filename'] or ''}
            for r in rows]


# ── Хранение снимков ────────────────────────────────────────

def _conn():
    from app.db import get_connection
    return get_connection()


def save(path: str, data: dict, broker: str = '') -> None:
    """Сохранить снимок отчёта (идемпотентно по имени файла и дате)."""
    conn = _conn()
    conn.execute('CREATE TABLE IF NOT EXISTS report_snapshot ('
                 'filename TEXT NOT NULL, snapshot_date TEXT NOT NULL, '
                 'broker TEXT DEFAULT "", assets REAL NOT NULL, cash REAL DEFAULT 0, '
                 'updated_at TEXT NOT NULL DEFAULT (datetime(\'now\', \'localtime\')), '
                 'PRIMARY KEY (filename, snapshot_date))')
    conn.execute(
        'INSERT INTO report_snapshot(filename, snapshot_date, broker, assets, cash, updated_at) '
        "VALUES (?,?,?,?,?, datetime('now','localtime')) "
        'ON CONFLICT(filename, snapshot_date) DO UPDATE SET '
        'assets=excluded.assets, cash=excluded.cash, broker=excluded.broker',
        (os.path.basename(path), data['date'], broker, data['assets'], data['cash']))
    conn.commit()
    conn.close()


def all_snapshots() -> list[dict]:
    """Все сохранённые снимки по возрастанию даты."""
    conn = _conn()
    try:
        rows = conn.execute('SELECT filename, snapshot_date, broker, assets, cash '
                            'FROM report_snapshot ORDER BY snapshot_date').fetchall()
    except Exception:  # noqa: BLE001 — таблицы ещё нет
        rows = []
    conn.close()
    return [dict(r) for r in rows]

