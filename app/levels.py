"""Уровни поддержки/сопротивления и перевес крупных игроков на них.

Логика — не «поймать шум», а понять, **между какими уровнями цена и кто на них
действует**:

1. **Профиль объёма по цене** (market profile): для каждого ценового уровня
   собираем объём покупок и продаж. Уровни с аномальным объёмом — это зоны,
   где реально торговались, то есть поддержка/сопротивление.
2. **Разделение по сторонам**: у каждой зоны видно, кто её «ел» — инициативные
   покупатели (buy) или продавцы (sell), и отдельно — крупные принты.
3. **Вердикт**: цена между уровнями; у ближнего уровня смотрим перевес и
   недавний импульс дельты и говорим, куда вероятнее следующее движение.

Источник — обезличенные сделки (`market_trade`): там есть цена, объём и
сторона, поэтому профиль строится по фактам, а не по экстраполяции. Если
сделки за период уже вычищены (срок хранения тика), профиль строится по
минуткам: объём бара распределяется по его диапазону low…high.
"""

from __future__ import annotations

import math
from datetime import datetime

from app.screener import MSK, get_connection

# Сколько ценовых «полок» строим. 80 полок — компромисс между детализацией и
# шумом: на дневном диапазоне SBER это шаг ~0.01 ₽.
DEFAULT_BUCKETS = 80
MIN_BUCKETS = 12
MAX_BUCKETS = 240

# Зона «рядом», если цена подошла ближе этого процента диапазона.
NEAR_PCT = 0.35
# Минимальное расстояние до цели: ближе этого уровня движение — уже шум.
MIN_TARGET_PCT = 0.05


class LevelsError(ValueError):
    """Некорректный запрос уровней."""


def _price_bounds(conn, ticker: str, from_ns: int, to_ns: int,
                  uid: str = '') -> tuple[float, float, int] | None:
    # Сделки фильтруем по instrument_uid (индекс idx_market_trade_uid_ts).
    # По ticker индекса нет — это был полный скан таблицы на 5 млн строк.
    if uid:
        trade_where, trade_args = 'instrument_uid = ?', (uid, from_ns, to_ns)
    else:
        trade_where, trade_args = 'ticker = ? COLLATE NOCASE', (ticker, from_ns, to_ns)
    row = conn.execute(
        f'''SELECT MIN(price) AS lo, MAX(price) AS hi, COUNT(*) AS n FROM market_trade
           WHERE {trade_where} AND ts_ns >= ? AND ts_ns < ?''',
        trade_args).fetchone()
    if row and row['n']:
        return float(row['lo']), float(row['hi']), int(row['n'])
    row = conn.execute(
        '''SELECT MIN(low) AS lo, MAX(high) AS hi, COUNT(*) AS n FROM market_bar_1m
           WHERE ticker = ? COLLATE NOCASE AND minute_ns >= ? AND minute_ns < ?''',
        (ticker, from_ns, to_ns)).fetchone()
    if row and row['n']:
        return float(row['lo']), float(row['hi']), int(row['n'])
    return None


def _profile_from_trades(conn, ticker: str, from_ns: int, to_ns: int,
                         lo: float, step: float, buckets: int,
                         large_rub: float, uid: str = '') -> list[dict]:
    trade_where = 'instrument_uid = :uid' if uid else 'ticker = :ticker COLLATE NOCASE'
    params = {'lo': lo, 'step': step, 'large': large_rub, 'ticker': ticker,
              'uid': uid, 'from': from_ns, 'to': to_ns}
    rows = conn.execute(
        f'''SELECT CAST((price - :lo) / :step AS INTEGER) AS b,
                  SUM(CASE WHEN side='buy'  THEN shares ELSE 0 END) AS buy_shares,
                  SUM(CASE WHEN side='sell' THEN shares ELSE 0 END) AS sell_shares,
                  SUM(CASE WHEN side='buy'  THEN value  ELSE 0 END) AS buy_value,
                  SUM(CASE WHEN side='sell' THEN value  ELSE 0 END) AS sell_value,
                  SUM(CASE WHEN side='buy'  AND value >= :large THEN value ELSE 0 END) AS large_buy,
                  SUM(CASE WHEN side='sell' AND value >= :large THEN value ELSE 0 END) AS large_sell,
                  COUNT(*) AS trades
           FROM market_trade
           WHERE {trade_where} AND ts_ns >= :from AND ts_ns < :to
           GROUP BY b''',
        params).fetchall()
    profile = [{'bucket': i, 'price': lo + (i + 0.5) * step, 'buy_shares': 0,
                'sell_shares': 0, 'buy_value': 0.0, 'sell_value': 0.0,
                'large_buy': 0.0, 'large_sell': 0.0, 'trades': 0}
               for i in range(buckets)]
    for row in rows:
        # Сделка по максимальной цене попадает ровно в buckets — прижимаем
        index = max(0, min(buckets - 1, int(row['b'] or 0)))
        if 0 <= index < buckets:
            item = profile[index]
            item['buy_shares'] += row['buy_shares'] or 0
            item['sell_shares'] += row['sell_shares'] or 0
            item['buy_value'] += row['buy_value'] or 0
            item['sell_value'] += row['sell_value'] or 0
            item['large_buy'] += row['large_buy'] or 0
            item['large_sell'] += row['large_sell'] or 0
            item['trades'] += row['trades'] or 0
    return profile


