"""Лестница погашений и дюрация облигационной части портфеля.

Даты погашения, купоны, биржевая YTM и дюрация берутся из MOEX ISS — открытые
данные, без токена и регистрации. Справочник кэшируется в таблице ``bond_ref``:
ISS опрашивается только для новых бумаг и записей старше TTL, поэтому страница
читает кэш и не зависит от сети при каждом открытии.

Зачем это нужно портфелю: купонная доходность («сколько платят в год») не
отвечает на вопрос «когда вернутся деньги и что будет с ценой при сдвиге
ставок». Лестница показывает, в какие годы погашаются выпуски (риск
реинвестирования), а дюрация — на сколько процентов сдвинется стоимость
облигационной части при изменении ставок на 1 п.п.
"""

from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from datetime import date, datetime

from app.db import get_connection

ISS = 'https://iss.moex.com/iss'
TTL = 6 * 3600             # секунд: как часто перезапрашиваем ISS
CONCENTRATION_WARN = 30.0  # % облигационной части в одном годе погашения
TAX_RATE = 0.13            # НДФЛ с купона
USD_CODE = 'USD000UTSTOM'  # курс USD для валютных выпусков
_TIMEOUT = 4.0
_UA = {'User-Agent': 'BrokerReport/1.0 (bond-ladder)'}

_last_sync = 0.0
_SYNC_COOLDOWN = 600.0  # сек: не долбим ISS на каждом запросе, если он недоступен


# ── сеть: MOEX ISS ──────────────────────────────────────────

def _get_json(path: str, params: dict | None = None) -> dict | None:
    url = ISS + path + '?iss.meta=off'
    if params:
        url += '&' + urllib.parse.urlencode(params)
    try:
        req = urllib.request.Request(url, headers=_UA)
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            return json.loads(resp.read().decode('utf-8'))
    except Exception:  # noqa: BLE001 — сеть не должна ломать страницу
        return None


def _table(doc: dict | None, key: str) -> list[dict]:
    block = (doc or {}).get(key) or {}
    cols = block.get('columns') or []
    return [dict(zip(cols, row)) for row in block.get('data') or []]


def _find_on_moex(code: str) -> dict | None:
    """Строка поиска ISS по ISIN или коду QUIK (SU26241RMFS8, 26241RMFS)."""
    key = (code or '').strip().upper()
    if not key:
        return None
    rows = _table(_get_json('/securities.json', {'q': key}), 'securities')
    if not rows:
        return None
    exact = [r for r in rows
             if key in {str(r.get('secid') or '').upper(),
                        str(r.get('isin') or '').upper(),
                        str(r.get('regnumber') or '').upper()}]
    for row in exact or rows:
        if 'bond' in str(row.get('group') or ''):
            return row
    return (exact or rows)[0]


def _fetch_ref(code: str) -> dict | None:
    """Справочник + биржевые метрики одной бумаги из ISS."""
    hit = _find_on_moex(code)
    if not hit:
        return None
    secid = hit.get('secid') or ''
    desc = {r.get('name'): r.get('value')
            for r in _table(_get_json(f'/securities/{secid}.json'), 'description')}
    if not desc:
        return None

    group = str(desc.get('GROUP') or '')
    is_bond = 'bond' in group
    ref = {
        'code': code,
        'secid': secid,
        'isin': desc.get('ISIN') or hit.get('isin') or '',
        'name': desc.get('SHORTNAME') or hit.get('shortname') or code,
        'kind': 'bond' if is_bond else 'other',
        'currency': _currency(desc.get('FACEUNIT')),
        'matdate': desc.get('MATDATE') or '',
        'offerdate': desc.get('OFFERDATE') or '',
        'coupon_pct': _num(desc.get('COUPONPERCENT')),
        'coupon_value': _num(desc.get('COUPONVALUE')),
        'coupon_freq': int(_num(desc.get('COUPONFREQUENCY')) or 1),
        'facevalue': _num(desc.get('FACEVALUE')),
        'board': hit.get('primary_boardid') or '',
        'price_pct': None,
        'ytm': None,
        'duration_days': None,
        'updated_at': _now(),
    }
    if is_bond and ref['board']:
        rows = _table(_get_json(
            f'/engines/stock/markets/bonds/boards/{ref["board"]}/securities/{secid}.json'),
            'marketdata')
        row = next((r for r in rows if r.get('LAST')), rows[0] if rows else {})
        ref['price_pct'] = _num(row.get('LAST')) or _num(row.get('LCURRENTPRICE'))
        ref['ytm'] = _num(row.get('YIELD'))
        ref['duration_days'] = _num(row.get('DURATION'))
    return ref


