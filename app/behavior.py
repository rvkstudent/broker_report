"""Поведение игроков на уровнях: накопление, касания, сигнал.

Идея в трёх частях.

1. **Накопление** (`level_stats`). Объём по цене хранится в постоянных ценовых
   полках (шаг зависит от порядка цены, а не от диапазона периода), поэтому
   профиль копится день за днём и не «переезжает» при смене окна. Сырые сделки
   живут 4 дня, а `level_stats` — сколько нужно, так что статистика уровней
   переживает чистку ленты.
2. **Касания** (`touch_events`). По минуткам находим эпизоды, когда цена
   подходила к зоне, и для каждого считаем: объём относительно обычного, перевес
   агрессора, крупные принты, глубину прокола и исход — уровень **удержал** или
   **пробит**. Из этого получается собственная статистика зоны (hold rate).
3. **Сигнал** (`signal`). Решает не «уровень = разворот», а связка: что за
   уровень, что на нём делают крупные сейчас, как этот уровень вёл себя раньше
   и хватает ли ожидаемого хода, чтобы покрыть издержки.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

from app.screener import MSK, MINUTE_NS, get_connection

# Сколько баров даём уровню «успокоиться» после прокола, чтобы считать пробой
# состоявшимся, а не случайным тычком.
BREAK_CONFIRM_BARS = 3
# Касанием считаем подход ближе этого процента (или шире зоны — что больше).
APPROACH_PCT = 0.25


def price_step(price: float) -> float:
    """Шаг ценовой полки: стабилен при изменении периода и дня.

    Шаг мельче торгового лота — иначе на тихом рынке весь дневной диапазон
    попадает в 2–3 полки и профиль вырождается в один уровень.
    """
    if price <= 0:
        return 0.01
    if price < 1:
        return 0.0001
    if price < 10:
        return 0.002
    if price < 100:
        return 0.01
    if price < 1000:
        return 0.01
    if price < 10000:
        return 0.1
    return 1.0


def bucket_of(price: float, step: float) -> int:
    return int(math.floor(price / step))


# ── накопление ──────────────────────────────────────────────


def accumulate_level_stats(ticker_uid: str, ticker: str, from_ns: int, to_ns: int,
                           large_rub: float = 1_000_000.0) -> int:
    """Разложить сделки по ценовым полкам и записать в level_stats.

    Идемпотентно: за один день данные перезаписываются целиком, поэтому
    повторный запуск (и опоздавшие сделки) не портят статистику.
    """
    conn = get_connection()
    try:
        # Уже посчитанные дни не пересчитываем: сегодня и вчера оставляем
        # «живыми» (могут приходить опоздавшие сделки), остальные — берём из
        # level_stats. Без этого каждый вызов заново агрегировал 30 дней тиков.
        first_day = datetime.fromtimestamp(from_ns / 1_000_000_000, MSK).date()
        last_day = datetime.fromtimestamp(to_ns / 1_000_000_000, MSK).date()
        today = datetime.now(MSK).date()
        protect = {today.isoformat(), (today - timedelta(days=1)).isoformat()}
        existing = {row['day'] for row in conn.execute(
            'SELECT DISTINCT day FROM level_stats '
            'WHERE instrument_uid = ? AND day >= ? AND day <= ?',
            (ticker_uid, first_day.isoformat(), last_day.isoformat())).fetchall()}
        skip = sorted(existing - protect)

        where = 'instrument_uid = ? AND ts_ns >= ? AND ts_ns < ?'
        args: list = [large_rub, large_rub, ticker_uid, from_ns, to_ns]
        if skip:
            where += ' AND trade_date NOT IN (%s)' % ','.join('?' * len(skip))
            args.extend(skip)

        rows = conn.execute(
            f'''SELECT trade_date, price,
                      SUM(CASE WHEN side='buy'  THEN value ELSE 0 END) AS buy_rub,
                      SUM(CASE WHEN side='sell' THEN value ELSE 0 END) AS sell_rub,
                      SUM(CASE WHEN side='buy'  AND value >= ? THEN value ELSE 0 END) AS large_buy,
                      SUM(CASE WHEN side='sell' AND value >= ? THEN value ELSE 0 END) AS large_sell,
                      COUNT(*) AS trades
               FROM market_trade
               WHERE {where}
               GROUP BY trade_date, price''',
            args).fetchall()
        if not rows:
            return 0

        # Пересобираем дни целиком, чтобы не осталось старых полок
        days = sorted({row['trade_date'] for row in rows})
        placeholders = ','.join('?' * len(days))
        conn.execute(
            f'DELETE FROM level_stats WHERE instrument_uid = ? AND day IN ({placeholders})',
            (ticker_uid, *days))

        prepared = []
        for row in rows:
            price = float(row['price'])
            step = price_step(price)
            prepared.append((
                ticker_uid, row['trade_date'], bucket_of(price, step), price,
                row['buy_rub'] or 0.0, row['sell_rub'] or 0.0,
                row['large_buy'] or 0.0, row['large_sell'] or 0.0, row['trades'] or 0,
            ))
        conn.executemany(
            '''INSERT OR REPLACE INTO level_stats
               (instrument_uid, day, bucket, price, buy_rub, sell_rub,
                large_buy, large_sell, trades)
               VALUES (?,?,?,?,?,?,?,?,?)''', prepared)
        conn.commit()
        return len(prepared)
    finally:
        conn.close()


def history_profile(ticker_uid: str, days: int = 0) -> list[dict]:
    """Накопленный профиль по цене за всё время (или за N последних дней)."""
    conn = get_connection()
    try:
        params: list = [ticker_uid]
        extra = ''
        if days:
            since = (datetime.now(MSK) - timedelta(days=days)).strftime('%Y-%m-%d')
            extra = ' AND day >= ?'
            params.append(since)
        rows = conn.execute(
            f'''SELECT bucket,
                       MAX(price) AS price,
                       SUM(buy_rub) AS buy_rub, SUM(sell_rub) AS sell_rub,
                       SUM(large_buy) AS large_buy, SUM(large_sell) AS large_sell,
                       SUM(trades) AS trades
                FROM level_stats WHERE instrument_uid = ?{extra}
                GROUP BY bucket ORDER BY bucket''', params).fetchall()
        return [{
            'bucket': r['bucket'], 'price': r['price'],
            'buy_value': r['buy_rub'] or 0.0, 'sell_value': r['sell_rub'] or 0.0,
            'large_buy': r['large_buy'] or 0.0, 'large_sell': r['large_sell'] or 0.0,
            'trades': r['trades'] or 0,
        } for r in rows]
    finally:
        conn.close()


# ── касания уровня ──────────────────────────────────────────


def _load_bars(ticker: str, from_ns: int, to_ns: int) -> list[dict]:
    conn = get_connection()
    try:
        rows = conn.execute(
            '''SELECT minute_ns, open, high, low, close, volume, turnover,
                      buy_volume, sell_volume, large_buy_turnover, large_sell_turnover
               FROM market_bar_1m
               WHERE ticker = ? COLLATE NOCASE AND minute_ns >= ? AND minute_ns < ?
               ORDER BY minute_ns''', (ticker, from_ns, to_ns)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def touch_events(bars: list[dict], zone: dict) -> list[dict]:
    """Эпизоды подхода цены к зоне с исходом «удержал» / «пробил».

    Касание начинается, когда бар пересёкся с полосой зоны, расширенной на
    APPENDIX, и заканчивается, когда цена ушла от зоны дальше её ширины.
    """
    low, high = zone['low'], zone['high']
    width = max(high - low, (high + low) / 2 * 0.0005)
    approach = max(width, (high + low) / 2 * APPROACH_PCT / 100)
    band_lo, band_hi = low - approach, high + approach

    events: list[dict] = []
    current: dict | None = None

    for index, bar in enumerate(bars):
        inside = bar['low'] <= band_hi and bar['high'] >= band_lo
        if inside:
            if current is None:
                current = {
                    'start': index, 'entry_price': bar['open'],
                    'buy_volume': 0, 'sell_volume': 0, 'turnover': 0.0,
                    'large_buy': 0.0, 'large_sell': 0.0,
                    'min_close': bar['close'], 'max_close': bar['close'],
                }
            current['buy_volume'] += bar['buy_volume'] or 0
            current['sell_volume'] += bar['sell_volume'] or 0
            current['turnover'] += bar['turnover'] or 0
            current['large_buy'] += bar['large_buy_turnover'] or 0
            current['large_sell'] += bar['large_sell_turnover'] or 0
            current['min_close'] = min(current['min_close'], bar['close'])
            current['max_close'] = max(current['max_close'], bar['close'])
        elif current is not None:
            current['end'] = index
            current['exit_price'] = bar['close']
            events.append(current)
            current = None

    if current is not None:
        current['end'] = len(bars) - 1
        current['exit_price'] = bars[-1]['close']
        events.append(current)

    for event in events:
        total = event['buy_volume'] + event['sell_volume']
        event['bars'] = event['end'] - event['start'] + 1
        event['flow'] = round((event['buy_volume'] - event['sell_volume']) / total, 3) if total else 0.0
        large_total = event['large_buy'] + event['large_sell']
        event['large_flow'] = (round((event['large_buy'] - event['large_sell']) / large_total, 3)
                               if large_total else 0.0)
        event['large_rub'] = round(large_total, 0)
        event['start_time'] = bars[event['start']]['minute_ns']
        event['end_time'] = bars[event['end']]['minute_ns']
        # Исход: ушла ли цена за зону и удержалась ли там
        tail = bars[event['end'] + 1:event['end'] + 1 + BREAK_CONFIRM_BARS]
        beyond_down = event['min_close'] < low - width * 0.5
        beyond_up = event['max_close'] > high + width * 0.5
        stayed = tail and all(b['close'] < low for b in tail) if beyond_down else (
            tail and all(b['close'] > high for b in tail) if beyond_up else False)
        if beyond_down and stayed:
            event['outcome'] = 'break_down'
        elif beyond_up and stayed:
            event['outcome'] = 'break_up'
        elif beyond_down:
            event['outcome'] = 'pierce_down'
        elif beyond_up:
            event['outcome'] = 'pierce_up'
        else:
            event['outcome'] = 'held'
        event['anchor'] = zone['price']
    return events


def attach_history(zones: list[dict], bars: list[dict]) -> None:
    """Проставить каждой зоне её статистику касаний и текущее поведение."""
    for zone in zones:
        events = touch_events(bars, zone)
        finished = [e for e in events if e['end'] < len(bars) - 1]
        held = sum(1 for e in finished if e['outcome'] == 'held')
        breaks = sum(1 for e in finished
                     if e['outcome'] in ('break_down', 'break_up'))
        last = events[-1] if events else None
        # Текущий (незавершённый) эпизод тоже считаем касанием: он идёт прямо
        # сейчас и именно его поведение нам важно.
        zone['touches'] = len(events)
        zone['held'] = held
        zone['breaks'] = breaks
        zone['hold_rate'] = round(held / len(finished), 2) if finished else None
        zone['finished'] = len(finished)
        zone['avg_volume_rub'] = (round(sum(e['turnover'] for e in finished) / len(finished), 0)
                                  if finished else 0)
        zone['behavior'] = _classify(last, zone) if last else None


def _classify(event: dict | None, zone: dict) -> dict | None:
    """Что сейчас делают на уровне: абсорбция, пробой, прокол, тест."""
    if not event:
        return None
    outcome = event['outcome']
    if outcome == 'held':
        kind = 'absorption'
        label = 'уровень держат'
    elif outcome == 'break_up':
        kind, label = 'break_up', 'пробой вверх'
    elif outcome == 'break_down':
        kind, label = 'break_down', 'пробой вниз'
    elif outcome == 'pierce_up':
        kind, label = 'pierce_up', 'прокол вверх, вернулись'
    else:
        kind, label = 'pierce_down', 'прокол вниз, вернулись'
    return {
        'kind': kind,
        'label': label,
        'flow': event['flow'],
        'large_flow': event['large_flow'],
        'large_rub': event['large_rub'],
        'turnover': round(event['turnover'], 0),
        'bars': event['bars'],
        'start': event['start_time'],
        'end': event['end_time'],
        'active': event['end'] >= 0,
    }


# ── сигнал ──────────────────────────────────────────────────


def build_signal(zones: list[dict], price: float, next_up: dict | None,
                 next_down: dict | None, *, fee_pct: float = 0.05,
                 min_reward_ratio: float = 1.5) -> dict:
    """Сигнал по поведению на ближайшем уровне.

    Направление берётся из того, что делают на уровне, история уровня работает
    как вес, а издержки — как фильтр: если ожидаемый ход меньше
    ``min_reward_ratio × круговой комиссии``, сигнал помечается «не стоит».
    """
    cost_pct = 2 * fee_pct
    active = [z for z in zones if z.get('behavior')]
    if not active:
        return {'direction': 'neutral', 'confidence': 0, 'score': 0.0,
                'title': 'Нет активного уровня',
                'reasons': ['цена не у ближайших зон — поведение не читается'],
                'expected_move_pct': None, 'cost_pct': cost_pct,
                'blocked_by_cost': False, 'target': None, 'stop': None,
                'level': None}

    # Ближайший к цене уровень с поведением
    zone = min(active, key=lambda z: abs(z['price'] - price))
    behavior = zone['behavior']
    hold_rate = zone['hold_rate']
    touches = zone['touches']

    score = 0.0
    reasons: list[str] = []

    if behavior['kind'] == 'break_up':
        score += 1.4
        reasons.append('закрытие выше зоны с подтверждением — покупатели продавили')
    elif behavior['kind'] == 'break_down':
        score -= 1.4
        reasons.append('закрытие ниже зоны с подтверждением — продавцы продавили')
    elif behavior['kind'] == 'absorption':
        if zone['side'] == 'support':
            score += 1.0
        else:
            score -= 1.0
        reasons.append('объём на уровне есть, а цена не проходит — сторону держат')
    elif behavior['kind'] in ('pierce_up', 'pierce_down'):
        # Прокол и возврат — движение против прокола
        score += -0.9 if behavior['kind'] == 'pierce_up' else 0.9
        reasons.append('прокол без удержания — вероятен возврат')

    score += 0.9 * behavior['flow'] + 0.7 * behavior['large_flow']
    reasons.append(
        f"перевес агрессора {behavior['flow']:+.2f}, крупные {behavior['large_flow']:+.2f} "
        f"({behavior['large_rub']:,.0f} ₽)")

    if hold_rate is not None and touches >= 3:
        weight = min(1.0, touches / 8)
        bonus = (hold_rate - 0.5) * 2 * weight
        score += bonus if zone['side'] == 'support' else -bonus
        reasons.append(f'уровень держал {hold_rate * 100:.0f}% из {touches} касаний')

    score = max(-3.0, min(3.0, score))
    direction = 'up' if score > 0.4 else ('down' if score < -0.4 else 'neutral')
    confidence = min(90, int(abs(score) / 3.0 * 100)) if direction != 'neutral' else 0
    # Направление — это «уклон», а сигнал к действию появляется только если
    # есть цель и ход покрывает издержки.
    bias = {'up': 'вверх', 'down': 'вниз', 'neutral': 'нейтрально'}[direction]

    target_zone = next_up if direction == 'up' else (next_down if direction == 'down' else None)
    target = target_zone['price'] if target_zone else None
    stop_zone = next_down if direction == 'up' else (next_up if direction == 'down' else None)
    stop = stop_zone['price'] if stop_zone else None
    expected = abs((target / price - 1) * 100) if target else None
    blocked = bool(expected is not None and expected < cost_pct * min_reward_ratio)

    if expected is not None:
        reasons.append(f'ожидаемый ход {expected:.2f}% против издержек {cost_pct:.2f}%')
        if blocked:
            reasons.append(f'меньше {min_reward_ratio}× издержек — сделка не оправдана')
    elif direction != 'neutral':
        reasons.append('цели нет: ближайшая зона слишком близко — двигаться некуда')

    actionable = bool(direction != 'neutral' and expected is not None and not blocked)
    if actionable:
        title = 'Сигнал вверх' if direction == 'up' else 'Сигнал вниз'
    elif direction != 'neutral' and expected is None:
        title = f'Сигнала нет · уклон {bias}'
    elif blocked:
        title = f'Сигнала нет · ход меньше издержек · уклон {bias}'
    else:
        title = 'Явного перевеса нет'

    return {
        'direction': direction,
        'confidence': confidence,
        'score': round(score, 2),
        'actionable': actionable,
        'bias': bias,
        'title': title,
        'level': {'price': zone['price'], 'side': zone['side'],
                  'low': zone['low'], 'high': zone['high'],
                  'hold_rate': hold_rate, 'touches': touches,
                  'behavior': behavior['label']},
        'target': target,
        'stop': stop,
        'expected_move_pct': round(expected, 3) if expected is not None else None,
        'cost_pct': cost_pct,
        'blocked_by_cost': blocked,
        'reasons': reasons,
    }
