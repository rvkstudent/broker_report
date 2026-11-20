"""Рост портфеля по годам — по снимкам размера активов из брокерских отчётов.

Почему по отчётам, а не из сделок: в отчёте брокер уже оценил активы на дату
(бумаги по рынку + деньги) и учёл корпоративные действия (обратный сплит ВТБ
1:5000, конвертации и т. п.). Пересчитывать это из сделок и текущих цен —
источник ошибок в разы.

Что считаем:
* стоимость портфеля на дату — сумма последних снимков по каждому брокеру;
* внешние потоки (пополнения/выводы) — из ``cash_flow``; переводы между
  брокерами взаимно гасятся (списание у одного = зачисление у другого);
* доходность — time-weighted (TWR) по месяцам, не зависит от размера взносов;
* бенчмарк — MCFTR (индекс МосБиржи полной доходности, с дивидендами);
* пересчёт в USD — по биржевому курсу USD000UTSTOM на конец периода.
"""

from __future__ import annotations

import threading
import time
from datetime import date, datetime, timedelta, timezone

from app.db import get_connection, _categorize_cash_flow, _is_bond_class
from app import dashboard_prices as tinvest
from app import report_snapshots

BENCHMARK = 'MCFTR'
USD_INSTRUMENT = 'USD000UTSTOM'

_CACHE_TTL = 6 * 3600
_cache: dict = {}
_cache_lock = threading.Lock()


# ── утилиты ─────────────────────────────────────────────────

def _iso(dmy: str) -> str:
    d = (dmy or '').strip()[:10]
    if len(d) == 10 and d[2] == '.' and d[5] == '.':
        return f'{d[6:10]}-{d[3:5]}-{d[0:2]}'
    return d


def _family(filename: str) -> str:
    """К какому брокеру относится отчёт (по имени файла)."""
    name = (filename or '').lower()
    if '424f02n' in name:
        return 'sber'
    if 'втб' in name or 'vtb' in name or '130r63' in name:
        return 'vtb'
    if 'ебс' in name or 'открытие' in name or 'brok_rpt_bo' in name or 'брокерский' in name:
        return 'open'
    if name.startswith('report'):
        return 'vtb'
    return 'other'


def _month_ends(start: date, end: date) -> list[date]:
    out = []
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        if m == 12:
            last, y, m = date(y, 12, 31), y + 1, 1
        else:
            last = date(y, m + 1, 1) - timedelta(days=1)
            m += 1
        if last >= start:
            out.append(min(last, end))
    return out


def _daily_series(code: str, class_code: str, start: date, end: date) -> dict:
    client = tinvest._get_client()
    if client is None:
        return {}
    ins = tinvest._find(client, code, class_code or '')
    if not ins or not ins.get('uid'):
        return {}
    try:
        candles = client.candles(
            ins['uid'],
            datetime.combine(start, datetime.min.time(), tzinfo=timezone.utc),
            datetime.combine(end + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc))
    except Exception:  # noqa: BLE001
        return {}
    series = {}
    for c in candles:
        price = tinvest._convert(client, ins, c['close'])
        if price and price > 0:
            utc = datetime.fromtimestamp(c['ts_ns'] / 1_000_000_000, tz=timezone.utc)
            series[(utc.replace(tzinfo=None) + timedelta(hours=3)).strftime('%Y-%m-%d')] = price
    return series


def _cached(key: str, build):
    now = time.time()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < _CACHE_TTL:
            return hit[1]
    value = build()
    with _cache_lock:
        _cache[key] = (now, value)
    return value


# ── потоки денег ────────────────────────────────────────────

def _external_flows() -> tuple[list[dict], float]:
    """Пополнения/выводы с взаимным гашением переводов между брокерами.

    Перевод из Сбера в ВТБ выглядит как списание у одного и зачисление у
    другого. Внутри портфеля это не поток: гасим пары «+X / −X» в пределах
    двух дней и похожей суммой.
    """
    conn = get_connection()
    rows = conn.execute("""
        SELECT date, description, source, COALESCE(credit,0) AS credit, COALESCE(debit,0) AS debit
        FROM cash_flow ORDER BY substr(date,7,4)||substr(date,4,2)||substr(date,1,2)
    """).fetchall()
    conn.close()

    raw = []
    for r in rows:
        desc = r['description'] or ''
        cat = _categorize_cash_flow(desc, r['source'] or '')
        if cat not in ('Пополнение', 'Вывод ДС'):
            continue
        amount = (r['credit'] or 0) - (r['debit'] or 0)
        if amount:
            raw.append({'date': _iso(r['date']), 'amount': amount})

    raw.sort(key=lambda f: f['date'])
    used = [False] * len(raw)
    transfers = 0.0
    for i, a in enumerate(raw):
        if used[i]:
            continue
        for j in range(i + 1, len(raw)):
            if used[j]:
                continue
            b = raw[j]
            if (datetime.fromisoformat(b['date']) - datetime.fromisoformat(a['date'])).days > 2:
                break
            same_size = abs(abs(a['amount']) - abs(b['amount'])) < max(100.0, abs(a['amount']) * 0.02)
            if a['amount'] * b['amount'] < 0 and same_size:
                used[i] = used[j] = True
                transfers += abs(a['amount'])
                break

    flows = [r for i, r in enumerate(raw) if not used[i]]
    return flows, round(transfers, 2)