def _currency(face_unit) -> str:
    unit = str(face_unit or '').upper()
    if unit in ('SUR', 'RUB', ''):
        return 'RUB'
    return unit


def _num(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _now() -> str:
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


# ── кэш справочника ─────────────────────────────────────────

_COLUMNS = ('code', 'secid', 'isin', 'name', 'kind', 'currency', 'matdate',
            'offerdate', 'coupon_pct', 'coupon_value', 'coupon_freq', 'facevalue',
            'board', 'price_pct', 'ytm', 'duration_days', 'updated_at')


def _load_cache() -> dict[str, dict]:
    try:
        conn = get_connection()
        rows = conn.execute(
            f'SELECT {", ".join(_COLUMNS)} FROM bond_ref').fetchall()
        conn.close()
    except Exception:  # noqa: BLE001 — таблицы может не быть до init_db
        return {}
    return {(r['code'] or '').upper(): dict(r) for r in rows}


def _save_ref(ref: dict) -> None:
    try:
        conn = get_connection()
        conn.execute(
            f'INSERT OR REPLACE INTO bond_ref ({", ".join(_COLUMNS)}) '
            f'VALUES ({", ".join("?" for _ in _COLUMNS)})',
            tuple(ref.get(c) for c in _COLUMNS))
        conn.commit()
        conn.close()
    except Exception:  # noqa: BLE001
        pass


def _stale(ref: dict | None) -> bool:
    if not ref or not ref.get('updated_at'):
        return True
    try:
        age = datetime.now() - datetime.strptime(ref['updated_at'], '%Y-%m-%d %H:%M:%S')
    except ValueError:
        return True
    return age.total_seconds() > TTL


def refresh(codes: list[str], force: bool = False) -> dict[str, dict]:
    """Обновить справочник: сеть трогаем только для новых/устаревших кодов."""
    cached = _load_cache()
    for code in codes:
        key = (code or '').upper()
        if not key or (not force and not _stale(cached.get(key))):
            continue
        ref = _fetch_ref(key)
        if ref:
            _save_ref(ref)
    return _load_cache()


# ── расчёт доходности и дюрации по графику купонов ──────────

def _add_months(day: date, months: int) -> date:
    month = day.month - 1 + months
    year = day.year + month // 12
    month = month % 12 + 1
    for d in (day.day, 30, 29, 28):
        try:
            return date(year, month, min(d, day.day))
        except ValueError:
            continue
    return date(year, month, 28)


def _cashflows(matdate: str, coupon_pct: float, freq: int,
               as_of: date) -> list[tuple[date, float]]:
    """Будущие платежи (дата, % от номинала): купоны + погашение.

    Даты строим назад от погашения шагом 12/freq месяцев — так же, как устроен
    график у эмитента; иначе YTM считалась бы «по сетке от сегодня», с
    систематической ошибкой в полпроцента.
    """
    try:
        maturity = date.fromisoformat(matdate)
    except (TypeError, ValueError):
        return []
    if maturity <= as_of:
        return []
    f = max(int(freq or 1), 1)
    step = max(int(round(12 / f)), 1)
    per_coupon = (coupon_pct or 0.0) / f
    dates = []
    day = maturity
    while day > as_of:
        dates.append(day)
        day = _add_months(day, -step)
    dates.reverse()
    flows = [(d, per_coupon) for d in dates]
    flows[-1] = (flows[-1][0], flows[-1][1] + 100.0)
    return flows


def _accrued_pct(matdate: str, coupon_pct: float, freq: int,
                 as_of: date) -> float:
    """НКД в % от номинала: доля текущего купонного периода."""
    try:
        maturity = date.fromisoformat(matdate)
    except (TypeError, ValueError):
        return 0.0
    f = max(int(freq or 1), 1)
    step = max(int(round(12 / f)), 1)
    next_day = maturity
    while next_day > as_of:
        prev_day = _add_months(next_day, -step)
        if prev_day <= as_of:
            period = (next_day - prev_day).days or 1
            elapsed = (as_of - prev_day).days
            return (coupon_pct or 0.0) / f * elapsed / period
        next_day = prev_day
    return 0.0


def ytm_from_price(price_pct: float, coupon_pct: float, freq: int,
                   matdate: str, as_of: date | None = None) -> float | None:
    """YTM из «чистой» цены в % от номинала (запасной расчёт, если нет биржевой)."""
    as_of = as_of or date.today()
    flows = _cashflows(matdate, coupon_pct, freq, as_of)
    if not price_pct or not flows:
        return None
    dirty = price_pct + _accrued_pct(matdate, coupon_pct, freq, as_of)

    def pv(y: float) -> float:
        return sum(amount / (1 + y) ** ((day - as_of).days / 365.25)
                   for day, amount in flows)

    lo, hi = -0.95, 5.0
    for _ in range(200):
        mid = (lo + hi) / 2
        if pv(mid) > dirty:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def duration_from_price(price_pct: float, coupon_pct: float, freq: int,
                        matdate: str, as_of: date | None = None) -> float | None:
    """Дюрация Маколея в годах по графику купонов."""
    as_of = as_of or date.today()
    y = ytm_from_price(price_pct, coupon_pct, freq, matdate, as_of)
    flows = _cashflows(matdate, coupon_pct, freq, as_of)
    if y is None or not flows:
        return None
    pvs = [(day - as_of).days / 365.25 for day, _ in flows]
    values = [amount / (1 + y) ** t for (_, amount), t in zip(flows, pvs)]
    total = sum(values)
    return sum(t * v for t, v in zip(pvs, values)) / total if total else None


# ── позиции портфеля ────────────────────────────────────────

def _positions() -> list[dict]:
    """Открытые позиции — из сделок (LIFO), как в блоке «Текущий портфель».

    Не из ``portfolio``: строки снимков отчётов приходят из репликации
    размноженными (одна позиция ×16) и покрывают только один счёт — валютные
    выпуски в последнем отчёте могут отсутствовать. Сделочная сводка даёт
    фактический остаток по всем счетам.
    """
    from app.db import get_instrument_summary
    try:
        raw = get_instrument_summary(None, '', '', 'all') or []
    except Exception:  # noqa: BLE001
        return []
    out = []
    for pos in raw:
        code = (pos.get('security_code') or '').strip().upper()
        if not code or not pos.get('qty'):
            continue
        out.append({'code': code,
                    'name': pos.get('security_name') or code,
                    'qty': float(pos['qty'])})
    return out


def _ref_for(code: str, refs: dict[str, dict]) -> dict:
    """Справочник по любому из кодов бумаги.

    В кэше запись хранится под тем кодом, по которому её спросили, а позиция
    приходит то ISIN-ом (RU000A106E90), то кодом QUIK/биржи (SU26243RMFS4,
    26254RMFS) — это одна и та же бумага, и искать надо по всем трём.
    """
    key = (code or '').upper()
    if not key:
        return {}
    exact = refs.get(key)
    if exact:
        return exact
    for ref in refs.values():
        if key in (str(ref.get('isin') or '').upper(),
                   str(ref.get('secid') or '').upper(),
                   str(ref.get('code') or '').upper()):
            return ref
    return {}


def _cached_prices() -> dict[str, float]:
    """Рублёвая цена за единицу из кэша цен (QUIK/T-Invest)."""
    try:
        conn = get_connection()
        rows = conn.execute(
            'SELECT sec_code, price FROM current_price WHERE price > 0').fetchall()
        conn.close()
    except Exception:  # noqa: BLE001
        return {}
    return {(r['sec_code'] or '').upper(): float(r['price']) for r in rows}


def _usd_rate() -> float | None:
    """Курс USD для валютных выпусков: кэш цен, иначе котировка MOEX."""
    cached = _cached_prices().get(USD_CODE)
    if cached:
        return cached
    rows = _table(_get_json(f'/engines/currency/markets/selt/boards/CETS'
                            f'/securities/{USD_CODE}.json'), 'marketdata')
    for row in rows:
        price = _num(row.get('LAST')) or _num(row.get('LCURRENTPRICE'))
        if price:
            return price
    return None


def _looks_like_bond(ref: dict, code: str) -> bool:
    if ref.get('kind'):
        return ref['kind'] == 'bond'
    name = str(ref.get('name') or '').upper()
    return 'ОФЗ' in name or 'ОБЛИГ' in name or name.startswith('SU26') or code.startswith('SU26')


def _kind_label(name: str, ref: dict) -> str:
    if str(ref.get('currency') or 'RUB') != 'RUB':
        return 'Валютная'
    if 'ОФЗ' in str(name or '').upper():
        return 'ОФЗ'
    return 'Корпоративная'


def _years_left(matdate: str, as_of: date) -> float | None:
    try:
        return max((date.fromisoformat(matdate) - as_of).days / 365.25, 0.0)
    except (TypeError, ValueError):
        return None


def _build_rows(positions: list[dict], refs: dict[str, dict],
                prices: dict[str, float], price_src: str, as_of: date,
                usd_rate: float | None) -> list[dict]:
    """Строки лестницы: позиции + справочник ISS + цены из конвейера QUIK/T-Invest.

    Цена в ISS бесплатная, но с задержкой, поэтому стоимость считается по
    ``prices`` (тот же источник, что у дашборда: QUIK, если свежий, иначе
    T-Invest). ISS-котировка используется только если цены нет вовсе — тогда
    это помечается в строке (``price_src='iss'``).
    """
    merged: dict[str, dict] = {}
    for pos in positions:
        code = pos['code']
        ref = _ref_for(code, refs)
        if not _looks_like_bond(ref, code):
            continue
        # Одна бумага может прийти двумя кодами (26254RMFS и RU000A10D533).
        key = str(ref.get('isin') or code).upper()
        row = merged.get(key)
        if row:
            row['qty'] += pos['qty']
            continue
        merged[key] = {'code': key, 'name': pos.get('name') or code,
                       'qty': pos['qty'], 'ref': ref, 'codes': [code]}

    fx = usd_rate or None
    rows = []
    for item in merged.values():
        ref = item['ref']
        currency = str(ref.get('currency') or 'RUB').upper()
        need_fx = currency != 'RUB'
        if need_fx and not fx:
            continue  # без курса валютный выпуск не посчитать — не выдумываем

        face = _num(ref.get('facevalue')) or 1000.0
        # Курс применяем только к валютному номиналу: для рублёвых бумаг
        # множитель строго 1, иначе стоимость умножается на курс USD.
        rate = (fx or 1.0) if need_fx else 1.0
        unit_rub = None
        src_used = None
        for code in item['codes']:
            price = prices.get(code)
            if price:
                unit_rub, src_used = float(price), (price_src or 'market')
                break
        # Цена из кэша/API — за единицу в рублях, поэтому для валютных бумаг
        # она уже содержит курс и пересчёт не нужен.
        if unit_rub is None and _num(ref.get('price_pct')):
            unit_rub = face * _num(ref['price_pct']) / 100 * rate
            src_used = 'iss'

        value = item['qty'] * unit_rub if unit_rub else None
        # Цена в % от номинала — из фактической рыночной цены, чтобы YTM
        # считался по той же цене, что и стоимость; ISS — только запасной путь.
        if unit_rub and src_used != 'iss':
            price_pct = unit_rub / face / rate * 100
        else:
            price_pct = _num(ref.get('price_pct'))
        coupon_value = _num(ref.get('coupon_value'))
        coupon_year = (item['qty'] * coupon_value * int(ref.get('coupon_freq') or 0)
                       * rate) if coupon_value else None

        # YTM считаем по фактической цене — тогда карточка согласована сама с
        # собой; биржевое значение из ISS берём, только если цены нет.
        ytm_pct = None
        if unit_rub and price_pct:
            local = ytm_from_price(price_pct, _num(ref.get('coupon_pct')) or 0,
                                   int(ref.get('coupon_freq') or 1),
                                   ref.get('matdate') or '', as_of)
            ytm_pct = local * 100 if local is not None else _num(ref.get('ytm'))
        else:
            ytm_pct = _num(ref.get('ytm'))

        duration_days = _num(ref.get('duration_days'))
        duration = duration_days / 365.25 if duration_days else None
        if duration is None and price_pct:
            duration = duration_from_price(price_pct, _num(ref.get('coupon_pct')) or 0,
                                           int(ref.get('coupon_freq') or 1),
                                           ref.get('matdate') or '', as_of)

        years = _years_left(ref.get('matdate'), as_of)
        # В сделках имя иногда — сам код (SU26243RMFS4); показываем человеческое
        # название из справочника, если оно есть.
        name = item['name']
        if ref.get('name') and name.upper() in {c.upper() for c in item['codes']}:
            name = ref['name']
        rows.append({
            'code': item['code'],
            'name': name,
            'kind': _kind_label(name, ref),
            'currency': currency,
            'qty': item['qty'],
            'price_pct': round(price_pct, 3) if price_pct else None,
            'coupon_pct': _num(ref.get('coupon_pct')),
            'coupon_freq': int(ref.get('coupon_freq') or 0),
            'matdate': ref.get('matdate') or '',
            'years': round(years, 1) if years is not None else None,
            'ytm': round(ytm_pct, 2) if ytm_pct is not None else None,
            'duration': round(duration, 2) if duration else None,
            'value': round(value, 2) if value else None,
            'coupon_year': round(coupon_year, 2) if coupon_year else None,
            'price_src': src_used,
            'ref_ok': bool(ref.get('matdate')),
        })
    rows.sort(key=lambda r: (r['matdate'] or '9999', -(r['value'] or 0)))
    return rows


def summarize(rows: list[dict], total: float | None = None) -> dict:
    """Сводка по строкам портфеля: доли, дюрация, лестница погашений."""
    bond_value = sum(r['value'] for r in rows if r.get('value'))
    denom = bond_value or 0.0
    for row in rows:
        value = row.get('value') or 0
        row['pct'] = round(value / denom * 100, 1) if denom else 0.0

    def weighted(field: str) -> float | None:
        pairs = [(r['value'], r[field]) for r in rows
                 if r.get('value') and r.get(field) is not None]
        weight = sum(v for v, _ in pairs)
        return sum(v * f for v, f in pairs) / weight if weight else None

    ytm_avg = weighted('ytm')
    duration_avg = weighted('duration')
    coupon_year = sum(r['coupon_year'] for r in rows if r.get('coupon_year'))

    ladder: list[dict] = []
    by_year: dict[str, float] = {}
    for row in rows:
        year = (row.get('matdate') or '')[0:4] or '—'
        by_year[year] = by_year.get(year, 0.0) + (row.get('value') or 0)
    for year in sorted(by_year):
        value = by_year[year]
        ladder.append({'year': year, 'value': round(value, 2),
                       'pct': round(value / denom * 100, 1) if denom else 0.0})

    max_year = max(ladder, key=lambda b: b['value']) if ladder else None
    return {
        'rows': rows,
        'ladder': ladder,
        'bond_value': round(bond_value, 2),
        'bonds_pct': round(bond_value / total * 100, 1) if total else None,
        'ytm_avg': round(ytm_avg, 2) if ytm_avg is not None else None,
        'duration_avg': round(duration_avg, 2) if duration_avg is not None else None,
        # Сколько рублей стоимости сдвинет изменение ставок на 1 п.п.
        'dv01': round(sum((r['value'] or 0) * (r['duration'] or 0)
                          for r in rows) / 100, 2),
        'coupon_year': round(coupon_year, 2),
        'coupon_month': round(coupon_year / 12, 2) if coupon_year else 0.0,
        'coupon_year_net': round(coupon_year * (1 - TAX_RATE), 2),
        'max_year': max_year,
        'warn': bool(max_year and max_year['pct'] > CONCENTRATION_WARN),
        'next_maturity': next((r['matdate'] for r in rows if r.get('matdate')), ''),
        'fx_missing': any(r.get('fx_missing') for r in rows),
        'pending': any(not r['ref_ok'] for r in rows),
    }


def build(total: float | None = None, as_of: date | None = None,
          prices: dict[str, float] | None = None, price_source: str = '') -> dict:
    """Лестница погашений и дюрация по текущим позициям портфеля.

    ``prices`` — ``{sec_code: цена за единицу в рублях}`` из общего конвейера
    цен (QUIK → T-Invest), как у дашборда. Если не переданы, стоимость берётся
    из ISS — это запасной путь, поэтому в строках будет ``price_src='iss'``.
    """
    as_of = as_of or date.today()
    positions = _positions()
    codes = [p['code'] for p in positions]

    refs = _load_cache()
    missing = [c for c in codes if _stale(_ref_for(c, refs))]
    if missing:
        # Обновляем синхронно: справочник нужен для расчёта на этой же
        # странице. Защита от недоступного ISS — пауза _SYNC_COOLDOWN, чтобы
        # не ждать сеть на каждом открытии.
        global _last_sync
        now = time.monotonic()
        if now - _last_sync > _SYNC_COOLDOWN:
            _last_sync = now
            refresh(missing)
        refs = _load_cache()

    rows = _build_rows(positions, refs, prices or {}, price_source, as_of, _usd_rate())
    data = summarize(rows, total)
    data['as_of'] = as_of.isoformat()
    data['price_source'] = price_source
    data['updated_at'] = next((r.get('updated_at') for r in refs.values()
                               if r.get('updated_at')), '')
    return data