def _profile_from_bars(conn, ticker: str, from_ns: int, to_ns: int,
                       lo: float, step: float, buckets: int,
                       large_rub: float) -> list[dict]:
    """Фолбэк: распределяем объём бара по его диапазону low…high."""
    rows = conn.execute(
        '''SELECT high, low, buy_volume, sell_volume, buy_turnover, sell_turnover,
                  large_buy_turnover, large_sell_turnover, trades
           FROM market_bar_1m
           WHERE ticker = ? COLLATE NOCASE AND minute_ns >= ? AND minute_ns < ?''',
        (ticker, from_ns, to_ns)).fetchall()
    profile = [{'bucket': i, 'price': lo + (i + 0.5) * step, 'buy_shares': 0,
                'sell_shares': 0, 'buy_value': 0.0, 'sell_value': 0.0,
                'large_buy': 0.0, 'large_sell': 0.0, 'trades': 0}
               for i in range(buckets)]
    for row in rows:
        low = float(row['low'] or 0)
        high = float(row['high'] or 0)
        if high <= 0 or low <= 0:
            continue
        first = max(0, min(buckets - 1, int((low - lo) / step)))
        last = max(0, min(buckets - 1, int((high - lo) / step)))
        span = last - first + 1
        for index in range(first, last + 1):
            item = profile[index]
            item['buy_shares'] += (row['buy_volume'] or 0) // span
            item['sell_shares'] += (row['sell_volume'] or 0) // span
            item['buy_value'] += (row['buy_turnover'] or 0) / span
            item['sell_value'] += (row['sell_turnover'] or 0) / span
            item['large_buy'] += (row['large_buy_turnover'] or 0) / span
            item['large_sell'] += (row['large_sell_turnover'] or 0) / span
            item['trades'] += (row['trades'] or 0) // span
    return profile