def _manual_flows() -> list[dict]:
    """Ручные потоки (переводы между брокерами, которых нет в отчётах)."""
    try:
        from app.db import get_manual_flows
        return [{'date': m['date'], 'amount': m['amount'],
                 'filename': 'manual', 'description': m.get('description') or ''}
                for m in get_manual_flows() if m.get('amount')]
    except Exception:  # noqa: BLE001
        return []


def external_flows() -> tuple[list[dict], float]:
    """Внешние потоки (вводы/выводы) с гашением переводов между брокерами.

    Берём операции из отчётов (``capital_flow``): вывод у одного брокера и
    зачисление у другого — перевод внутри портфеля, а не ввод/вывод. Пары
    «+X у A / −X у B» в пределах 3 дней и 2% суммы гасятся.
    """
    flows = report_snapshots.all_flows()
    if not flows:
        raw, transfers = _external_flows()
        # ручные потоки добавляем и здесь — иначе они теряются без отчётов
        for m in _manual_flows():
            raw.append(m)
        return sorted(raw, key=lambda f: f['date']), transfers
    for f in flows:
        f['broker'] = _family(f.get('filename', ''))

    positives = [f for f in flows if f['amount'] > 0]
    negatives = [f for f in flows if f['amount'] < 0]
    used = set()
    transfers = 0.0
    for p in sorted(positives, key=lambda f: f['date']):
        if id(p) in used:
            continue
        pd = date.fromisoformat(p['date'])
        for n in negatives:
            if id(n) in used:
                continue
            nd = date.fromisoformat(n['date'])
            if abs((nd - pd).days) > 3:
                continue
            if abs(abs(n['amount']) - p['amount']) <= max(100.0, p['amount'] * 0.02):
                used.add(id(p))
                used.add(id(n))
                transfers += p['amount']
                break

    external = [f for f in flows if id(f) not in used]
    external.extend(_manual_flows())
    external.sort(key=lambda f: f['date'])
    return external, round(transfers, 2)


def _snapshot_families() -> dict:
    """Снимки по брокерам: {семья: [(дата, активы), …]} + допустимый «возраст»."""
    families: dict[str, list] = {}
    for s in report_snapshots.all_snapshots():
        families.setdefault(_family(s['filename']), []).append((s['snapshot_date'], s['assets']))
    for rows in families.values():
        rows.sort()
    return families


