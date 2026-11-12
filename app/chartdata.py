"""Данные для графика инструмента: свечи из минуток, дельта, VWAP, маркеры.

Свечи собираются из `market_bar_1m` (срок хранения — год), поэтому график
строится и за дни, по которым сырых сделок уже нет. Маркеры сделок берутся из
`market_trade` и доступны только за срок хранения тика.

Время в ответах — **московское**, закодированное как UNIX-метка: фронтенд
рисует его без сдвига, поэтому подписи оси совпадают с временем биржи.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from app.screener import MINUTE_NS, MSK, get_connection

# Сколько точек отдаём максимум. Месяц минутных свечей — это ~25 тыс. баров,
# браузер такое отрисует, но по сети это лишние мегабайты.
MAX_BARS = 20000
MAX_MARKERS = 3000

INTERVALS = (1, 5, 15, 60)


class ChartError(ValueError):
    """Некорректный запрос к данным графика."""


def _to_chart_time(ns: int) -> int:
    """Наносекунды UTC → секунды «МСК как UTC» для оси графика."""
    return ns // 1_000_000_000 + 3 * 3600


def instrument(ticker: str) -> dict | None:
    conn = get_connection()
    try:
        row = conn.execute(
            'SELECT instrument_uid, ticker, name, class_code, lot, avg_daily_value '
            'FROM screener_instrument WHERE ticker = ? COLLATE NOCASE',
            (ticker.strip(),)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def data_range(ticker: str) -> dict:
    """Что вообще есть по инструменту: бары и сырые сделки."""
    conn = get_connection()
    try:
        row = conn.execute(
            '''SELECT MIN(minute_ns) AS first_bar, MAX(minute_ns) AS last_bar,
                      COUNT(*) AS bars FROM market_bar_1m WHERE ticker = ? COLLATE NOCASE''',
            (ticker.strip(),)).fetchone()
        trades = conn.execute(
            '''SELECT MIN(ts_ns) AS first_trade, MAX(ts_ns) AS last_trade, COUNT(*) AS n
               FROM market_trade WHERE ticker = ? COLLATE NOCASE''',
            (ticker.strip(),)).fetchone()
    finally:
        conn.close()
    return {
        'first_bar': row['first_bar'],
        'last_bar': row['last_bar'],
        'bars': row['bars'] or 0,
        'first_trade': trades['first_trade'],
        'last_trade': trades['last_trade'],
        'trades': trades['n'] or 0,
    }


def _load_minutes(ticker: str, from_ns: int, to_ns: int) -> list[dict]:
    conn = get_connection()
    try:
        rows = conn.execute(
            '''SELECT minute_ns, open, high, low, close, vwap, volume, turnover,
                      buy_volume, sell_volume, buy_turnover, sell_turnover, trades,
                      large_buy_turnover, large_sell_turnover
               FROM market_bar_1m
               WHERE ticker = ? COLLATE NOCASE AND minute_ns >= ? AND minute_ns < ?
               ORDER BY minute_ns LIMIT ?''',
            (ticker.strip(), from_ns, to_ns, MAX_BARS + 1)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _resample(minutes: list[dict], step: int) -> list[dict]:
    """Склеить минутки в бары по step минут."""
    if step <= 1:
        return minutes
    buckets: dict[int, dict] = {}
    for bar in minutes:
        key = bar['minute_ns'] // (step * MINUTE_NS)
        agg = buckets.get(key)
        if agg is None:
            buckets[key] = {
                'minute_ns': key * step * MINUTE_NS,
                'open': bar['open'], 'high': bar['high'], 'low': bar['low'],
                'close': bar['close'],
                'volume': bar['volume'] or 0, 'turnover': bar['turnover'] or 0,
                'buy_volume': bar['buy_volume'] or 0, 'sell_volume': bar['sell_volume'] or 0,
                'buy_turnover': bar['buy_turnover'] or 0,
                'sell_turnover': bar['sell_turnover'] or 0,
                'trades': bar['trades'] or 0,
                'large_buy_turnover': bar['large_buy_turnover'] or 0,
                'large_sell_turnover': bar['large_sell_turnover'] or 0,
            }
        else:
            agg['high'] = max(agg['high'], bar['high'])
            agg['low'] = min(agg['low'], bar['low'])
            agg['close'] = bar['close']
            for field in ('volume', 'turnover', 'buy_volume', 'sell_volume',
                          'buy_turnover', 'sell_turnover', 'trades',
                          'large_buy_turnover', 'large_sell_turnover'):
                agg[field] += bar[field] or 0
    return [buckets[key] for key in sorted(buckets)]


def _fill_session_vwap(bars: list[dict]) -> None:
    """Кумулятивная VWAP от начала торгового дня — в каждом баре."""
    day = None
    cum_volume = cum_turnover = 0.0
    for bar in bars:
        local_day = (bar['minute_ns'] // 1_000_000_000 + 3 * 3600) // 86400
        if local_day != day:
            day, cum_volume, cum_turnover = local_day, 0.0, 0.0
        cum_volume += bar['volume'] or 0
        cum_turnover += bar['turnover'] or 0
        bar['session_vwap'] = (cum_turnover / cum_volume) if cum_volume else 0.0


def prepared(ticker: str, from_ns: int, to_ns: int, interval: int = 1) -> dict:
    """Свечи с полями для анализа и VWAP — общая точка для графика и бэктеста."""
    if interval not in INTERVALS:
        raise ChartError(f'Интервал должен быть одним из {INTERVALS}')
    inst = instrument(ticker)
    if inst is None:
        raise ChartError(f'Инструмент {ticker} не найден в универсуме')

    minutes = _load_minutes(ticker, from_ns, to_ns)
    truncated = len(minutes) > MAX_BARS
    if truncated:
        minutes = minutes[-MAX_BARS:]
    bars = _resample(minutes, interval)
    _fill_session_vwap(bars)
    return {'instrument': inst, 'interval': interval, 'bars': bars, 'truncated': truncated}


def series(ticker: str, from_ns: int, to_ns: int, interval: int = 1) -> dict:
    """Свечи, объём, дельта и VWAP для графика."""
    data = prepared(ticker, from_ns, to_ns, interval)
    candles, volume, delta, vwap = [], [], [], []
    for bar in data['bars']:
        t = _to_chart_time(bar['minute_ns'])
        candles.append({'time': t, 'open': bar['open'], 'high': bar['high'],
                        'low': bar['low'], 'close': bar['close']})
        volume.append({'time': t, 'value': bar['volume'],
                       'color': '#26a69a66' if bar['close'] >= bar['open'] else '#ef535066'})
        delta_value = (bar['buy_turnover'] or 0) - (bar['sell_turnover'] or 0)
        delta.append({'time': t, 'value': round(delta_value, 2),
                      'color': '#26a69a' if delta_value >= 0 else '#ef5350'})
        if bar['session_vwap']:
            vwap.append({'time': t, 'value': round(bar['session_vwap'], 4)})

    summary = {
        'last_price': candles[-1]['close'] if candles else 0.0,
        'first_price': candles[0]['open'] if candles else 0.0,
        'turnover': round(sum(bar['turnover'] or 0 for bar in data['bars']), 2),
        'volume': int(sum(bar['volume'] or 0 for bar in data['bars'])),
        'buy_volume': int(sum(bar['buy_volume'] or 0 for bar in data['bars'])),
        'sell_volume': int(sum(bar['sell_volume'] or 0 for bar in data['bars'])),
        'bars': len(candles),
    }
    if summary['first_price']:
        summary['change_pct'] = round(
            (summary['last_price'] - summary['first_price']) / summary['first_price'] * 100, 2)
    else:
        summary['change_pct'] = 0.0

    return {'instrument': data['instrument'], 'interval': interval,
            'candles': candles, 'volume': volume, 'delta': delta, 'vwap': vwap,
            'truncated': data['truncated'], 'summary': summary}


def markers(ticker: str, from_ns: int, to_ns: int, min_value: float = 1_000_000.0,
            mode: str = 'large') -> dict:
    """Маркеры сделок для графика.

    mode='large' — только крупные принты (от min_value);
    mode='all'   — все сделки, но не больше MAX_MARKERS (берём самые крупные).
    """
    conn = get_connection()
    try:
        if mode == 'all':
            rows = conn.execute(
                '''SELECT ts_ns, price, lots, shares, value, side FROM market_trade
                   WHERE ticker = ? COLLATE NOCASE AND ts_ns >= ? AND ts_ns < ?
                   ORDER BY value DESC LIMIT ?''',
                (ticker.strip(), from_ns, to_ns, MAX_MARKERS)).fetchall()
        else:
            rows = conn.execute(
                '''SELECT ts_ns, price, lots, shares, value, side FROM market_trade
                   WHERE ticker = ? COLLATE NOCASE AND ts_ns >= ? AND ts_ns < ?
                     AND value >= ?
                   ORDER BY ts_ns LIMIT ?''',
                (ticker.strip(), from_ns, to_ns, min_value, MAX_MARKERS)).fetchall()
    finally:
        conn.close()

    items = [{'time': _to_chart_time(r['ts_ns']), 'price': r['price'], 'lots': r['lots'],
              'shares': r['shares'], 'value': r['value'], 'side': r['side']} for r in rows]
    items.sort(key=lambda x: x['time'])
    return {'mode': mode, 'min_value': min_value, 'count': len(items),
            'truncated': len(rows) >= MAX_MARKERS, 'items': items}


def tape(ticker: str, from_ns: int, to_ns: int, limit: int = 100) -> list[dict]:
    """Последние сделки для таблицы под графиком."""
    conn = get_connection()
    try:
        rows = conn.execute(
            '''SELECT trade_time, price, lots, shares, value, side FROM market_trade
               WHERE ticker = ? COLLATE NOCASE AND ts_ns >= ? AND ts_ns < ?
               ORDER BY ts_ns DESC LIMIT ?''',
            (ticker.strip(), from_ns, to_ns, int(limit))).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def resolve_range(range_key: str) -> tuple[int, int, str]:
    """Диапазон для графика: сегодня / 3 / 7 / 30 / 90 дней (МСК)."""
    now = datetime.now(MSK)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    days = {'today': 1, '3d': 3, '7d': 7, '30d': 30, '90d': 90}.get(range_key, 1)
    start = today - timedelta(days=days - 1)
    to_ns = int((now + timedelta(minutes=1)).timestamp() * 1_000_000_000)
    return int(start.timestamp() * 1_000_000_000), to_ns, range_key if days > 1 else 'today'
