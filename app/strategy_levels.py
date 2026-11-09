"""Единственная торговая стратегия: покупка от уровней (только лонг).

Два входа, оба в лонг:

* **разворот** — цена подошла к поддержке, агрессор покупает (дельта за окно
  положительная), либо бар проколол зону и закрылся обратно над ней;
* **продолжение** — цена закрылась выше сопротивления с объёмом выше среднего
  и положительной дельтой.

Уровни берутся **только из данных до начала торгового дня** (или до начала
окна) — иначе бэктест «увидит будущее»: зона, построенная по дневному профилю,
уже содержит объём, который придёт после входа. Для истории используется
накопленный `level_stats`, при его отсутствии — сделки/минутки до точки отсчёта.

Выходы: стоп за противоположной границей зоны, цель — следующая зона сверху
(или тейк-профит по проценту), ограничение по времени удержания.
"""

from __future__ import annotations

from datetime import datetime

from app import behavior, levels
from app.screener import MSK

KEY = 'levels_long'
LABEL = 'Покупка от уровней (лонг)'
DESCRIPTION = ('Разворот от поддержки или продолжение после пробоя сопротивления. '
               'Только лонг, одна позиция за раз.')

# Параметры в минутах и процентах — не зависят от выбранного таймфрейма.
PARAMS = [
    {'key': 'warmup_min', 'label': 'Прогрев уровней, минут', 'default': 60, 'min': 0, 'max': 240, 'step': 5},
    {'key': 'approach_pct', 'label': 'Подход к зоне, %', 'default': 0.05, 'min': 0.01, 'max': 1.0, 'step': 0.01},
    {'key': 'break_margin_pct', 'label': 'Пробой за зону, %', 'default': 0.03, 'min': 0.01, 'max': 1.0, 'step': 0.01},
    {'key': 'confirm_min', 'label': 'Подтверждение дельтой, минут', 'default': 5, 'min': 1, 'max': 60, 'step': 1},
    {'key': 'confirm_delta', 'label': 'Порог дельты', 'default': 0.15, 'min': 0.0, 'max': 0.9, 'step': 0.05},
    {'key': 'volume_factor', 'label': 'Объём выше среднего, ×', 'default': 1.2, 'min': 0.5, 'max': 5.0, 'step': 0.1},
    {'key': 'min_hold_rate', 'label': 'Мин. hold rate зоны', 'default': 0.0, 'min': 0.0, 'max': 1.0, 'step': 0.05},
    {'key': 'max_zone_width_pct', 'label': 'Макс. ширина зоны, %', 'default': 0.25, 'min': 0.02, 'max': 2.0, 'step': 0.01},
    {'key': 'cooldown_min', 'label': 'Пауза между сигналами, минут', 'default': 30, 'min': 0, 'max': 240, 'step': 5},
    {'key': 'min_rr', 'label': 'Мин. R:R (цель / стоп)', 'default': 1.5, 'min': 0.5, 'max': 5.0, 'step': 0.1},
    {'key': 'atr_mult', 'label': 'Стоп в ATR, ×', 'default': 1.0, 'min': 0.0, 'max': 5.0, 'step': 0.1},
    # Цель — только предохранитель: движение ведёт трейлинг-стоп, иначе
    # выходим на первом же уровне, не дав тренду раскрыться.
    # Консервативно по умолчанию: цель близкая, трейлинг выключен. Проверено
    # на полном дне — широкая цель вместе с трейлом дают хуже (фильтр R:R
    # перестаёт отсекать слабые входы, и в выборку попадает мусор).
    # Для трендовых дней трейлинг включается параметрами ниже.
    {'key': 'take_profit_pct', 'label': 'Тейк-профит, %', 'default': 0.4, 'min': 0.05, 'max': 20.0, 'step': 0.05},
    {'key': 'trail_atr', 'label': 'Трейлинг-стоп, ATR (0 — выкл)', 'default': 0.0, 'min': 0.0, 'max': 6.0, 'step': 0.25},
    {'key': 'breakeven_atr', 'label': 'Безубыток после, ATR (0 — выкл)', 'default': 0.0, 'min': 0.0, 'max': 6.0, 'step': 0.25},
    {'key': 'stop_loss_pct', 'label': 'Стоп-лосс, %', 'default': 0.25, 'min': 0.05, 'max': 5.0, 'step': 0.05},
    # Для дивидендных бумаг стоп часто вреден: выбивает из позиции на шуме,
    # а смысл владения — дивиденды и восстановление цены.
    {'key': 'use_stop', 'label': 'Стоп (1 — да, 0 — нет)', 'default': 1, 'min': 0, 'max': 1, 'step': 1},
    {'key': 'max_hold_min', 'label': 'Держать максимум, минут', 'default': 120, 'min': 1, 'max': 600, 'step': 5},
]