def _zones_from_profile(profile: list[dict], price: float, step: float,
                        large_rub: float) -> list[dict]:
    """Собрать зоны из пиков профиля: пик + соседние полки с заметным объёмом."""
    raw_totals = [(p['buy_value'] + p['sell_value']) for p in profile]
    if not raw_totals:
        return []
    # Сглаживаем по трём полкам: одиночный аукционный принт на десятки
    # миллионов иначе «съедает» все остальные уровни.
    totals = []
    for i in range(len(raw_totals)):
        window = raw_totals[max(0, i - 1):i + 2]
        totals.append(sum(window) / len(window))
    ordered = sorted(totals)
    max_total = ordered[-1]
    # Порог берём от 92-го перцентиля, чтобы одиночный выброс (аукционный
    # принт) не задирал планку; но на разреженном профиле перцентиль равен
    # нулю — тогда отталкиваемся от максимума.
    peak_total = max(ordered[min(len(ordered) - 1, int(len(ordered) * 0.92))],
                     max_total * 0.2)
    if peak_total <= 0:
        return []
    min_peak = max(peak_total * 0.35, max_total * 0.04)

    zones = []
    used = set()
    for i, total in enumerate(totals):
        if i in used or total < min_peak:
            continue
        if i > 0 and totals[i - 1] > total:
            continue
        if i < len(totals) - 1 and totals[i + 1] > total:
            continue

        # Расширяем зону, пока соседние полки набирают хотя бы половину пика
        lo_i = hi_i = i
        while lo_i > 0 and totals[lo_i - 1] >= total * 0.45 and lo_i - 1 not in used:
            lo_i -= 1
        while hi_i < len(totals) - 1 and totals[hi_i + 1] >= total * 0.45 and hi_i + 1 not in used:
            hi_i += 1
        used.update(range(lo_i, hi_i + 1))

        band = profile[lo_i:hi_i + 1]
        buy_value = sum(b['buy_value'] for b in band)
        sell_value = sum(b['sell_value'] for b in band)
        volume_value = buy_value + sell_value
        if volume_value <= 0:
            continue
        large_buy = sum(b['large_buy'] for b in band)
        large_sell = sum(b['large_sell'] for b in band)
        large_total = large_buy + large_sell
        zone_price = sum(b['price'] * (b['buy_value'] + b['sell_value']) for b in band) / volume_value

        zones.append({
            'price': round(zone_price, 4),
            'low': round(profile[lo_i]['price'] - step / 2, 4),
            'high': round(profile[hi_i]['price'] + step / 2, 4),
            'volume_rub': round(volume_value, 0),
            'buy_rub': round(buy_value, 0),
            'sell_rub': round(sell_value, 0),
            'buy_share': round(buy_value / volume_value, 3),
            'flow': round((buy_value - sell_value) / volume_value, 3),
            'large_rub': round(large_total, 0),
            'large_flow': round((large_buy - large_sell) / large_total, 3) if large_total else 0.0,
            'trades': sum(b['trades'] for b in band),
            'side': 'support' if zone_price < price else 'resistance',
            'distance_pct': round((zone_price / price - 1) * 100, 3),
            'rank': 0,
        })

    zones = _merge_zones(zones, step)
    zones.sort(key=lambda z: z['volume_rub'], reverse=True)
    for index, zone in enumerate(zones, start=1):
        zone['rank'] = index
    return zones


def _merge_zones(zones: list[dict], step: float) -> list[dict]:
    """Склеить зоны, стоящие ближе 0.2 % друг к другу — это одна зона."""
    if not zones:
        return zones
    ordered = sorted(zones, key=lambda z: z['price'])
    merged: list[dict] = []
    for zone in ordered:
        if merged:
            prev = merged[-1]
            # Склеиваем только пересекающиеся полосы: иначе на узком
            # дневном диапазоне все уровни сливаются в один.
            if zone['low'] <= prev['high'] + step * 0.5:
                total = prev['volume_rub'] + zone['volume_rub']
                weight = zone['volume_rub'] / total if total else 0.5
                prev['price'] = round(prev['price'] * (1 - weight) + zone['price'] * weight, 4)
                prev['low'] = min(prev['low'], zone['low'])
                prev['high'] = max(prev['high'], zone['high'])
                prev['volume_rub'] += zone['volume_rub']
                prev['buy_rub'] += zone['buy_rub']
                prev['sell_rub'] += zone['sell_rub']
                prev['large_rub'] += zone['large_rub']
                prev['trades'] += zone['trades']
                if total:
                    prev['buy_share'] = round(prev['buy_rub'] / total, 3)
                    prev['flow'] = round((prev['buy_rub'] - prev['sell_rub']) / total, 3)
                continue
        merged.append(dict(zone))
    return merged


def _recent_delta(conn, ticker: str, from_ns: int, to_ns: int, minutes: int = 30) -> dict:
    """Импульс: дельта покупок/продаж за последние N минут по минуткам."""
    now_ns = int(datetime.now(MSK).timestamp() * 1_000_000_000)
    start_ns = max(from_ns, min(to_ns, now_ns) - minutes * 60 * 1_000_000_000)
    row = conn.execute(
        '''SELECT SUM(buy_volume) AS buy, SUM(sell_volume) AS sell,
                  SUM(buy_turnover) AS buy_rub, SUM(sell_turnover) AS sell_rub
           FROM market_bar_1m WHERE ticker = ? COLLATE NOCASE
             AND minute_ns >= ? AND minute_ns <= ?''',
        (ticker, start_ns, to_ns)).fetchone()
    buy = float(row['buy'] or 0)
    sell = float(row['sell'] or 0)
    buy_rub = float(row['buy_rub'] or 0)
    sell_rub = float(row['sell_rub'] or 0)
    total = buy + sell
    return {
        'minutes': minutes,
        'buy_share': round(buy / total, 3) if total else 0.5,
        'delta_ratio': round((buy - sell) / total, 3) if total else 0.0,
        'delta_rub': round(buy_rub - sell_rub, 0),
    }