def _family_max_age(rows: list) -> int:
    """Сколько дней снимок считается актуальным.

    Годовые отчёты (2016–2020) идут раз в 365 дней — их тянем вперёд до 400 дней,
    иначе график и оценка рвутся. Месячные — 200 дней: просроченный снимок
    (например, ВТБ без отчётов за январь–июнь 2026) нельзя складывать со свежими
    снимками другого брокера, иначе стоимость задваивается.
    """
    gaps = sorted((date.fromisoformat(rows[i + 1][0]) - date.fromisoformat(rows[i][0])).days
                  for i in range(len(rows) - 1))
    if not gaps:
        return 400
    median = gaps[len(gaps) // 2]
    return 400 if median > 200 else 200


def _value_at(families: dict, day: str) -> float:
    """Сумма последних снимков по брокерам.

    Пока у счёта есть более поздний снимок, он «жив» — тянем последнюю оценку
    вперёд (в 2016–2020 отчёты годовые). Просрочку (>200 дней) применяем только
    к последнему снимку счёта, иначе закрытый счёт висит в оценке годами.
    """
    total = 0.0
    d_day = date.fromisoformat(day)
    for rows in families.values():
        prev = None
        nxt = None
        for dd, assets in rows:
            if dd <= day:
                prev = (dd, assets)
            elif nxt is None:
                nxt = dd
        if prev is None:
            continue
        if nxt is None and (d_day - date.fromisoformat(prev[0])).days > 200:
            continue
        total += prev[1]
    return total


def _value_asof(families: dict, day: str) -> float:
    """Сумма последних известных снимков по брокерам на дату, без срока давности.

    Нужна для стартовой стоимости валютного окна: важен размер капитала
    *тогда*, даже если счёт закрыт позже (поэтому правило «>200 дней — не
    актуально» здесь не применяем).
    """
    total = 0.0
    for rows in families.values():
        prev = None
        for dd, assets in rows:
            if dd <= day:
                prev = assets
            else:
                break
        if prev is not None:
            total += prev
    return total


def xirr_for_value(terminal: float, start_day: str | None = None,
                   start_value: float | None = None) -> float | None:
    """XIRR, если портфель закрыть по заданной стоимости.

    ``start_day`` — считать только с этой даты (текущая эпоха портфеля),
    ``start_value`` — стоимость портфеля на эту дату (стартовый капитал).
    """
    external, _ = external_flows()
    if start_day:
        cash = [(date.fromisoformat(start_day), -(start_value or 0))]
        cash += [(date.fromisoformat(f['date']), -f['amount'])
                 for f in external if f['date'] > start_day]
    else:
        cash = [(date.fromisoformat(f['date']), -f['amount']) for f in external]
    if not cash:
        return None
    return _xirr(cash, terminal)


def contribution_returns() -> dict:
    """Доходность каждого пополнения отдельно («по паям»).

    Портфель делим на паи: пай стоит столько, сколько сейчас единица капитала.
    Каждое пополнение покупает паи по текущей цене пая, поэтому у каждого взноса
    своя доходность — от его даты до сегодня. Средневзвешенная по суммам
    доходность и отвечает на вопрос «на сколько в среднем растут мои вложения».
    """
    snaps = report_snapshots.all_snapshots()
    flows, _transfers = external_flows()
    flows = sorted([f for f in flows if f.get('amount')], key=lambda f: f['date'])
    if not snaps or not flows:
        return {}

    families = _snapshot_families()

    end_day = max(s['snapshot_date'] for s in snaps)
    # Считаем только текущую «эпоху» портфеля: после последнего обнуления счёта
    # (перевод другому брокеру) прежние взносы уже не имеют отношения к текущему
    # портфелю, и их доходность посчитать нельзя.
    zeros = [s['snapshot_date'] for s in snaps if not s['assets']]
    epoch_start = max(zeros) if zeros else min(s['snapshot_date'] for s in snaps)
    # Первый ввод после обнуления — начало отсчёта паёв (иначе паи уходят в минус).
    first_in = next((f['date'] for f in flows if f['date'] >= epoch_start and f['amount'] > 0), None)
    if first_in is None:
        return {}
    epoch_start = first_in
    points = sorted(d for d in ({f['date'] for f in flows} | {s['snapshot_date'] for s in snaps})
                    if d >= epoch_start)
    units = 0.0
    nav = 1.0
    out = []
    for day in points:
        value = _value_at(families, day)
        if units > 0 and value > 0:
            nav = value / units
        for f in (x for x in flows if x['date'] == day):
            if nav <= 0:
                continue
            units += f['amount'] / nav
            out.append({'date': day, 'amount': f['amount'], 'nav': nav})

    value_end = _value_at(families, end_day)
    if units <= 0 or value_end <= 0:
        return {}
    nav_end = value_end / units
    end = date.fromisoformat(end_day)
    deposits = []
    weighted = 0.0
    total_amount = 0.0
    for row in out:
        if row['amount'] <= 0 or row['nav'] <= 0:
            continue
        start = date.fromisoformat(row['date'])
        years = (end - start).days / 365.0
        factor = nav_end / row['nav']
        total_pct = round((factor - 1) * 100, 2)
        annual = round(((factor ** (1 / years)) - 1) * 100, 2) if years > 0.05 else None
        deposits.append({'date': row['date'], 'amount': round(row['amount'], 2),
                         'years': round(years, 2), 'total_pct': total_pct, 'annual': annual})
        if annual is not None:
            weighted += annual * row['amount']
            total_amount += row['amount']

    deposits.sort(key=lambda r: r['date'])
    return {
        'rows': deposits,
        'weighted_annual': round(weighted / total_amount, 2) if total_amount else None,
        'nav_end': round(nav_end, 4),
        'as_of': end_day,
        'count': len(deposits),
        'total': round(sum(r['amount'] for r in deposits), 2),
    }


def _year_xirr(start_day: str, start_value: float, flows: list, end_day: str,
               end_value: float):
    """Годовая доходность одного года с учётом дат вводов/выводов.

    Решает: −старт − Σ ввод/(1+r)^t + Σ вывод/(1+r)^t + конец/(1+r)^T = 0.
    """
    if not end_value or (not start_value and not flows):
        return None
    d0 = date.fromisoformat(start_day)
    years = (date.fromisoformat(end_day) - d0).days / 365.0
    if years <= 0:
        return None

    def npv(r):
        total = -start_value
        for day, amount in flows:
            t = (date.fromisoformat(day) - d0).days / 365.0
            total -= amount / (1 + r) ** t
        return total + end_value / (1 + r) ** years

    lo, hi = -0.95, 5.0
    f_lo = npv(lo)
    if f_lo * npv(hi) > 0:
        return None
    for _ in range(200):
        mid = (lo + hi) / 2
        v = npv(mid)
        if abs(v) < 1e-6:
            break
        if f_lo * v <= 0:
            hi = mid
        else:
            lo, f_lo = mid, v
    rate = (lo + hi) / 2 * 100
    if not (-95 <= rate <= 500):
        return None
    return round(rate, 2)


def _usd_at(series: dict, day: str):
    keys = [d for d in series if d <= day]
    return series[max(keys)] if keys else None


def _xirr(cash: list, terminal: float | None = None,
          terminal_day: str | None = None) -> float | None:
    """Годовая доходность по нерегулярным потокам (перебор корня NPV).

    ``terminal_day`` — дата, на которую посчитана итоговая стоимость. Без неё
    берём сегодня, но для снимков из отчётов это должно быть ``as_of``, иначе
    период растягивается до сегодня и годовая доходность занижается.
    """
    flows = list(cash)
    if terminal:
        flows.append((date.fromisoformat(terminal_day) if terminal_day else date.today(),
                      terminal))
    if len(flows) < 2:
        return None
    flows.sort()

    def npv(rate: float) -> float:
        d0 = flows[0][0]
        return sum(amount / (1 + rate) ** ((d - d0).days / 365.0) for d, amount in flows)

    lo, hi = -0.9999, 10.0
    f_lo = npv(lo)
    if f_lo * npv(hi) >= 0:
        return None
    for _ in range(200):
        mid = (lo + hi) / 2
        v = npv(mid)
        if abs(v) < 1e-6:
            break
        if f_lo * v <= 0:
            hi = mid
        else:
            lo, f_lo = mid, v
    return round((lo + hi) / 2 * 100, 2)


def invested_summary() -> dict:
    """Доходность вложенных денег: вложено, сейчас, прибыль и XIRR.

    Деньги ходят между брокерами (Открытие → ВТБ → Ньютон → Сбер): вывод у
    одного и зачисление у другого — это НЕ ввод и НЕ вывод, а перевод внутри
    портфеля. Поэтому пары «+X у брокера A / −X у брокера B» в пределах
    нескольких дней взаимно гасятся; внешними остаются только операции с
    деньгами вне портфеля.
    """
    external, transfers = external_flows()
    connected = round(sum(f['amount'] for f in external if f['amount'] > 0), 2)
    withdrawn = round(sum(f['amount'] for f in external if f['amount'] < 0), 2)
    net_invested = round(connected + withdrawn, 2)

    # Стоимость — по последнему снимку каждого брокера из отчётов.
    # Снимки старше 200 дней не учитываем: счёт мог быть закрыт, а деньги
    # уже переведены другому брокеру (иначе стоимость задваивается).
    snaps = report_snapshots.all_snapshots()
    latest = {}
    for s in snaps:
        fam = _family(s['filename'])
        if fam not in latest or s['snapshot_date'] > latest[fam]['snapshot_date']:
            latest[fam] = s
    last_day = max((s['snapshot_date'] for s in latest.values()), default=None)
    if last_day:
        cutoff = date.fromisoformat(last_day) - timedelta(days=200)
        latest = {fam: s for fam, s in latest.items()
                  if date.fromisoformat(s['snapshot_date']) >= cutoff}
    value = round(sum(s['assets'] for s in latest.values()), 2)
    if not value:
        from app.db import get_open_trades, get_current_prices
        prices = {p['sec_code']: p['price'] for p in get_current_prices()}
        value = round(sum((l['qty'] or 0) * (prices.get(l['security_code']) or l['buy_price'] or 0)
                          for l in get_open_trades(None, None, '', 'all')), 2)

    # Дата, на которую посчитана стоимость: последний снимок, а не «сегодня».
    as_of = max((s['snapshot_date'] for s in latest.values()), default=None) or date.today().isoformat()

    profit = round(value - net_invested, 2) if net_invested else None
    simple = round(profit / net_invested * 100, 2) if (profit is not None and net_invested) else None

    cash = [(date.fromisoformat(f['date']), -f['amount']) for f in external]
    xirr = _xirr(cash, value, as_of)

    # XIRR за период «текущего портфеля»: после последнего обнуления счёта
    # (перевод денег другому брокеру / закрытие) начинается новая история, и
    # общая доходность «за все годы» её размывает. Считаем отдельно от
    # ближайшего снимка после последнего нуля.
    xirr_period = None
    period_start = None
    period_start_value = None
    try:
        zeros = [s['snapshot_date'] for s in snaps if not s['assets']]
        after = [s for s in snaps
                 if (not zeros or s['snapshot_date'] > max(zeros)) and s['assets']]
        if zeros and after:
            first = min(after, key=lambda s: s['snapshot_date'])
        else:
            # Нет «обнуления» счёта — ищем самый большой разрыв в истории снимков:
            # после него начинается текущий период (у нас это 2020-11-08 → 2022-12-31).
            dates = sorted({s['snapshot_date'] for s in snaps})
            best_gap, best_date = 0, None
            for prev, cur in zip(dates, dates[1:]):
                delta = (date.fromisoformat(cur) - date.fromisoformat(prev)).days
                if delta > best_gap:
                    best_gap, best_date = delta, cur
            first = (next((s for s in snaps if s['snapshot_date'] == best_date and s['assets']),
                          None) if best_gap > 300 else None)
        if first:
            period_start = first['snapshot_date']
            period_start_value = round(first['assets'], 2)
            period_cash = [(date.fromisoformat(period_start), -period_start_value)]
            period_cash += [(date.fromisoformat(f['date']), -f['amount'])
                            for f in external if f['date'] > period_start]
            xirr_period = _xirr(period_cash, value, as_of)
    except Exception:  # noqa: BLE001
        xirr_period = None

    # ── То же в USD: каждый ввод пересчитываем по курсу НА ДАТУ операции ──
    # Курс USD000UTSTOM в источнике есть только с 2022 года, поэтому валютный
    # расчёт начинаем с этого момента. Но к началу окна портфель уже мог стоить
    # денег (отчёты 2016–2021 идут без курса), и эту стоимость обязательно
    # добавляем стартовым потоком: иначе рост «старых» денег приписывается
    # вводам окна и XIRR получается завышенным.
    usd_start = date(2022, 1, 3)
    usd_series = _cached(
        f'usd::{usd_start}::{as_of}',
        lambda: _daily_series(USD_INSTRUMENT, 'CETS', usd_start, date.fromisoformat(as_of)))
    if not usd_series:
        usd_series = _cached(f'usd2022::{as_of}',
                             lambda: _daily_series(USD_INSTRUMENT, 'CETS',
                                                   date(2022, 1, 3), date.fromisoformat(as_of)))
    # Стоимость портфеля на начало USD-окна — это уже вложенный капитал.
    families: dict[str, list] = {}
    for s in snaps:
        families.setdefault(_family(s['filename']), []).append((s['snapshot_date'], s['assets']))
    for rows in families.values():
        rows.sort()
    start_rub = (_value_asof(families, usd_start.isoformat())
                 if (snaps and usd_start.isoformat() < as_of) else 0.0)
    start_rate = _usd_at(usd_series, usd_start.isoformat())
    if not start_rate and usd_series:
        start_rate = usd_series[min(usd_series)]
    usd_start_value = round(start_rub / start_rate, 2) if (start_rub and start_rate) else 0.0

    usd_cash = []
    usd_connected = usd_withdrawn = 0.0
    if usd_start_value:
        usd_cash.append((usd_start, -usd_start_value))
    for f in external:
        if f['date'] < usd_start.isoformat():
            continue
        rate = _usd_at(usd_series, f['date'])
        if not rate:
            continue
        usd_amount = f['amount'] / rate
        usd_cash.append((date.fromisoformat(f['date']), -usd_amount))
        if usd_amount > 0:
            usd_connected += usd_amount
        else:
            usd_withdrawn += usd_amount
    usd_rate_now = _usd_at(usd_series, as_of)
    usd_value = round(value / usd_rate_now, 2) if usd_rate_now else None
    usd_xirr = _xirr(usd_cash, usd_value, as_of)
    usd_profit = (round(usd_value - usd_start_value - (usd_connected + usd_withdrawn), 2)
                  if usd_value is not None else None)

    # Разбивка прибыли: доход (дивиденды/купоны) + торговый результат + переоценка
    try:
        income = round(sum(i['amount'] for i in report_snapshots.all_income()), 2)
    except Exception:  # noqa: BLE001
        income = 0.0
    trading = 0.0
    try:
        from app.db import get_trade_lots
        for lot in get_trade_lots(None, None, None, 'all'):
            trading += ((lot.get('profit') or 0)
                        - (lot.get('buy_fee') or 0) - (lot.get('sell_fee') or 0))
    except Exception:  # noqa: BLE001
        trading = 0.0
    trading = round(trading, 2)
    revaluation = round(profit - income - trading, 2) if profit is not None else None

    return {
        'connected': connected,
        'withdrawn': withdrawn,
        'transfers': round(transfers, 2),
        'net_invested': net_invested,
        'value': value,
        'profit': profit,
        'income': income,
        'trading': trading,
        'revaluation': revaluation,
        'pct_to_invested': (round((income + trading) / net_invested * 100, 2)
                            if net_invested else None),
        'simple_pct': simple,
        'xirr': xirr,
        'xirr_period': xirr_period,
        'period_start': period_start,
        'period_start_value': period_start_value,
        'usd_connected': round(usd_connected, 2),
        'usd_withdrawn': round(usd_withdrawn, 2),
        'usd_value': usd_value,
        'usd_start_value': usd_start_value,
        'usd_profit': usd_profit,
        'usd_xirr': usd_xirr,
        'usd_rate_now': round(usd_rate_now, 4) if usd_rate_now else None,
        'usd_since': usd_start.isoformat(),
        'flows': len(external),
        'flows_total': len(external) + (1 if transfers else 0),
        'first_flow': external[0]['date'] if external else None,
        'last_flow': external[-1]['date'] if external else None,
        'by_broker': {fam: s['assets'] for fam, s in latest.items()},
        'as_of': as_of,
    }


# ── текущий портфель: структура, доходность по выплатам, скользящая XIRR ──

def _position_category(name: str, class_code: str) -> str:
    """К какой группе отнести бумагу в структуре текущего портфеля."""
    n = (name or '').upper()
    lc = (class_code or '').upper()
    if lc == 'SPBFUT' or 'ФЬЮЧЕРС' in n:
        return 'Фьючерсы'
    if 'ОФЗ' in n or lc == 'TQOB':
        return 'ОФЗ'
    if _is_bond_class(lc) or 'ОБЛИГ' in n:
        return 'Облигации'
    if lc.startswith('TQTF') or lc.startswith('TQIF') or 'ФОНД' in n or 'ETF' in n:
        return 'Фонды'
    return 'Акции'


def current_portfolio_summary(invested: dict | None = None) -> dict:
    """Текущая структура портфеля + доходность по выплатам + скользящая XIRR.

    Отвечает на вопрос «чем сейчас наполнен портфель и как он работает
    последние 12/36 месяцев» — без «размывания» убыточной историей 2016–2020.
    Полную YTM здесь не считаем: в БД нет графика купонов и дат погашения по
    каждой бумаге, поэтому доходность облигаций даётся по фактическим выплатам
    за 12 месяцев (нижняя, «денежная» оценка). Скользящая XIRR — money-weighted
    за окно, она показывает результат именно текущего портфеля.
    """
    inv = invested if invested is not None else invested_summary()
    value = inv.get('value') or 0.0
    as_of = inv.get('as_of') or date.today().isoformat()
    as_of_d = date.fromisoformat(as_of)

    # ── структура текущих позиций ──
    from app.db import get_instrument_summary, get_current_prices
    try:
        positions = get_instrument_summary(None, '', '', 'all') or []
    except Exception:  # noqa: BLE001
        positions = []
    try:
        price_rows = {p['sec_code']: p for p in (get_current_prices() or [])}
    except Exception:  # noqa: BLE001
        price_rows = {}

    by_cat: dict[str, float] = {}
    sec_total = 0.0
    for pos in positions:
        code = pos.get('security_code') or ''
        row = price_rows.get(code) or {}
        price = row.get('price') or pos.get('avg_price') or 0
        val = (pos.get('qty') or 0) * price
        cat = _position_category(pos.get('security_name') or '', row.get('class_code') or '')
        if cat == 'Фьючерсы':
            # нотионал фьючерса — не стоимость капитала; в структуру не кладём
            by_cat.setdefault('Фьючерсы', 0.0)
            continue
        by_cat[cat] = by_cat.get(cat, 0.0) + val
        sec_total += val

    cash = max(value - sec_total, 0.0)
    if cash > 0.01:
        by_cat['Кэш'] = cash
    denom = (sec_total + cash) or value
    structure = []
    for label in ('ОФЗ', 'Облигации', 'Акции', 'Фонды', 'Фьючерсы', 'Кэш'):
        v = round(by_cat.get(label, 0.0), 2)
        if v <= 0:
            continue
        structure.append({'label': label, 'value': v,
                          'pct': round(v / denom * 100, 1) if denom else 0.0})

    bonds_value = round(by_cat.get('ОФЗ', 0.0) + by_cat.get('Облигации', 0.0), 2)

    # ── доход по фактическим выплатам за 12 месяцев ──
    start_12 = as_of_d - timedelta(days=365)
    try:
        income = report_snapshots.all_income() or []
    except Exception:  # noqa: BLE001
        income = []
    income_12m = round(sum((i.get('amount') or 0) for i in income
                           if start_12.isoformat() <= (i.get('date') or '') <= as_of), 2)
    yield_total = round(income_12m / value * 100, 2) if value else None
    yield_bonds = round(income_12m / bonds_value * 100, 2) if bonds_value else None

    # ── скользящая XIRR (money-weighted за последние N месяцев) ──
    try:
        snaps = report_snapshots.all_snapshots() or []
    except Exception:  # noqa: BLE001
        snaps = []
    families: dict[str, list] = {}
    for s in snaps:
        families.setdefault(_family(s['filename']), []).append((s['snapshot_date'], s['assets']))
    for rows in families.values():
        rows.sort()
    try:
        external, _ = external_flows()
    except Exception:  # noqa: BLE001
        external = []

    # Даты снимков: скользящее окно стартуем от РЕАЛЬНОГО снимка, ближайшего к
    # целевой дате. Нельзя брать _value_asof от произвольной даты: он суммирует
    # последние снимки всех счетов, включая давно закрытые (старый брокер
    # 2020-го + счёт 2022-го + текущий), из-за чего старт завышается в разы и
    # XIRR становится ложно отрицательной.
    snap_dates = sorted({d for rows in families.values() for d, _ in rows if d <= as_of})

    def rolling(months: int):
        if not snap_dates:
            return None
        target = as_of_d - timedelta(days=int(months * 30.4375))
        start_day = min(snap_dates, key=lambda d: abs((date.fromisoformat(d) - target).days))
        start = date.fromisoformat(start_day)
        # Стоимость на дату — только счета, актуальные на тот момент
        # (_value_at отсекает просроченные снимки закрытых брокеров).
        start_value = _value_at(families, start_day)
        if not start_value:
            start_value = sum(a for rows in families.values()
                              for d, a in rows if d == start_day)
        if not start_value:
            return None
        cash_flows = [(start, -start_value)]
        cash_flows += [(date.fromisoformat(f['date']), -f['amount'])
                       for f in external
                       if start_day < (f.get('date') or '') <= as_of]
        return _xirr(cash_flows, value, as_of)

    return {
        'as_of': as_of,
        'total': round(denom, 2),
        'structure': structure,
        'bonds_value': bonds_value,
        'ofz_value': round(by_cat.get('ОФЗ', 0.0), 2),
        'cash_value': round(by_cat.get('Кэш', 0.0), 2),
        'income_12m': income_12m,
        'yield_total_pct': yield_total,
        'yield_bonds_pct': yield_bonds,
        'rolling_xirr_12m': rolling(12),
        'rolling_xirr_36m': rolling(36),
    }


# ── основной расчёт ─────────────────────────────────────────

def build(start_date: date | None = None, end_date: date | None = None) -> dict:
    snaps = report_snapshots.all_snapshots()
    if not snaps:
        return {'years': [], 'series': [], 'snapshots': [], 'benchmark': BENCHMARK,
                'usd': USD_INSTRUMENT, 'start': '', 'end': '',
                'transfers': 0.0, 'error': 'Нет снимков отчётов'}

    for s in snaps:
        s['family'] = _family(s['filename'])

    families: dict[str, list] = {}
    for s in snaps:
        families.setdefault(s['family'], []).append((s['snapshot_date'], s['assets']))
    for fam in families:
        families[fam].sort()

    start = start_date or date.fromisoformat(min(
        min(s['snapshot_date'] for s in snaps),
        min((f['date'] for f in report_snapshots.all_flows()), default='9999-12-31')))
    end = end_date or date.today()

    benchmark = _cached(f'bench::{start}::{end}', lambda: _daily_series(BENCHMARK, '', start, end))
    # Курс USD в источнике есть только с 2022 года — раньше запрос возвращает пусто.
    usd_from = max(start, date(2022, 1, 3))
    usd = _cached(f'usd::{usd_from}::{end}',
                  lambda: _daily_series(USD_INSTRUMENT, 'CETS', usd_from, end))
    def last_on(day: str, data: dict):
        keys = [d for d in data if d <= day]
        return data[max(keys)] if keys else None

    def value_at(day: str) -> float:
        """Сумма последних снимков по брокерам (с учётом срока актуальности)."""
        return _value_at(families, day)

    flows, transfers = external_flows()
    flow_by_month = {}
    # Вводы и выводы по годам считаем из самих потоков: месячный нетто-поток
    # складывать нельзя — в одном месяце бывают и пополнения, и списания.
    dep_by_year, wd_by_year = {}, {}
    # Потоки в USD — по курсу НА ДАТУ операции, а не на конец периода
    usd_flow_by_month = {}
    for f in flows:
        year = f['date'][:4]
        if f['amount'] > 0:
            dep_by_year[year] = dep_by_year.get(year, 0) + f['amount']
        else:
            wd_by_year[year] = wd_by_year.get(year, 0) + f['amount']
        flow_by_month[f['date'][:7]] = flow_by_month.get(f['date'][:7], 0) + f['amount']
        rate = _usd_at(usd, f['date'])
        if rate:
            usd_flow_by_month[f['date'][:7]] = usd_flow_by_month.get(f['date'][:7], 0) + f['amount'] / rate

    # Точки оценки — даты снимков (+ сегодня). Между снимками стоимость
    # считаем неизменной: месячная сетка с «замороженной» стоимостью даёт
    # скачки TWR (весь рост года падает на один месяц).
    points = sorted({s['snapshot_date'] for s in snaps} | {end.isoformat()})
    points = [p for p in points if p >= start.isoformat()]
    flows_sorted = sorted(flows, key=lambda f: f['date'])

    monthly = []
    prev_value = None
    prev_usd = None
    prev_point = None
    fi = 0
    for ds in points:
        value = value_at(ds)
        # потоки за интервал (prev_point, ds]
        flow = 0.0
        usd_flow = 0.0
        while fi < len(flows_sorted) and flows_sorted[fi]['date'] <= ds:
            f = flows_sorted[fi]
            if prev_point is None or f['date'] > prev_point:
                flow += f['amount']
                rate = _usd_at(usd, f['date'])
                if rate:
                    usd_flow += f['amount'] / rate
            fi += 1
        if prev_value and prev_value > 0 and value:
            ret = (value - flow) / prev_value - 1
        else:
            ret = None
        rate = _usd_at(usd, ds)
        usd_value = (value / rate) if (rate and value) else None
        if prev_usd and usd_value:
            usd_ret = (usd_value - usd_flow) / prev_usd - 1
        else:
            usd_ret = None
        monthly.append({'date': ds, 'total': round(value, 2), 'flow': round(flow, 2), 'ret': ret,
                        'usd_total': round(usd_value, 2) if usd_value else None, 'usd_ret': usd_ret})
        if value:
            prev_value = value
        elif value == 0:
            # Счёт закрыт/пуст (деньги выведены) — цепочку доходности начинаем
            # заново, иначе следующий год считается от устаревшей стоимости.
            prev_value = None
        if usd_value:
            prev_usd = usd_value
        prev_point = ds

    years = []
    by_year = {}
    for row in monthly:
        by_year.setdefault(row['date'][:4], []).append(row)

    # Дивиденды и купоны по годам (из отчётов)
    income_by_year = {}
    for inc in report_snapshots.all_income():
        year = inc['date'][:4]
        income_by_year[year] = income_by_year.get(year, 0) + inc['amount']

    # Операционный результат по годам: реализованная прибыль по сделкам
    # с учётом комиссий (по дате продажи)
    trading_by_year = {}
    try:
        from app.db import get_trade_lots
        for lot in get_trade_lots(None, None, None, 'all'):
            sell_date = (lot.get('sell_date') or '').strip()
            if len(sell_date) != 10:
                continue
            year = sell_date[6:10] if sell_date[2] == '.' else sell_date[:4]
            net = ((lot.get('profit') or 0)
                   - (lot.get('buy_fee') or 0) - (lot.get('sell_fee') or 0))
            trading_by_year[year] = trading_by_year.get(year, 0) + net
    except Exception:  # noqa: BLE001
        trading_by_year = {}

    # Вариационная маржа по фьючерсам — это тоже торговый результат (реализован
    # ежедневно), поэтому показываем её в колонке «Торговый результат», а не в
    # «прочем». Берём из cash_flow, куда её кладёт разбор отчётов ЕБС.
    try:
        from app.db import get_connection as _gconn
        _c = _gconn()
        _rows = _c.execute("SELECT date, COALESCE(credit,0) c, COALESCE(debit,0) d "
                           "FROM cash_flow WHERE description LIKE 'Вариационная маржа%'").fetchall()
        _c.close()
        for _r in _rows:
            _day = _r['date'] or ''
            _y = _day[6:10] if len(_day) == 10 and _day[2] == '.' else _day[:4]
            if len(_y) == 4 and _y.isdigit():
                trading_by_year[_y] = trading_by_year.get(_y, 0) + (_r['c'] - _r['d'])
    except Exception:  # noqa: BLE001
        pass

    prev_end = None
    prev_bench = last_on(start.isoformat(), benchmark)
    first_year = min(by_year) if by_year else None
    for year in sorted(by_year):
        rows = by_year[year]
        first, last = rows[0], rows[-1]
        # До первого снимка портфель считаем нулевым (счёт открывается с нуля),
        # иначе прирост самого первого года не посчитать.
        base = prev_end if prev_end is not None else (0.0 if year == first_year else None)
        deposits = round(dep_by_year.get(year, 0), 2)
        withdrawals = round(-wd_by_year.get(year, 0), 2)
        pnl = round(last['total'] - base - deposits + withdrawals, 2) if base is not None else None

        twr = 1.0
        counted = 0
        for r in rows:
            if r['ret'] is not None:
                twr *= (1 + r['ret'])
                counted += 1
        twr_pct = round((twr - 1) * 100, 2) if counted else None

        bench_from = prev_bench
        bench_to = last_on(last['date'], benchmark)
        bench_pct = round((bench_to / bench_from - 1) * 100, 2) if bench_from and bench_to else None
        if bench_to:
            prev_bench = bench_to

        rate = last_on(last['date'], usd)
        usd_value = round(last['total'] / rate, 2) if rate and last['total'] else None
        # Доходность в USD — цепочка месячных доходностей, где потоки взяты
        # по курсу на дату операции (не на конец периода).
        usd_chain = 1.0
        usd_counted = 0
        for r in rows:
            if r.get('usd_ret') is not None:
                usd_chain *= (1 + r['usd_ret'])
                usd_counted += 1
        usd_pct = round((usd_chain - 1) * 100, 2) if usd_counted else None
        income_year = round(income_by_year.get(year, 0), 2)
        trading_year = round(trading_by_year.get(year, 0), 2)
        # XIRR за год: старт года + датированные вводы/выводы + конец года
        year_flows = [(f['date'], f['amount']) for f in flows_sorted if f['date'][:4] == year]
        xirr_year = _year_xirr(f'{year}-01-01', base or 0.0, year_flows,
                               last['date'], last['total'] or 0.0)
        # Переоценка — то, что осталось от прироста после дохода и трейдинга
        # (изменение цен бумаг). Так «прирост = доход + трейдинг + переоценка».
        reval = round(pnl - income_year - trading_year, 2) if pnl is not None else None

        years.append({
            'year': year,
            'total': last['total'] or None,
            'deposits': deposits,
            'withdrawals': withdrawals,
            'income': income_year,
            'trading': trading_year,
            'reval': reval,
            'pnl': pnl,
            'twr': twr_pct,
            'xirr': xirr_year,
            'benchmark': bench_pct,
            'usd_rate': round(rate, 4) if rate else None,
            'usd_total': usd_value,
            'usd_pct': usd_pct,
            'snapshots': sum(1 for s in snaps if s['snapshot_date'][:4] == year),
        })
        if last['total'] is not None:
            prev_end = last['total']

    base_row = next((r for r in monthly if r['total']), None)
    base_bench = last_on(base_row['date'], benchmark) if base_row else None
    # lightweight-charts раскладывает точки равномерно по индексу, а не по
    # реальным датам: за 2022–2025 снимков по два в год, и год занимал столько
    # же места, сколько один месяц 2026-го. Поэтому для графика берём плотную
    # сетку «концы месяцев + даты снимков» (TWR считается по снимкам отдельно).
    grid = {s['snapshot_date'] for s in snaps} | {end.isoformat()}
    d = date(start.year, start.month, 1)
    while d <= end:
        nxt = date(d.year + (d.month == 12), (d.month % 12) + 1, 1)
        eom = nxt - timedelta(days=1)
        if start <= eom <= end:
            grid.add(eom.isoformat())
        d = nxt
    chart = []
    for ds in sorted(grid):
        if ds < start.isoformat():
            continue
        v = value_at(ds)
        b = last_on(ds, benchmark)
        chart.append({
            'date': ds,
            'total': round(v, 2) if v else None,
            'benchmark': round(b / base_bench * base_row['total'], 2)
            if (b and base_bench and base_row and base_row['total']) else None,
        })

    return {
        'years': years,
        'series': chart,
        'snapshots': sorted(snaps, key=lambda s: s['snapshot_date'], reverse=True),
        'benchmark': BENCHMARK,
        'usd': USD_INSTRUMENT,
        'transfers': transfers,
        'start': start.isoformat(),
        'end': end.isoformat(),
    }