def no_stop_tickers() -> set:
    """Тикеры, для которых стоп не применяется (дивидендные).

    Список задаётся в окружении: SCREENER_NO_STOP_TICKERS=SBER,GAZP,…
    """
    import os

    raw = (os.environ.get('SCREENER_NO_STOP_TICKERS') or '').strip()
    return {t.strip().upper() for t in raw.split(',') if t.strip()}


def catalog() -> list[dict]:
    return [{'key': KEY, 'label': LABEL, 'description': DESCRIPTION, 'params': PARAMS}]


def _resolve(raw: dict | None) -> dict:
    raw = raw or {}
    params = {}
    for spec in PARAMS:
        value = raw.get(spec['key'], spec['default'])
        try:
            value = float(value)
        except (TypeError, ValueError):
            value = spec['default']
        if spec['key'].endswith('_min'):
            value = int(round(value))
        params[spec['key']] = max(spec['min'], min(spec['max'], value))
    return params


def zones_before(ticker: str, before_ns: int, *, days: int = 30,
                 large_rub: float = 1_000_000.0) -> list[dict]:
    """Зоны по данным строго до `before_ns` — без заглядывания в будущее."""
    try:
        data = levels.analyze(ticker, before_ns - days * 86400 * 1_000_000_000,
                              before_ns - 60 * 1_000_000_000, history_days=days,
                              large_rub=large_rub)
    except levels.LevelsError:
        return []
    zones = [z for z in data['zones'] if z.get('origin') != 'edge']
    for zone in zones:
        zone.setdefault('touches', 0)
        zone.setdefault('hold_rate', None)
    return zones


def _delta_series(bars: list[dict], window: int) -> list[float]:
    """Дельта покупок/продаж за окно, нормированная на объём окна (−1…+1)."""
    result, volumes, deltas = [], [], []
    sum_volume = sum_delta = 0.0
    for bar in bars:
        volume = bar['volume'] or 0
        delta = (bar['buy_volume'] or 0) - (bar['sell_volume'] or 0)
        volumes.append(volume)
        deltas.append(delta)
        sum_volume += volume
        sum_delta += delta
        if len(volumes) > window:
            sum_volume -= volumes.pop(0)
            sum_delta -= deltas.pop(0)
        result.append(sum_delta / sum_volume if sum_volume > 0 else 0.0)
    return result


