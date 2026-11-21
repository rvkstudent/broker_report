"""Предварительные сигналы по акциям — быстрый отбор кандидатов на лонг.

Смысл: до глубокого AI-разбора нужен дешёвый фильтр «на что вообще смотреть».
Считаем по минутным данным скринера (screener.db), без запросов в сеть:

* тренд дня: цена выше/ниже VWAP и открытия;
* положение в диапазоне трёх дней: у верхней границы (пробой) или у нижней
  (отскок) — оба варианта годятся для лонга, но по разным причинам;
* перевес покупок: дельта последних 30 минут и доля покупок в крупных сделках;
* всплеск объёма: текущий темп против среднего по дню.

Сигнал «лонг» ставится только когда совпали тренд И перевес покупателей:
это ещё не рекомендация, а повод открыть график и запустить AI-план.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone, timedelta

MSK = timezone(timedelta(hours=3))
LOOKBACK_DAYS = 3
RECENT_MINUTES = 30


def _connect() -> sqlite3.Connection | None:
    from app.screener import screener_db_path
    try:
        conn = sqlite3.connect('file:%s?mode=ro' % screener_db_path(), uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.Error:
        return None


def _day_stats(conn: sqlite3.Connection, ticker: str) -> dict | None:
    """Агрегаты по последним минутам: диапазон, VWAP, объём, дельта."""
    rows = conn.execute(
        """SELECT trade_date, open, high, low, close, vwap, volume, turnover
             FROM market_bar_1m
            WHERE ticker = ? COLLATE NOCASE
            ORDER BY minute_ns DESC
            LIMIT 1200""", (ticker,)).fetchall()
    if not rows:
        return None
    rows = list(reversed(rows))
    last = rows[-1]
    price = float(last['close'] or 0)
    if not price:
        return None

    days = sorted({r['trade_date'] for r in rows})[-LOOKBACK_DAYS:]
    window = [r for r in rows if r['trade_date'] in days]
    hi = max(float(r['high'] or 0) for r in window)
    lo = min(float(r['low'] or 0) for r in window if float(r['low'] or 0) > 0)
    if not hi or not lo:
        return None
    span = hi - lo or 1e-9
    position = (price - lo) / span            # 0 — у минимума, 1 — у максимума

    today = max(days)
    today_rows = [r for r in window if r['trade_date'] == today]
    day_open = float(today_rows[0]['open'] or price)
    vwap_num = sum(float(r['vwap'] or 0) * float(r['volume'] or 0) for r in today_rows)
    vwap_den = sum(float(r['volume'] or 0) for r in today_rows)
    vwap = (vwap_num / vwap_den) if vwap_den else price

    # Объём: темп последнего часа против среднего минутного за день
    if len(today_rows) > 60:
        recent_vol = sum(float(r['volume'] or 0) for r in today_rows[-60:]) / 60
        avg_vol = sum(float(r['volume'] or 0) for r in today_rows) / len(today_rows)
        vol_ratio = (recent_vol / avg_vol) if avg_vol else 1.0
    else:
        vol_ratio = 1.0

    cutoff = int((datetime.now(MSK) - timedelta(minutes=RECENT_MINUTES)).timestamp() * 1e9)
    delta = conn.execute(
        """SELECT SUM(CASE WHEN side = 'buy' THEN value ELSE 0 END) AS b,
                  SUM(CASE WHEN side = 'sell' THEN value ELSE 0 END) AS s
             FROM market_trade
            WHERE ticker = ? COLLATE NOCASE AND ts_ns >= ?""",
        (ticker, cutoff)).fetchone()
    buy = float((delta or {})['b'] or 0) if delta else 0.0
    sell = float((delta or {})['s'] or 0) if delta else 0.0
    total = buy + sell
    delta_ratio = (buy - sell) / total if total else 0.0

    return {
        'price': price, 'hi': hi, 'lo': lo, 'position': position,
        'above_vwap': price > vwap, 'above_open': price > day_open,
        'vwap': round(vwap, 4), 'day_open': round(day_open, 4),
        'vol_ratio': round(vol_ratio, 2), 'delta_ratio': round(delta_ratio, 3),
        'range_pct': round((hi / lo - 1) * 100, 2), 'days': len(days),
    }


def signal_for(stats: dict | None) -> dict:
    """Правило: лонг = тренд вверх + покупки перевешивают."""
    if not stats:
        return {'signal': 'нет данных', 'score': 0, 'reasons': []}
    score = 0
    reasons: list[str] = []
    if stats['above_vwap']:
        score += 1
        reasons.append('цена выше VWAP дня')
    if stats['above_open']:
        score += 1
        reasons.append('цена выше открытия')
    if stats['delta_ratio'] > 0.05:
        score += 1
        reasons.append(f'перевес покупок {stats["delta_ratio"] * 100:.0f}% (30 мин)')
    if stats['position'] > 0.75:
        score += 1
        reasons.append('цена у верхней границы 3 дней — пробой')
    elif stats['position'] < 0.35 and stats['delta_ratio'] > 0:
        score += 1
        reasons.append('отскок от нижней части диапазона при покупках')
    if stats['vol_ratio'] >= 1.5:
        score += 1
        reasons.append(f'всплеск объёма ×{stats["vol_ratio"]:.1f}')

    if score >= 3 and stats['above_vwap'] and stats['delta_ratio'] > 0:
        label = 'лонг'
    elif score >= 2:
        label = 'наблюдать'
    else:
        label = '—'
    return {'signal': label, 'score': score, 'reasons': reasons[:4], **stats}


def for_tickers(tickers: list[str]) -> dict[str, dict]:
    """Сигналы по списку тикеров одним проходом (без сети)."""
    conn = _connect()
    if conn is None:
        return {}
    out: dict[str, dict] = {}
    try:
        for ticker in tickers:
            try:
                out[ticker.upper()] = signal_for(_day_stats(conn, ticker))
            except sqlite3.Error:
                continue
    finally:
        conn.close()
    return out
