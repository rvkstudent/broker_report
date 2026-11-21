"""Математические модели инвестирования с прогнозируемым результатом.

Страница `/models` отвечает на вопрос «сколько я получу и с каким разбросом»,
а не «что купить». Модели намеренно разные по природе:

1. **Купонная машина** — детерминированная: если держать облигации до
   погашения и реинвестировать купоны, годовой результат равен IRR после
   налогов, и он известен заранее. Единственная модель с «прогнозом», а не
   распределением.
2. **Иммунизация (Редингтон)** — показывает, при какой дюрации результат
   перестаёт зависеть от сдвига ставок: ``D = H``. Если дюрация меньше
   горизонта, рост ставок бьёт по капиталу и не компенсируется.
3. **Таргетирование волатильности по индексу страха (RVI)** — правило размера
   риска ``w = σ*/RVI``: чем выше страх, тем меньше риска в акциях. Работает
   как регулятор, а не как «покупай на панике».
4. **Монте-Карло** — распределение результата (P10/P50/P90), чтобы «средняя
   доходность» не прятала хвосты.

Параметры по умолчанию взяты из данных, а не из головы: доходность облигаций —
IRR текущего портфеля после налогов, акции — исторические оценки MCFTR
(CAGR 7,85%, σ 23,8% за 2008–2026), корреляция MCFTR/RGBITR +0,45.
"""

from __future__ import annotations

import math
import random
import threading
import time
from datetime import date, timedelta

from app.bond_ladder import _get_json, _table, _num, _add_months
from app.db import get_connection

# Исторические оценки российского рынка (2008–2026, месячные данные MOEX)
EQ_MU = 0.105          # средняя арифметическая MCFTR, годовых
EQ_SD = 0.238          # σ MCFTR
BOND_SD = 0.060        # σ доходности облигационной части при D ≈ 4–5
CORR = 0.45            # корреляция MCFTR / RGBITR
TAX = 0.13
RVI_MIN_MEDIAN = 20.0  # ниже — «жадность», выше P75 — «страх»

_rvi_cache: dict = {'at': 0.0, 'data': None}
_rvi_lock = threading.Lock()


# ── индекс страха ───────────────────────────────────────────

def rvi_snapshot(force: bool = False) -> dict:
    """Текущий RVI и его место в истории (перцентиль, медиана, P25/P75/P90)."""
    with _rvi_lock:
        if not force and _rvi_cache['data'] and time.time() - _rvi_cache['at'] < 3600:
            return _rvi_cache['data']

        today = date.today()
        frm = (today - timedelta(days=365 * 3)).isoformat()
        # Эндпоинт истории отдаёт по 100 строк и параметр from игнорирует,
        # поэтому забираем страницами (start) и обрезаем по дате сами.
        values: list[tuple[float, str]] = []
        start = 0
        for _ in range(60):
            doc = _get_json('/history/engines/stock/markets/index/securities/RVI.json',
                            {'history.columns': 'TRADEDATE,CLOSE', 'start': start})
            rows = _table(doc, 'history')
            if not rows:
                break
            for r in rows:
                value = _num(r.get('CLOSE'))
                day = r.get('TRADEDATE') or ''
                if value and day >= frm:
                    values.append((value, day))
            start += len(rows)
            if len(rows) < 100:
                break
        if not values:
            data = {'available': False}
            _rvi_cache.update({'at': time.time(), 'data': data})
            return data

        values.sort(key=lambda x: x[1])
        series = [v for v, _ in values]
        cur = series[-1]
        ranked = sorted(series)
        def q(p: float) -> float:
            return ranked[int(p * (len(ranked) - 1))]
        data = {
            'available': True,
            'current': round(cur, 2),
            'date': values[-1][1],
            'median': round(q(0.5), 1),
            'p25': round(q(0.25), 1),
            'p75': round(q(0.75), 1),
            'p90': round(q(0.90), 1),
            'percentile': round(sum(1 for v in series if v <= cur) / len(series) * 100),
            'n': len(series),
        }
        data['regime'] = (
            'спокойствие' if data['percentile'] < 25 else
            'норма' if data['percentile'] < 75 else
            'страх' if data['percentile'] < 90 else 'паника')
        _rvi_cache.update({'at': time.time(), 'data': data})
        return data


# ── 1. купонная машина ──────────────────────────────────────

def _coupon_dates(matdate: str, freq: int, frm: date) -> list[date]:
    try:
        maturity = date.fromisoformat(matdate)
    except (TypeError, ValueError):
        return []
    if maturity <= frm or not freq:
        return []
    step = max(int(round(12 / freq)), 1)
    out, day = [], maturity
    while day > frm:
        out.append(day)
        day = _add_months(day, -step)
    return sorted(out)