def signals(ticker: str, bars: list[dict], zones: list[dict], params: dict,
            start_index: int = 0) -> list[dict]:
    """Сигналы на покупку: разворот от поддержки и пробой сопротивления."""
    if not bars or not zones:
        return []

    step_min = 1
    if len(bars) > 1:
        step_min = max(1, round((bars[1]['minute_ns'] - bars[0]['minute_ns']) / 60e9))
    confirm_bars = max(1, round(params['confirm_min'] / step_min))
    deltas = _delta_series(bars, confirm_bars)
    avg_window = max(confirm_bars, round(30 / step_min))

    volumes = [bar['volume'] or 0 for bar in bars]
    supports = [z for z in zones if z['side'] == 'support']
    resistances = [z for z in zones if z['side'] == 'resistance']

    # Широкие зоны («полоса на весь день») для входа бесполезны: касание её
    # верхней границы ничего не говорит о поведении на уровне.
    def narrow(zone: dict) -> bool:
        width = (zone['high'] - zone['low']) / max(zone['price'], 1e-9) * 100
        return width <= params['max_zone_width_pct']

    supports = [z for z in supports if narrow(z)]
    resistances = [z for z in resistances if narrow(z)]
    cooldown_bars = max(0, round(params['cooldown_min'] / step_min))
    last_signal_index = -10 ** 9

    found = []
    for index in range(max(1, start_index), len(bars)):
        bar = bars[index]
        prev = bars[index - 1]
        price = bar['close']
        if price <= 0:
            continue
        if index - last_signal_index < cooldown_bars:
            continue
        approach = price * params['approach_pct'] / 100
        margin = price * params['break_margin_pct'] / 100
        avg_volume = sum(volumes[max(0, index - avg_window):index]) / max(1, min(index, avg_window))
        volume_ok = avg_volume > 0 and (bar['volume'] or 0) >= params['volume_factor'] * avg_volume
        delta_ok = deltas[index] >= params['confirm_delta']
        candle_range = max(1e-9, bar['high'] - bar['low'])
        strong_bar = (bar['close'] - bar['open']) / candle_range >= 0.35

        # ── разворот: касание поддержки и возврат над нижней границей ──
        for zone in supports:
            if zone.get('hold_rate') is not None and zone['hold_rate'] < params['min_hold_rate']:
                continue
            touched = bar['low'] <= zone['high'] + approach and bar['low'] >= zone['low'] - approach
            approached_from_above = prev['close'] >= zone['high']
            closed_back = price >= zone['low']
            # Подтверждение: агрессор покупает либо бар явно выкуплен
            if touched and approached_from_above and closed_back and (delta_ok or strong_bar):
                found.append({
                    'time': bar['minute_ns'], 'price': price, 'kind': 'reversal',
                    'kind_label': 'разворот', 'zone': zone,
                    'delta': round(deltas[index], 3),
                    'volume_ratio': round((bar['volume'] or 0) / avg_volume, 2) if avg_volume else 0,
                })
                last_signal_index = index
                break
        else:
            # ── продолжение: закрытие выше сопротивления с объёмом ──
            for zone in resistances:
                if zone.get('hold_rate') is not None and zone['hold_rate'] < params['min_hold_rate']:
                    continue
                if prev['close'] <= zone['high'] and price >= zone['high'] + margin and volume_ok and delta_ok:
                    found.append({
                        'time': bar['minute_ns'], 'price': price, 'kind': 'breakout',
                        'kind_label': 'пробой', 'zone': zone,
                        'delta': round(deltas[index], 3),
                        'volume_ratio': round((bar['volume'] or 0) / avg_volume, 2) if avg_volume else 0,
                    })
                    last_signal_index = index
                    break
    return found