def _build_profile(conn, ticker: str, from_ns: int, to_ns: int, buckets: int,
                   large_rub: float, uid: str = '') -> dict | None:
    """Профиль объёма по цене за окно: сделки, а если их нет — минутки."""
    bounds = _price_bounds(conn, ticker, from_ns, to_ns, uid=uid)
    if not bounds:
        return None
    lo, hi, points = bounds
    if hi <= lo:
        hi = lo + max(lo * 0.001, 1e-6)
    step = (hi - lo) / buckets
    if uid:
        has_trades = conn.execute(
            'SELECT 1 FROM market_trade WHERE instrument_uid = ? '
            'AND ts_ns >= ? AND ts_ns < ? LIMIT 1', (uid, from_ns, to_ns)).fetchone()
    else:
        has_trades = conn.execute(
            'SELECT 1 FROM market_trade WHERE ticker = ? COLLATE NOCASE '
            'AND ts_ns >= ? AND ts_ns < ? LIMIT 1', (ticker, from_ns, to_ns)).fetchone()
    if has_trades:
        profile = _profile_from_trades(conn, ticker, from_ns, to_ns, lo, step, buckets,
                                       large_rub, uid=uid)
        source = 'trades'
    else:
        profile = _profile_from_bars(conn, ticker, from_ns, to_ns, lo, step, buckets, large_rub)
        source = 'bars'
    return {'profile': profile, 'lo': lo, 'hi': hi, 'step': step,
            'points': points, 'source': source}