def bond_cashflows(rows: list[dict], today: date | None = None) -> tuple[list[tuple[date, float]], float]:
    """Денежные потоки облигационной части с налогами: (потоки, стоимость)."""
    today = today or date.today()
    flows: list[tuple[date, float]] = []
    value = 0.0
    for row in rows:
        qty = row.get('qty') or 0
        price = row.get('value')  # стоимость позиции в рублях
        if not price or not qty:
            continue
        value += price
        flows.append((today, -price))
        freq = int(row.get('coupon_freq') or 0)
        coupon_rub = _coupon_per_payment(row)
        for day in _coupon_dates(row.get('matdate') or '', freq, today):
            flows.append((day, qty * coupon_rub * (1 - TAX)))
        nominal = (row.get('facevalue') or 1000.0) * qty
        if row.get('currency') not in (None, 'RUB'):
            fx = (price / (qty * (row.get('price_pct') or 100) / 100 * (row.get('facevalue') or 1000.0))
                  if row.get('price_pct') else 1.0)
            nominal *= fx
        gain = max(nominal - price, 0.0)
        flows.append((date.fromisoformat(row['matdate']), nominal - gain * TAX))
    return flows, value


def _coupon_per_payment(row: dict) -> float:
    """Купон за выплату в рублях: из % от номинала (для валютных — с курсом)."""
    pct = row.get('coupon_pct') or 0.0
    face = row.get('facevalue') or 1000.0
    per_year = face * pct / 100
    freq = int(row.get('coupon_freq') or 0) or 1
    rub = per_year / freq
    if row.get('currency') not in (None, 'RUB'):
        price = row.get('value') or 0
        qty = row.get('qty') or 1
        unit = (price / qty) if qty else 0
        pct_price = row.get('price_pct') or 0
        if pct_price:
            rub = price / qty * pct / pct_price      # доля купона к цене
    return rub


def xirr(flows: list[tuple[date, float]], today: date | None = None) -> float | None:
    """IRR по датам (акт/365)."""
    if not flows:
        return None
    today = today or date.today()
    lo, hi = -0.95, 3.0
    def pv(rate: float) -> float:
        return sum(cf / (1 + rate) ** ((d - today).days / 365.25) for d, cf in flows)
    if pv(lo) * pv(hi) > 0:
        return None
    for _ in range(200):
        mid = (lo + hi) / 2
        if pv(mid) > 0:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def coupon_machine(bond_rows: list[dict], horizon: int) -> dict:
    """Детерминированная модель: IRR после налогов и стоимость через horizon лет."""
    flows, value = bond_cashflows(bond_rows)
    rate = xirr(flows)
    gross_flows = [(d, cf if d == flows[0][0] else cf / (1 - TAX))
                   for d, cf in flows]
    gross = xirr(gross_flows)
    future = (value * (1 + rate) ** horizon) if (rate is not None and value) else None
    return {
        'value': round(value, 2),
        'irr_net': round(rate * 100, 2) if rate is not None else None,
        'irr_gross': round(gross * 100, 2) if gross is not None else None,
        'tax_drag': (round((gross - rate) * 100, 2)
                     if rate is not None and gross is not None else None),
        'future': round(future, 2) if future else None,
        'horizon': horizon,
        'next_flow': min((d.isoformat() for d, cf in flows if cf > 0 and d > date.today()),
                         default=''),
    }


# ── 2. иммунизация ──────────────────────────────────────────

def immunization(bond_rows: list[dict], horizon: int) -> dict:
    """Сколько потеряет портфель при сдвиге ставок, если дюрация ≠ горизонт."""
    value = sum((r.get('value') or 0) for r in bond_rows) or 0.0
    weight = sum((r.get('value') or 0) * (r.get('duration') or 0) for r in bond_rows)
    duration = (weight / value) if value else 0.0
    gap = duration - horizon
    shocks = []
    for dy in (-3, -2, -1, 1, 2, 3):
        # Классическая оценка первого порядка: цена падает на D×Δy, но купоны
        # весь горизонт H реинвестируются по новой ставке (+H×Δy). При D = H
        # слагаемые сокращаются — это и есть иммунизация.
        effect = -duration * dy / 100
        reinvest = horizon * dy / 100
        shocks.append({'dy': dy,
                       'price_pct': round(effect * 100, 1),
                       'total_pct': round((effect + reinvest) * 100, 1),
                       'rub': round(value * effect, 0)})
    return {
        'value': round(value, 2),
        'duration': round(duration, 2),
        'horizon': horizon,
        'gap': round(gap, 2),
        'immunized': abs(gap) <= 0.5,
        'shocks': shocks,
        'advice': (f'дюрация {duration:.1f} при горизонте {horizon} — '
                   f'{"иммунизировано" if abs(gap) <= 0.5 else ("удлинить" if gap < 0 else "укоротить")} '
                   f'на {abs(gap):.1f} года'),
    }


# ── 3. индекс страха как регулятор риска ────────────────────

