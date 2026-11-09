"""Волатильность инструмента: абсолютная (₽) и относительная (%).

Считается по минутным барам из `market_bar_1m` — там есть OHLC, поэтому
true range и размах считаются без сырого тика и работают за весь срок
хранения минуток (а не только за срок хранения ленты).

Зачем две меры сразу: **проценты** сравнимы между бумагами (SBER за 0.02 %
и NVTK за 0.1 % — это разная волатильность при разных ценах), а **рубли**
нужны для оценки риска позиции: стоп в 0.3 % на 100 тыс ₽ и на 1 млн ₽ —
одинаковые проценты, но разные деньги.
"""

from __future__ import annotations

import math
import statistics

from app import chartdata

# Длительность торговой сессии MOEX в минутах (09:50–23:50). Нужна, чтобы
# привести волатильность бара к «дневной» — иначе бумаги с разным числом
# собранных минут несравнимы.
SESSION_MINUTES = 14 * 60


def _true_range(bars: list[dict]) -> list[float]:
    result = []
    prev_close = None
    for bar in bars:
        high, low = bar['high'], bar['low']
        if prev_close is None:
            result.append(high - low)
        else:
            result.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
        prev_close = bar['close']
    return result


def _log_returns(bars: list[dict]) -> list[float]:
    result = []
    prev = None
    for bar in bars:
        close = bar['close']
        if prev and prev > 0 and close > 0:
            result.append(math.log(close / prev))
        prev = close
    return result


def metrics(ticker: str, from_ns: int, to_ns: int, interval: int = 1,
            atr_window: int = 14) -> dict:
    """Волатильность инструмента за диапазон.

    Отдаём и «на бар», и приведённую к часу/дню, и фактический размах —
    последнее не экстраполяция, а то, что реально было.
    """
    data = chartdata.prepared(ticker, from_ns, to_ns, interval)
    bars = data['bars']
    inst = data['instrument']

    empty = {
        'ticker': ticker.upper(), 'interval': interval, 'bars': 0, 'price': 0.0,
        'session_minutes': 0, 'atr_rub': 0.0, 'atr_pct': 0.0,
        'sigma_bar_pct': 0.0, 'sigma_bar_rub': 0.0,
        'sigma_hour_pct': 0.0, 'sigma_day_pct': 0.0, 'sigma_day_rub': 0.0,
        'avg_range_rub': 0.0, 'avg_range_pct': 0.0,
        'range_rub': 0.0, 'range_pct': 0.0,
        'volume_rub': 0.0,
    }
    if not bars:
        return empty

    price = bars[-1]['close']
    session_minutes = len(bars) * interval

    ranges = [bar['high'] - bar['low'] for bar in bars]
    avg_range_rub = statistics.fmean(ranges)
    tr = _true_range(bars)
    atr_rub = statistics.fmean(tr[-atr_window:]) if tr else 0.0

    returns = _log_returns(bars)
    sigma_bar_pct = (statistics.pstdev(returns) * 100) if len(returns) > 1 else 0.0
    # Приведение к часу/дню — по корню из времени (стандартное правило
    # масштабирования волатильности).
    bars_per_hour = 60 / max(1, interval)
    bars_per_day = SESSION_MINUTES / max(1, interval)

    high = max(bar['high'] for bar in bars)
    low = min(bar['low'] for bar in bars)

    return {
        'ticker': ticker.upper(),
        'interval': interval,
        'bars': len(bars),
        'price': round(price, 4),
        'session_minutes': session_minutes,
        'atr_rub': round(atr_rub, 4),
        'atr_pct': round(atr_rub / price * 100, 4) if price else 0.0,
        'sigma_bar_pct': round(sigma_bar_pct, 4),
        'sigma_bar_rub': round(sigma_bar_pct / 100 * price, 4),
        'sigma_hour_pct': round(sigma_bar_pct * math.sqrt(bars_per_hour), 3),
        'sigma_day_pct': round(sigma_bar_pct * math.sqrt(bars_per_day), 3),
        'sigma_day_rub': round(sigma_bar_pct * math.sqrt(bars_per_day) / 100 * price, 3),
        'avg_range_rub': round(avg_range_rub, 4),
        'avg_range_pct': round(avg_range_rub / price * 100, 4) if price else 0.0,
        'range_rub': round(high - low, 4),
        'range_pct': round((high - low) / low * 100, 3) if low else 0.0,
        'volume_rub': round(sum(bar['turnover'] or 0 for bar in bars), 0),
    }


def universe(from_ns: int, to_ns: int, interval: int = 1,
             atr_window: int = 14) -> dict[str, dict]:
    """Волатильность по всем выбранным инструментам одним проходом.

    Один SQL вместо N: бары читаются разом и группируются в Python — на
    универсуме в 50 бумаг это ~10 тыс. строк, дешевле N запросов.
    """
    from app.screener import get_connection

    conn = get_connection()
    try:
        rows = conn.execute(
            '''SELECT b.ticker, b.minute_ns, b.high, b.low, b.close, b.turnover
               FROM market_bar_1m b
               JOIN screener_instrument s ON s.instrument_uid = b.instrument_uid
               WHERE s.selected = 1 AND b.minute_ns >= ? AND b.minute_ns < ?
               ORDER BY b.ticker, b.minute_ns''', (from_ns, to_ns)).fetchall()
    finally:
        conn.close()

    grouped: dict[str, list[dict]] = {}
    for row in rows:
        grouped.setdefault(row['ticker'], []).append({
            'minute_ns': row['minute_ns'], 'high': row['high'], 'low': row['low'],
            'close': row['close'], 'turnover': row['turnover'],
        })

    result = {}
    for ticker, bars in grouped.items():
        result[ticker] = _metrics_from_bars(ticker, bars, interval, atr_window)
    return result


def _metrics_from_bars(ticker: str, bars: list[dict], interval: int,
                       atr_window: int) -> dict:
    """Тот же расчёт, но по уже загруженным барам (для универсума)."""
    if not bars:
        return {'ticker': ticker, 'bars': 0, 'atr_rub': 0.0, 'atr_pct': 0.0,
                'sigma_bar_pct': 0.0, 'sigma_day_pct': 0.0, 'sigma_day_rub': 0.0,
                'avg_range_pct': 0.0, 'range_pct': 0.0}
    price = bars[-1]['close']
    returns = _log_returns(bars)
    sigma_bar_pct = (statistics.pstdev(returns) * 100) if len(returns) > 1 else 0.0
    tr = _true_range(bars)
    atr_rub = statistics.fmean(tr[-atr_window:]) if tr else 0.0
    avg_range = statistics.fmean([b['high'] - b['low'] for b in bars])
    high = max(b['high'] for b in bars)
    low = min(b['low'] for b in bars)
    bars_per_day = SESSION_MINUTES / max(1, interval)
    return {
        'ticker': ticker,
        'bars': len(bars),
        'price': round(price, 4),
        'atr_rub': round(atr_rub, 4),
        'atr_pct': round(atr_rub / price * 100, 4) if price else 0.0,
        'sigma_bar_pct': round(sigma_bar_pct, 4),
        'sigma_day_pct': round(sigma_bar_pct * math.sqrt(bars_per_day), 3),
        'sigma_day_rub': round(sigma_bar_pct * math.sqrt(bars_per_day) / 100 * price, 3),
        'avg_range_pct': round(avg_range / price * 100, 4) if price else 0.0,
        'range_pct': round((high - low) / low * 100, 3) if low else 0.0,
        'range_rub': round(high - low, 4),
    }