def analyze(ticker: str, from_ns: int, to_ns: int, *, buckets: int = DEFAULT_BUCKETS,
            large_rub: float = 1_000_000.0, near_pct: float = NEAR_PCT,
            history_days: int = 0) -> dict:
    """Уровни + перевес на них + вероятное направление следующего движения.

    ``from_ns``/``to_ns`` — окно графика (для профиля, который рисуется слева).
    Уровни и их история при ``history_days`` считаются по **своему** окну
    («последние N дней»), иначе они менялись бы при смене таймфрейма.
    """
    ticker = (ticker or '').strip().upper()
    if not ticker:
        raise LevelsError('Не указан тикер')
    buckets = max(MIN_BUCKETS, min(int(buckets), MAX_BUCKETS))

    conn = get_connection()
    try:
        uid_row = conn.execute(
            'SELECT instrument_uid FROM screener_instrument WHERE ticker = ? COLLATE NOCASE',
            (ticker,)).fetchone()
        uid = uid_row['instrument_uid'] if uid_row else None

        period = _build_profile(conn, ticker, from_ns, to_ns, buckets, large_rub,
                                uid=uid or '')
        if period is None:
            raise LevelsError(f'Нет данных по {ticker} за выбранный период')

        price_row = conn.execute(
            'SELECT close FROM market_bar_1m WHERE ticker = ? COLLATE NOCASE '
            'AND minute_ns <= ? ORDER BY minute_ns DESC LIMIT 1',
            (ticker, to_ns)).fetchone()
        price = float(price_row['close']) if price_row else (period['lo'] + period['hi']) / 2

        delta = _recent_delta(conn, ticker, from_ns, to_ns)
    finally:
        conn.close()

    source = period['source']
    zone_profile, zone_step = period['profile'], period['step']
    levels_from, levels_to = from_ns, to_ns

    # ── уровни и их история: своё окно, независимое от графика ──
    if history_days:
        from app import behavior

        now_ns = int(datetime.now(MSK).timestamp() * 1_000_000_000)
        levels_from = now_ns - history_days * 86400 * 1_000_000_000
        levels_to = now_ns
        if uid:
            try:
                # Досчитываем полки по тем сделкам, что ещё лежат в ленте
                behavior.accumulate_level_stats(uid, ticker, levels_from, levels_to,
                                                large_rub)
            except Exception:
                pass
            hist = behavior.history_profile(uid, history_days)
            if len(hist) >= 6:
                zone_step = behavior.price_step(price)
                zone_profile = [{
                    'price': item['price'], 'buy_value': item['buy_value'],
                    'sell_value': item['sell_value'], 'large_buy': item['large_buy'],
                    'large_sell': item['large_sell'], 'trades': item['trades'],
                } for item in hist]
                source = 'history'

    zones = _zones_from_profile(zone_profile, price, zone_step, large_rub)
    for zone in zones:
        zone['origin'] = 'history' if source == 'history' else 'period'

    # Дополняем зонами того окна, для которого рисуется профиль: накопленная
    # история может не иметь уровней выше цены, а дневной профиль их видит.
    if source != period['source'] or (zone_profile is not period['profile']):
        intraday = _zones_from_profile(period['profile'], price, period['step'], large_rub)
        for candidate in intraday:
            overlaps = any(not (candidate['high'] < z['low'] or candidate['low'] > z['high'])
                           for z in zones)
            if not overlaps:
                candidate['origin'] = 'period'
                zones.append(candidate)
        zones.sort(key=lambda z: z['volume_rub'], reverse=True)
        for index, zone in enumerate(zones, start=1):
            zone['rank'] = index

    # Крайние полки профиля: если сверху (или снизу) цены зон не нашлось,
    # берём реальную ближайшую торгованную полку — цена всегда должна быть
    # между уровнями, а не «в пустоте».
    def _edge_zone(above: bool) -> dict | None:
        pool = [p for p in period['profile'] if (p['buy_value'] + p['sell_value']) > 0]
        pool = [p for p in pool if (p['price'] > price) == above]
        if not pool:
            return None
        point = (min if above else max)(pool, key=lambda p: p['price'])
        total = point['buy_value'] + point['sell_value']
        if total <= 0:
            return None
        half = period['step'] / 2
        return {
            'price': round(point['price'], 4),
            'low': round(point['price'] - half, 4),
            'high': round(point['price'] + half, 4),
            'volume_rub': round(total, 0),
            'buy_rub': round(point['buy_value'], 0),
            'sell_rub': round(point['sell_value'], 0),
            'buy_share': round(point['buy_value'] / total, 3),
            'flow': round((point['buy_value'] - point['sell_value']) / total, 3),
            'large_rub': round(point['large_buy'] + point['large_sell'], 0),
            'large_flow': 0.0,
            'trades': point['trades'],
            'side': 'resistance' if above else 'support',
            'distance_pct': round((point['price'] / price - 1) * 100, 3),
            'rank': 0,
            'origin': 'edge',
        }

    support = next((z for z in sorted(zones, key=lambda z: -z['price'])
                    if z['price'] < price), None)
    resistance = next((z for z in sorted(zones, key=lambda z: z['price'])
                       if z['price'] > price), None)
    if resistance is None:
        edge = _edge_zone(True)
        if edge:
            zones.append(edge)
            resistance = edge
    if support is None:
        edge = _edge_zone(False)
        if edge:
            zones.append(edge)
            support = edge

    inside = next((z for z in zones if z['low'] <= price <= z['high']), None)

    signal = None
    if history_days and zones:
        from app import behavior

        bars = behavior._load_bars(ticker, levels_from, levels_to)
        behavior.attach_history(zones, bars)
        support = next((z for z in sorted(zones, key=lambda z: -z['price'])
                        if z['price'] < price), None)
        resistance = next((z for z in sorted(zones, key=lambda z: z['price'])
                           if z['price'] > price), None)
        # Цель — только реальная зона (не техническая «крайняя полка») и не
        # ближе MIN_TARGET_PCT: иначе получается «цель на 0.00 %», что бессмысленно.
        def _target(up: bool) -> dict | None:
            pool = [z for z in zones if z.get('origin') != 'edge']
            if up:
                candidates = [z for z in pool
                              if z['price'] > price
                              and (z['price'] / price - 1) * 100 >= MIN_TARGET_PCT]
                return min(candidates, key=lambda z: z['price']) if candidates else None
            candidates = [z for z in pool
                          if z['price'] < price
                          and (1 - z['price'] / price) * 100 >= MIN_TARGET_PCT]
            return max(candidates, key=lambda z: z['price']) if candidates else None

        signal = behavior.build_signal(zones, price, next_up=_target(True),
                                       next_down=_target(False), fee_pct=0.05)

    verdict = _verdict(price, zones, support, resistance, delta, near_pct, inside)

    profile_out = [{
        'price': round(item['price'], 4),
        'buy': round(item['buy_value'], 0),
        'sell': round(item['sell_value'], 0),
        'total': round(item['buy_value'] + item['sell_value'], 0),
        'delta': round(item['buy_value'] - item['sell_value'], 0),
        'large': round(item['large_buy'] + item['large_sell'], 0),
    } for item in period['profile']]

    return {
        'ticker': ticker,
        'price': round(price, 4),
        'range': {'low': round(period['lo'], 4), 'high': round(period['hi'], 4),
                  'step': round(period['step'], 5)},
        'levels_window': {'from': levels_from, 'to': levels_to, 'days': history_days},
        'source': source,
        'points': period['points'],
        'zones': zones,
        'profile': profile_out,
        'inside': inside,
        'support': support,
        'resistance': resistance,
        'delta': delta,
        'verdict': verdict,
        'signal': signal,
        'history_days': history_days,
    }