def simulate(bars: list[dict], zone_signals: list[dict], params: dict, *,
             notional: float = 100_000.0, fee_pct: float = 0.05,
             zones: list[dict] | None = None, ticker: str = '',
             use_stop: bool | None = None) -> dict:
    """Лонг-только: вход по открытию следующего бара, стоп за зоной, цель — следующая зона."""
    zones = zones or []
    by_time = {bar['minute_ns']: index for index, bar in enumerate(bars)}
    step_min = 1
    if len(bars) > 1:
        step_min = max(1, round((bars[1]['minute_ns'] - bars[0]['minute_ns']) / 60e9))
    max_hold = max(1, round(params['max_hold_min'] / step_min))
    fee_rate = fee_pct / 100
    decimals = _decimals(bars)
    # Стоп либо выключен явно, либо не применяется к дивидендным бумагам
    atr_series = _atr_series(bars)
    skipped_rr = 0
    skipped_times: list[int] = []
    rr_values: list[float] = []
    stop_enabled = bool(params.get('use_stop', 1)) if use_stop is None else bool(use_stop)
    if ticker and ticker.upper() in no_stop_tickers():
        stop_enabled = False

    trades = []
    equity = []
    running = 0.0
    position = None
    pending = None
    signal_index = {by_time[s['time']]: s for s in zone_signals if s['time'] in by_time}

    def next_zone_above(price: float, zone: dict) -> float | None:
        candidates = [z['price'] for z in zones if z['price'] > max(price, zone['high'])]
        return min(candidates) if candidates else None

    for index, bar in enumerate(bars):
        if position is None and pending is not None:
            price = bar['open']
            if price > 0:
                shares = max(1, int(notional // price))
                position = {'entry_price': price, 'entry_time': bar['minute_ns'],
                            'shares': shares, 'bars': 0,
                            'stop': pending['stop'], 'target': pending['target'],
                            'kind': pending['kind'], 'kind_label': pending['kind_label'],
                            'zone_price': pending['zone_price'], 'best': price,
                            'stop0': pending['stop']}
            pending = None

        if position is not None:
            position['bars'] += 1
            exit_price = None
            reason = ''
            if stop_enabled and bar['low'] <= position['stop']:
                exit_price, reason = round(min(bar['open'], position['stop']), decimals), 'stop'
            elif position['target'] and bar['high'] >= position['target']:
                exit_price, reason = round(max(bar['open'], position['target']), decimals), 'target'
            elif position['bars'] >= max_hold:
                exit_price, reason = round(bar['close'], decimals), 'time'
            # Управление прибылью ведём после закрытия бара: стоп для текущего
            # бара не должен «подтягиваться» его же максимумом.
            if position is not None and stop_enabled:
                position['best'] = max(position['best'], bar['high'])
                atr_now = atr_series[index] if index < len(atr_series) else 0.0
                profit = position['best'] - position['entry_price']
                # 1) как только прибыль перекрыла ATR — стоп в безубыток,
                #    чтобы хороший вход не превращался в минус;
                if (params['breakeven_atr'] > 0
                        and profit >= params['breakeven_atr'] * atr_now):
                    breakeven = position['entry_price'] * 1.0005
                    if breakeven > position['stop']:
                        position['stop'] = round(breakeven, decimals)
                # 2) дальше ведём трейлинг по ATR
                if params['trail_atr'] > 0:
                    trail = position['best'] - params['trail_atr'] * atr_now
                    if trail > position['stop']:
                        position['stop'] = round(trail, decimals)

            if exit_price is not None:
                gross = (exit_price - position['entry_price']) * position['shares']
                fees = (position['entry_price'] + exit_price) * position['shares'] * fee_rate
                pnl = gross - fees
                trades.append({
                    'entry_time': position['entry_time'] // 1_000_000_000 + 3 * 3600,
                    'entry_price': position['entry_price'],
                    'exit_time': bar['minute_ns'] // 1_000_000_000 + 3 * 3600,
                    'exit_price': exit_price,
                    'side': 'long', 'shares': position['shares'], 'bars': position['bars'],
                    'reason': reason, 'kind': position['kind'],
                    'kind_label': position['kind_label'],
                    'zone_price': position['zone_price'],
                    'stop': position['stop'], 'target': position['target'],
                    'stop_initial': position['stop0'],
                    'gross': round(gross, 2), 'fees': round(fees, 2),
                    'pnl': round(pnl, 2),
                    'pnl_pct': round(pnl / notional * 100, 3),
                })
                running += pnl
                equity.append({'time': bar['minute_ns'] // 1_000_000_000 + 3 * 3600,
                               'value': round(running, 2)})
                position = None

        if position is None and pending is None and index in signal_index and index < len(bars) - 1:
            item = signal_index[index]
            zone = item['zone']
            entry_ref = bar['close']
            # Стоп: не ближе ATR — иначе шум выбивает из позиции. Плюс запас
            # под границей зоны, если зона ниже.
            atr = atr_series[index] if index < len(atr_series) else 0.0
            stop_by_pct = entry_ref * (1 - params['stop_loss_pct'] / 100)
            stop_by_atr = entry_ref - params['atr_mult'] * atr
            stop_by_zone = zone['low'] * (1 - 0.1 / 100)
            stop = min(stop_by_pct, stop_by_atr, stop_by_zone) if stop_enabled else -1.0

            target = next_zone_above(entry_ref, zone)
            cap = entry_ref * (1 + params['take_profit_pct'] / 100)
            if target is None or target > cap:
                target = cap

            # Отсекаем сделки, где цель ближе min_rr стопов: именно из них
            # получались «+58 при −958». Без стопа (дивидендные бумаги) риск
            # всё равно оцениваем — по ATR, иначе фильтр не работает вовсе.
            risk = entry_ref - stop if stop_enabled else max(
                params['atr_mult'] * atr, entry_ref * params['stop_loss_pct'] / 100)
            reward = (target - entry_ref) if target else 0.0
            if risk > 0:
                rr = reward / risk
                rr_values.append(rr)
                if rr < params['min_rr']:
                    skipped_rr += 1
                    skipped_times.append(item['time'])
                    continue

            stop = round(stop, decimals) if stop_enabled else -1.0
            target = round(target, decimals) if target else None
            pending = {'stop': stop, 'target': target, 'kind': item['kind'],
                       'kind_label': item['kind_label'], 'zone_price': zone['price']}

    metrics = _metrics(trades, running, notional)
    metrics['skipped_rr'] = skipped_rr
    metrics['avg_rr'] = round(sum(rr_values) / len(rr_values), 2) if rr_values else 0.0
    return {'trades': trades, 'equity': equity, 'metrics': metrics,
            'markers': _markers(trades), 'stop_enabled': stop_enabled,
            'skipped_times': skipped_times}


def _atr_series(bars: list[dict], window: int = 14) -> list[float]:
    """ATR по истинному диапазону — база для стопа, не зависящая от «шума»."""
    result = []
    ranges = []
    prev_close = None
    for bar in bars:
        high, low = bar['high'], bar['low']
        true_range = high - low if prev_close is None else max(
            high - low, abs(high - prev_close), abs(low - prev_close))
        ranges.append(true_range)
        prev_close = bar['close']
        window_values = ranges[-window:]
        result.append(sum(window_values) / len(window_values))
    return result


def _decimals(bars: list[dict]) -> int:
    if not bars:
        return 2
    text = f"{bars[0]['close']:.6f}".rstrip('0')
    return min(4, len(text.split('.')[1]) if '.' in text else 0)


def _metrics(trades: list[dict], total: float, notional: float) -> dict:
    if not trades:
        return {'trades': 0, 'net_pnl': 0.0, 'gross_pnl': 0.0, 'total_return_pct': 0.0,
                'win_rate': 0.0, 'profit_factor': 0.0, 'avg_win': 0.0, 'avg_loss': 0.0,
                'expectancy': 0.0, 'max_drawdown': 0.0, 'max_drawdown_pct': 0.0,
                'fees': 0.0, 'sharpe': 0.0, 'best': 0.0, 'worst': 0.0, 'avg_bars': 0.0}
    pnls = [t['pnl'] for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    peak = running = drawdown = 0.0
    for pnl in pnls:
        running += pnl
        peak = max(peak, running)
        drawdown = max(drawdown, peak - running)
    import statistics
    stdev = statistics.pstdev(pnls) if len(pnls) > 1 else 0.0
    return {
        'trades': len(trades), 'net_pnl': round(total, 2),
        'gross_pnl': round(sum(t['gross'] for t in trades), 2),
        'total_return_pct': round(total / notional * 100, 2),
        'win_rate': round(len(wins) / len(trades) * 100, 1),
        'profit_factor': round(sum(wins) / abs(sum(losses)), 2) if losses else 0.0,
        'avg_win': round(sum(wins) / len(wins), 2) if wins else 0.0,
        'avg_loss': round(sum(losses) / len(losses), 2) if losses else 0.0,
        'expectancy': round(sum(pnls) / len(pnls), 2),
        'max_drawdown': round(drawdown, 2),
        'max_drawdown_pct': round(drawdown / notional * 100, 2),
        'fees': round(sum(t['fees'] for t in trades), 2),
        'sharpe': round(statistics.mean(pnls) / stdev, 2) if stdev else 0.0,
        'best': round(max(pnls), 2), 'worst': round(min(pnls), 2),
        'avg_bars': round(sum(t['bars'] for t in trades) / len(trades), 1),
    }


def _markers(trades: list[dict]) -> list[dict]:
    markers = []
    for trade in trades:
        markers.append({'time': trade['entry_time'], 'position': 'belowBar',
                        'color': '#1a7f37', 'shape': 'arrowUp',
                        'text': 'Покупать'})
        markers.append({'time': trade['exit_time'],
                        'position': 'aboveBar' if trade['pnl'] >= 0 else 'belowBar',
                        'color': '#1a7f37' if trade['pnl'] >= 0 else '#c1121f',
                        'shape': 'circle', 'text': f"{trade['pnl']:+.0f} ₽"})
    markers.sort(key=lambda m: m['time'])
    return markers


def analyze_range(ticker: str, from_ns: int, to_ns: int, raw_params: dict | None = None, *,
                  interval: int = 1, notional: float = 100_000.0,
                  fee_pct: float = 0.05) -> dict:
    """Полный разбор диапазона: зоны, сигналы на покупку, сделки и метрики."""
    from app import chartdata

    params = _resolve(raw_params)
    prepared = chartdata.prepared(ticker, from_ns, to_ns, interval)
    bars = prepared['bars']
    if len(bars) < 10:
        raise levels.LevelsError('Недостаточно данных: нужно хотя бы 10 баров')

    warmup_ns = bars[0]['minute_ns'] + int(params['warmup_min'] * 60 * 1_000_000_000)
    zones = zones_before(ticker, warmup_ns)
    start_index = next((i for i, b in enumerate(bars) if b['minute_ns'] >= warmup_ns), len(bars))
    found = signals(ticker, bars, zones, params, start_index)
    result = simulate(bars, found, params, notional=notional, fee_pct=fee_pct, zones=zones,
                      ticker=ticker)

    skipped = set(result.get('skipped_times', []))
    signal_rows = [{
        'taken': item['time'] not in skipped,
        'time': item['time'] // 1_000_000_000 + 3 * 3600,
        'price': round(item['price'], 4),
        'kind': item['kind'],
        'kind_label': item['kind_label'],
        'zone_price': round(item['zone']['price'], 4),
        'zone_low': round(item['zone']['low'], 4),
        'zone_high': round(item['zone']['high'], 4),
        'delta': item['delta'],
        'volume_ratio': item['volume_ratio'],
    } for item in found]

    return {
        'ticker': ticker.upper(),
        'strategy': KEY,
        'label': LABEL,
        'params': params,
        'interval': interval,
        'notional': notional,
        'fee_pct': fee_pct,
        'bars': len(bars),
        'warmup_min': params['warmup_min'],
        'zones': zones,
        'signals': signal_rows,
        'trades': result['trades'],
        'equity': result['equity'],
        'metrics': result['metrics'],
        'markers': result['markers'],
        'stop_enabled': result['stop_enabled'],
        'no_stop_tickers': sorted(no_stop_tickers()),
    }


def signal_markers(rows: list[dict]) -> list[dict]:
    """Входы «Покупать» — принятые зелёным, отсеянные серым (не торгуем)."""
    markers = []
    for row in rows:
        taken = row.get('taken', True)
        markers.append({
            'time': row['time'],
            'position': 'belowBar' if taken else 'aboveBar',
            'color': '#1a7f37' if taken else '#adb5bd',
            'shape': 'arrowUp' if taken else 'circle',
            'text': ('Покупка' if row['kind'] == 'breakout' else 'Отскок') if taken
                    else 'отсеян',
        })
    markers.sort(key=lambda m: m['time'])
    return markers