def vol_target(rvi: dict, sigma_star: float, cap: float = 0.25) -> dict:
    """Доля рисковой части: w = min(cap, σ*/RVI)."""
    if not rvi.get('available'):
        return {'available': False}
    current = rvi['current']
    raw = (sigma_star / 100) / (current / 100) if current else 0.0
    weight = min(cap, max(0.0, raw))
    table = []
    for label, level in (('P25 (спокойствие)', rvi['p25']),
                         ('медиана', rvi['median']),
                         ('P75', rvi['p75']),
                         ('P90', rvi['p90']),
                         ('паника 60', 60.0)):
        table.append({'label': label, 'rvi': round(level, 1),
                      'weight': round(min(cap, max(0.0, (sigma_star / 100) / (level / 100))) * 100, 1)})
    return {
        'available': True,
        'sigma_star': sigma_star,
        'cap': cap * 100,
        'raw': round(raw * 100, 1),
        'weight': round(weight * 100, 1),
        'capped': raw > cap,
        'table': table,
    }


# ── 4. Монте-Карло ──────────────────────────────────────────

def monte_carlo(start: float, equity_share: float, bond_mu: float,
                years: int = 15, paths: int = 4000, seed: int = 42) -> dict:
    """Распределение капитала через `years` лет (нормальные годовые доходности)."""
    if not start:
        return {'available': False}
    rnd = random.Random(seed)
    w = max(0.0, min(1.0, equity_share))
    finals: list[float] = []
    for _ in range(paths):
        value = start
        for _ in range(years):
            z1 = rnd.gauss(0, 1)
            z2 = CORR * z1 + math.sqrt(1 - CORR * CORR) * rnd.gauss(0, 1)
            r_eq = EQ_MU + EQ_SD * z1
            r_bd = bond_mu + BOND_SD * z2
            value *= 1 + w * r_eq + (1 - w) * r_bd
        finals.append(value)
    finals.sort()
    def q(p: float) -> float:
        return finals[int(p * (len(finals) - 1))]
    cagrs = sorted((v / start) ** (1 / years) - 1 for v in finals)
    def qc(p: float) -> float:
        return cagrs[int(p * (len(cagrs) - 1))]
    return {
        'available': True,
        'paths': paths,
        'years': years,
        'equity_pct': round(w * 100, 1),
        'p10': round(q(0.1), 0),
        'p50': round(q(0.5), 0),
        'p90': round(q(0.9), 0),
        'cagr_p10': round(qc(0.1) * 100, 1),
        'cagr_p50': round(qc(0.5) * 100, 1),
        'cagr_p90': round(qc(0.9) * 100, 1),
        'p_target': round(sum(1 for c in cagrs if c >= 0.30) / len(cagrs) * 100, 1),
        'target': 30,
    }


def kelly(bond_mu: float) -> dict:
    """Доля акций по Келли: f* = (μ_акций − μ_облигаций) / σ²_акций."""
    excess = EQ_MU - bond_mu
    f = excess / (EQ_SD ** 2)
    return {
        'excess': round(excess * 100, 1),
        'full': round(f * 100, 1),
        'half': round(f * 50, 1),
        'positive': f > 0,
    }


# ── сборка для страницы ─────────────────────────────────────

def total_capital() -> float:
    """Стоимость портфеля по последнему снимку отчётов (как на дашборде)."""
    try:
        conn = get_connection()
        row = conn.execute("""
            SELECT SUM(p.value_end) AS v
            FROM portfolio p
            JOIN (SELECT MAX(report_id) AS rid FROM portfolio) m ON m.rid = p.report_id
        """).fetchone()
        conn.close()
        if row and row['v']:
            return float(row['v'])
    except Exception:  # noqa: BLE001
        pass
    return 0.0


def build(horizon: int = 15, sigma_star: float = 15.0, equity_pct: float = 10.0,
          paths: int = 4000) -> dict:
    from app import bond_ladder

    try:
        ladder = bond_ladder.build()
    except Exception:  # noqa: BLE001
        ladder = {'rows': [], 'bond_value': 0, 'duration_avg': None, 'ytm_avg': None}

    bonds = [r for r in ladder.get('rows') or [] if r.get('value')]
    machine = coupon_machine(bonds, horizon)
    imm = immunization(bonds, horizon)
    rvi = rvi_snapshot()
    vt = vol_target(rvi, sigma_star) if rvi.get('available') else {'available': False}

    bond_mu = (machine['irr_net'] or 0) / 100 or 0.14
    mc = monte_carlo(machine['value'] or total_capital(), equity_pct / 100,
                     bond_mu, years=horizon, paths=max(200, min(paths, 20000)))
    return {
        'machine': machine,
        'immunization': imm,
        'rvi': rvi,
        'vol_target': vt,
        'monte_carlo': mc,
        'kelly': kelly(bond_mu),
        'bond_mu': round(bond_mu * 100, 2),
        'params': {'horizon': horizon, 'sigma_star': sigma_star,
                   'equity_pct': equity_pct, 'paths': mc.get('paths')},
        'bond_metrics': {'value': ladder.get('bond_value'),
                         'duration': ladder.get('duration_avg'),
                         'ytm': ladder.get('ytm_avg')},
        'as_of': date.today().isoformat(),
    }