def _verdict(price: float, zones: list[dict], support: dict | None,
             resistance: dict | None, delta: dict, near_pct: float,
             inside: dict | None = None) -> dict:
    """Направление следующего движения: решает агрессор, уровни дают цель.

    Логика: сторону задаёт **инициатор сделок** (у нас `side` — сторона тейкера).
    Если агрессивные покупки доминируют у уровня, они этот уровень и продавят;
    уровни — это цели и ориентиры, а не переключатели знака.
    """
    if not zones and not inside:
        return {'direction': 'neutral', 'confidence': 0, 'score': 0.0, 'state': 'none',
                'target': None, 'target_pct': None,
                'title': 'Уровни не определились',
                'detail': 'За период слишком мало данных для профиля объёма.'}

    score = 0.0
    reasons = []

    if inside:
        # Цена внутри зоны: кто её «ест» — туда и выход
        score += 1.2 * inside['flow'] + 0.8 * inside['large_flow']
        reasons.append(
            f"цена в зоне {inside['low']:.3f}–{inside['high']:.3f}: покупки "
            f"{inside['buy_share'] * 100:.0f}% объёма, перевес {inside['flow']:+.2f}, "
            f"крупные {inside['large_flow']:+.2f}")
    else:
        score += 1.5 * delta['delta_ratio']
        reasons.append(
            f"дельта за {delta['minutes']} мин {delta['delta_ratio']:+.2f} "
            f"({delta['delta_rub']:+,.0f} ₽)")
        for zone in (support, resistance):
            if zone and abs(zone['distance_pct']) <= near_pct * 2:
                score += 0.6 * zone['flow'] + 0.4 * zone['large_flow']
                reasons.append(
                    f"у {'поддержки' if zone is support else 'сопротивления'} "
                    f"{zone['price']:.3f} перевес {zone['flow']:+.2f}, "
                    f"крупные {zone['large_flow']:+.2f}")

    score = max(-3.0, min(3.0, score))
    direction = 'up' if score > 0.3 else ('down' if score < -0.3 else 'neutral')
    confidence = min(90, int(abs(score) / 3.0 * 100)) if direction != 'neutral' else 0

    # Цель — следующий уровень ЗА текущим, а не тот, внутри которого стоим
    def _next_zone(up: bool) -> dict | None:
        pool = [z for z in zones if z is not inside]
        if up:
            above = [z for z in pool if z['price'] > price]
            return min(above, key=lambda z: z['price']) if above else None
        below = [z for z in pool if z['price'] < price]
        return max(below, key=lambda z: z['price']) if below else None

    target_zone = _next_zone(direction == 'up') if direction != 'neutral' else None
    target = target_zone['price'] if target_zone else None
    target_pct = round((target / price - 1) * 100, 3) if target else None
    # Цель ближе 0.02 % — это уже не движение, а шум
    if target_pct is not None and abs(target_pct) < 0.02:
        target, target_pct = None, None

    if target:
        reasons.append(f'цель {target:.3f} ({target_pct:+.2f}%)')
    if state := ('in_zone' if inside else 'between'):
        pass

    title = {'up': 'Вероятнее движение вверх',
             'down': 'Вероятнее движение вниз',
             'neutral': 'Явного перевеса нет'}[direction]

    return {
        'direction': direction,
        'confidence': confidence,
        'score': round(score, 2),
        'state': 'in_zone' if inside else 'between',
        'target': target,
        'target_pct': target_pct,
        'title': title,
        'detail': '; '.join(reasons),
    }
