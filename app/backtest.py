"""Бэктест intraday-стратегий на минутных свечах.

Сделки считаются по данным `market_bar_1m`: там есть OHLC, объём, дельта
покупок/продаж и крупные принты по сторонам — этого достаточно, чтобы
воспроизвести логику объёмного анализа без сырого тика.

Модель исполнения намеренно консервативная:

* сигнал считается по **закрытию** бара, вход — по **открытию следующего**
  (никакого заглядывания в будущее);
* внутри бара сначала проверяется стоп, потом тейк (худший порядок);
* одна позиция за раз, фиксированный номинал, комиссия с обеих сторон.
"""

from __future__ import annotations

import logging
import math
import statistics
from datetime import datetime, timezone
from collections import deque

from app import chartdata
from app.screener import MSK

STRATEGIES: dict[str, dict] = {
    'delta_momentum': {
        'label': 'Импульс дельты',
        'description': 'Вход по перевесу покупок/продаж за окно при повышенном объёме',
        'params': [
            {'key': 'window_min', 'label': 'Окно, минут', 'default': 15, 'min': 1, 'max': 240, 'step': 1},
            {'key': 'threshold', 'label': 'Порог дельты', 'default': 0.35, 'min': 0.05, 'max': 0.95, 'step': 0.05},
            {'key': 'volume_factor', 'label': 'Объём выше среднего, ×', 'default': 1.2, 'min': 0.5, 'max': 5.0, 'step': 0.1},
            {'key': 'take_profit_pct', 'label': 'Тейк-профит, %', 'default': 0.6, 'min': 0.05, 'max': 10.0, 'step': 0.05},
            {'key': 'stop_loss_pct', 'label': 'Стоп-лосс, %', 'default': 0.3, 'min': 0.05, 'max': 10.0, 'step': 0.05},
            {'key': 'max_hold_min', 'label': 'Держать максимум, минут', 'default': 30, 'min': 1, 'max': 600, 'step': 1},
        ],
    },
    'vwap_reversion': {
        'label': 'Возврат к VWAP',
        'description': 'Вход на отклонении от VWAP на k сигм, против движения, '
                       'при подтверждении дельтой за окно',
        'params': [
            # Абсолютный процент не работает: у SBER отклонение от VWAP редко
            # превышает 0.1%, а у NVTK доходит до 0.25% за тот же час. Поэтому
            # порог задаётся в сигмах волатильности самих баров.
            {'key': 'deviation_sigma', 'label': 'Отклонение, σ', 'default': 2.0, 'min': 0.5, 'max': 6.0, 'step': 0.1},
            {'key': 'min_deviation_pct', 'label': 'Мин. отклонение, %', 'default': 0.03, 'min': 0.0, 'max': 1.0, 'step': 0.01},
            {'key': 'sigma_window_min', 'label': 'Окно σ, минут', 'default': 30, 'min': 1, 'max': 240, 'step': 1},
            {'key': 'confirm_window_min', 'label': 'Окно подтверждения, минут', 'default': 5, 'min': 1, 'max': 60, 'step': 1},
            {'key': 'confirm_delta', 'label': 'Порог дельты', 'default': 0.1, 'min': 0.0, 'max': 0.9, 'step': 0.05},
            # Экономика важнее формы: входить, только если отклонение хотя бы
            # в cost_multiplier раз больше круговых издержек. 0 — не учитывать.
            {'key': 'cost_multiplier', 'label': 'Порог × издержки', 'default': 1.5, 'min': 0.0, 'max': 10.0, 'step': 0.1},
            {'key': 'take_profit_pct', 'label': 'Тейк-профит, %', 'default': 0.5, 'min': 0.05, 'max': 10.0, 'step': 0.05},
            {'key': 'stop_loss_pct', 'label': 'Стоп-лосс, %', 'default': 0.5, 'min': 0.05, 'max': 10.0, 'step': 0.05},
            {'key': 'max_hold_min', 'label': 'Держать максимум, минут', 'default': 60, 'min': 1, 'max': 600, 'step': 1},
        ],
    },
    'absorption': {
        'label': 'Накопление / распределение',
        'description': 'Объём выше нормы, цена стоит, дельта перевешивает в одну сторону',
        'params': [
            {'key': 'window_min', 'label': 'Окно объёма, минут', 'default': 30, 'min': 1, 'max': 240, 'step': 1},
            {'key': 'volume_factor', 'label': 'Объём выше среднего, ×', 'default': 2.0, 'min': 1.0, 'max': 10.0, 'step': 0.1},
            {'key': 'max_range_pct', 'label': 'Макс. ход бара, %', 'default': 0.15, 'min': 0.01, 'max': 3.0, 'step': 0.01},
            {'key': 'threshold', 'label': 'Порог дельты', 'default': 0.5, 'min': 0.05, 'max': 0.95, 'step': 0.05},
            {'key': 'take_profit_pct', 'label': 'Тейк-профит, %', 'default': 0.7, 'min': 0.05, 'max': 10.0, 'step': 0.05},
            {'key': 'stop_loss_pct', 'label': 'Стоп-лосс, %', 'default': 0.4, 'min': 0.05, 'max': 10.0, 'step': 0.05},
            {'key': 'max_hold_min', 'label': 'Держать максимум, минут', 'default': 60, 'min': 1, 'max': 600, 'step': 1},
        ],
    },
    'large_print_follow': {
        'label': 'За крупным принтом',
        'description': 'Вход, когда крупные сделки перевешивают на одной стороне',
        'params': [
            {'key': 'min_large_value', 'label': 'Крупные от, ₽ за бар', 'default': 2000000, 'min': 100000, 'max': 100000000, 'step': 100000},
            {'key': 'imbalance', 'label': 'Перевес крупных (0..1)', 'default': 0.6, 'min': 0.1, 'max': 1.0, 'step': 0.05},
            {'key': 'take_profit_pct', 'label': 'Тейк-профит, %', 'default': 0.6, 'min': 0.05, 'max': 10.0, 'step': 0.05},
            {'key': 'stop_loss_pct', 'label': 'Стоп-лосс, %', 'default': 0.4, 'min': 0.05, 'max': 10.0, 'step': 0.05},
            {'key': 'max_hold_min', 'label': 'Держать максимум, минут', 'default': 45, 'min': 1, 'max': 600, 'step': 1},
        ],
    },
}


class BacktestError(ValueError):
    """Некорректные параметры бэктеста."""


def strategy_catalog() -> list[dict]:
    return [{'key': key, **{k: v for k, v in cfg.items() if k != 'params'},
             'params': cfg['params']} for key, cfg in STRATEGIES.items()]


def _resolve_params(strategy: str, raw: dict | None) -> dict:
    cfg = STRATEGIES.get(strategy)
    if cfg is None:
        raise BacktestError(f'Неизвестная стратегия: {strategy}')
    params = {}
    raw = raw or {}
    for spec in cfg['params']:
        value = raw.get(spec['key'], spec['default'])
        try:
            value = float(value)
        except (TypeError, ValueError):
            raise BacktestError(f'{spec["label"]}: ожидается число')
        if spec['key'].endswith('_min'):
            value = int(round(value))
        params[spec['key']] = max(spec['min'], min(spec['max'], value))
    return params


def _bars(minutes: float, interval: int) -> int:
    """Минуты → количество баров, но не меньше одного."""
    return max(1, int(round(float(minutes) / max(1, interval))))


def _rolling_average(values, window: int) -> list[float]:
    out, total, q = [], 0.0, deque()
    for value in values:
        q.append(value)
        total += value
        if len(q) > window:
            total -= q.popleft()
        out.append(total / len(q))
    return out


def signals(strategy: str, bars: list[dict], params: dict,
            interval: int = 1, fee_pct: float = 0.0) -> list[int]:
    """+1 — лонг, −1 — шорт, 0 — нет сигнала (по закрытию бара)."""
    result = [0] * len(bars)
    if not bars:
        return result

    if strategy in ('delta_momentum', 'absorption'):
        window = _bars(params['window_min'], interval)
        # Дельта окна — это Σ(buy−sell) / Σобъём за окно, а не средняя дельта,
        # поделённая на объём текущего бара: только так величина лежит в −1…+1
        # и совпадает с Δ в скринере.
        volumes = deque()
        deltas = deque()
        sum_volume = sum_delta = 0.0

        for i, bar in enumerate(bars):
            volume = bar['volume'] or 0
            delta = (bar['buy_volume'] or 0) - (bar['sell_volume'] or 0)
            volumes.append(volume)
            deltas.append(delta)
            sum_volume += volume
            sum_delta += delta
            if len(volumes) > window:
                sum_volume -= volumes.popleft()
                sum_delta -= deltas.popleft()

            if volume <= 0 or len(volumes) < window or sum_volume <= 0:
                continue
            ratio = sum_delta / sum_volume
            avg_volume = sum_volume / len(volumes)
            boost = volume >= params['volume_factor'] * avg_volume
            if strategy == 'delta_momentum':
                if boost and ratio >= params['threshold']:
                    result[i] = 1
                elif boost and ratio <= -params['threshold']:
                    result[i] = -1
            else:
                move = abs(bar['close'] - bar['open']) / bar['open'] * 100 if bar['open'] else 100
                if (boost and move <= params['max_range_pct']
                        and ratio >= params['threshold']):
                    result[i] = 1
                elif (boost and move <= params['max_range_pct']
                        and ratio <= -params['threshold']):
                    result[i] = -1

    elif strategy == 'vwap_reversion':
        sigma_window = _bars(params['sigma_window_min'], interval)
        confirm_window = _bars(params['confirm_window_min'], interval)
        floor = params['min_deviation_pct'] / 100

        returns = []
        prev_close = None
        for bar in bars:
            returns.append((bar['close'] / prev_close - 1) if prev_close else 0.0)
            prev_close = bar['close']

        confirm_queue = deque()
        sum_volume = sum_delta = 0.0

        for i, bar in enumerate(bars):
            volume = bar['volume'] or 0
            delta = (bar['buy_volume'] or 0) - (bar['sell_volume'] or 0)
            confirm_queue.append((volume, delta))
            sum_volume += volume
            sum_delta += delta
            if len(confirm_queue) > confirm_window:
                old_volume, old_delta = confirm_queue.popleft()
                sum_volume -= old_volume
                sum_delta -= old_delta

            vwap = bar.get('session_vwap') or 0
            if vwap <= 0 or i < sigma_window:
                continue

            window_returns = returns[i - sigma_window + 1:i + 1]
            sigma = statistics.pstdev(window_returns) if len(window_returns) > 1 else 0.0
            # Порог: k сигм, но не меньше floor — иначе на «мёртвом» рынке
            # стратегия входит на каждом шуме.
            # Круговые издержки: комиссия с входа и с выхода.
            cost = 2 * fee_pct / 100
            threshold = max(floor, params['deviation_sigma'] * sigma,
                            params.get('cost_multiplier', 0.0) * cost)
            if threshold <= 0 or sum_volume <= 0:
                continue

            deviation = bar['close'] / vwap - 1
            ratio = sum_delta / sum_volume
            if deviation <= -threshold and ratio >= params['confirm_delta']:
                result[i] = 1       # цена ниже VWAP, но покупатели выкупают
            elif deviation >= threshold and ratio <= -params['confirm_delta']:
                result[i] = -1

    elif strategy == 'large_print_follow':
        for i, bar in enumerate(bars):
            large_buy = bar['large_buy_turnover'] or 0
            large_sell = bar['large_sell_turnover'] or 0
            total = large_buy + large_sell
            if total < params['min_large_value'] or total <= 0:
                continue
            imbalance = (large_buy - large_sell) / total
            if imbalance >= params['imbalance']:
                result[i] = 1
            elif imbalance <= -params['imbalance']:
                result[i] = -1

    return result


def _price_decimals(bars: list[dict]) -> int:
    """Точность котировки из самих сделок: 294.13 → 2 знака, 0.0125 → 4."""
    decimals = 0
    for bar in bars[:500]:
        for field in ('open', 'close'):
            text = f"{bar[field]:.6f}".rstrip('0')
            if '.' in text:
                decimals = max(decimals, len(text.split('.')[1]))
    return min(decimals, 4)


def _exit_hit(position: dict, bar: dict, params: dict, strategy: str,
              decimals: int) -> tuple[float, str] | None:
    """Стоп/тейк/VWAP внутри бара.

    Стоп проверяем первым — худший сценарий. Цены исполнения:
    * стоп при гэпе через уровень — по цене открытия (хуже для нас);
    * тейк при гэпе — тоже по открытию (лучше для нас, но это реально);
    * VWAP — по самому уровню, приведённому к сетке цены инструмента,
      иначе получаются «цены» вроде 294.1134, которых на бирже не бывает.
    """
    entry = position['entry_price']
    sl = params['stop_loss_pct'] / 100
    tp = params['take_profit_pct'] / 100
    vwap = bar.get('session_vwap') or 0.0

    if position['dir'] > 0:
        stop_price = entry * (1 - sl)
        take_price = entry * (1 + tp)
        if bar['low'] <= stop_price:
            return round(min(bar['open'], stop_price), decimals), 'stop'
        if bar['high'] >= take_price:
            return round(max(bar['open'], take_price), decimals), 'take'
        if strategy == 'vwap_reversion' and vwap and bar['high'] >= vwap:
            return round(min(max(vwap, bar['low']), bar['high']), decimals), 'vwap'
    else:
        stop_price = entry * (1 + sl)
        take_price = entry * (1 - tp)
        if bar['high'] >= stop_price:
            return round(max(bar['open'], stop_price), decimals), 'stop'
        if bar['low'] <= take_price:
            return round(min(bar['open'], take_price), decimals), 'take'
        if strategy == 'vwap_reversion' and vwap and bar['low'] <= vwap:
            return round(max(min(vwap, bar['high']), bar['low']), decimals), 'vwap'
    return None


def run(ticker: str, from_ns: int, to_ns: int, strategy: str, raw_params: dict | None = None,
        *, interval: int = 1, notional: float = 100_000.0, fee_pct: float = 0.05) -> dict:
    """Прогнать стратегию по инструменту и вернуть сделки, equity и метрики."""
    params = _resolve_params(strategy, raw_params)
    prepared = chartdata.prepared(ticker, from_ns, to_ns, interval)
    bars = prepared['bars']
    if len(bars) < 10:
        raise BacktestError('Недостаточно данных для бэктеста: нужно хотя бы 10 баров')

    sig = signals(strategy, bars, params, interval, fee_pct)
    max_hold = _bars(params['max_hold_min'], interval)
    decimals = _price_decimals(bars)
    fee_rate = fee_pct / 100

    trades: list[dict] = []
    equity: list[dict] = []
    running = 0.0
    position = None
    pending = 0

    for i, bar in enumerate(bars):
        if i > 0 and position is None and pending:
            price = bar['open']
            if price > 0:
                shares = max(1, int(notional // price))
                position = {'dir': pending, 'entry_price': price, 'entry_time': bar['minute_ns'],
                            'entry_index': i, 'shares': shares, 'bars': 0}
            pending = 0

        if position is not None:
            position['bars'] += 1
            hit = _exit_hit(position, bar, params, strategy, decimals)
            reason = hit[1] if hit else ''
            price = hit[0] if hit else None
            if price is None and position['bars'] >= max_hold:
                price, reason = round(bar['close'], decimals), 'time'
            if price is None and sig[i] == -position['dir']:
                price, reason = round(bar['close'], decimals), 'signal'
            if price is not None:
                gross = (price - position['entry_price']) * position['shares'] * position['dir']
                fees = (position['entry_price'] + price) * position['shares'] * fee_rate
                pnl = gross - fees
                trades.append({
                    'entry_time': chartdata._to_chart_time(position['entry_time']),
                    'entry_price': round(position['entry_price'], 4),
                    'exit_time': chartdata._to_chart_time(bar['minute_ns']),
                    'exit_price': round(price, 4),
                    'side': 'long' if position['dir'] > 0 else 'short',
                    'shares': position['shares'],
                    'bars': position['bars'],
                    'reason': reason,
                    'gross': round(gross, 2),
                    'fees': round(fees, 2),
                    'pnl': round(pnl, 2),
                    'pnl_pct': round(pnl / notional * 100, 3),
                })
                running += pnl
                equity.append({'time': chartdata._to_chart_time(bar['minute_ns']),
                               'value': round(running, 2)})
                position = None

        if i < len(bars) - 1:
            if position is None and sig[i]:
                pending = sig[i]

    return {
        'ticker': ticker.upper(),
        'strategy': strategy,
        'label': STRATEGIES[strategy]['label'],
        'params': params,
        'interval': interval,
        'notional': notional,
        'fee_pct': fee_pct,
        'bars': len(bars),
        'from': bars[0]['minute_ns'],
        'to': bars[-1]['minute_ns'],
        'trades': trades,
        'equity': equity,
        'metrics': _metrics(trades, running, notional),
        'markers': _markers(trades),
    }


def _metrics(trades: list[dict], total: float, notional: float) -> dict:
    if not trades:
        return {'trades': 0, 'net_pnl': 0.0, 'gross_pnl': 0.0, 'total_return_pct': 0.0,
                'win_rate': 0.0, 'profit_factor': 0.0, 'avg_win': 0.0,
                'avg_loss': 0.0, 'expectancy': 0.0, 'max_drawdown': 0.0,
                'max_drawdown_pct': 0.0, 'fees': 0.0, 'sharpe': 0.0,
                'best': 0.0, 'worst': 0.0, 'avg_bars': 0.0}

    pnls = [t['pnl'] for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))

    peak = running = drawdown = 0.0
    for pnl in pnls:
        running += pnl
        peak = max(peak, running)
        drawdown = max(drawdown, peak - running)

    stdev = statistics.pstdev(pnls) if len(pnls) > 1 else 0.0
    return {
        'trades': len(trades),
        'net_pnl': round(total, 2),
        'gross_pnl': round(sum(t['gross'] for t in trades), 2),
        'total_return_pct': round(total / notional * 100, 2),
        'win_rate': round(len(wins) / len(trades) * 100, 1),
        'profit_factor': round(gross_profit / gross_loss, 2) if gross_loss else 0.0,
        'avg_win': round(sum(wins) / len(wins), 2) if wins else 0.0,
        'avg_loss': round(sum(losses) / len(losses), 2) if losses else 0.0,
        'expectancy': round(sum(pnls) / len(pnls), 2),
        'max_drawdown': round(drawdown, 2),
        'max_drawdown_pct': round(drawdown / notional * 100, 2),
        'fees': round(sum(t['fees'] for t in trades), 2),
        'sharpe': round(statistics.mean(pnls) / stdev, 2) if stdev else 0.0,
        'best': round(max(pnls), 2),
        'worst': round(min(pnls), 2),
        'avg_bars': round(sum(t['bars'] for t in trades) / len(trades), 1),
    }


def _markers(trades: list[dict]) -> list[dict]:
    """Метки входа/выхода для графика."""
    out = []
    for t in trades:
        out.append({'time': t['entry_time'], 'position': 'belowBar' if t['side'] == 'long' else 'aboveBar',
                    'color': '#198754' if t['side'] == 'long' else '#dc3545',
                    'shape': 'arrowUp' if t['side'] == 'long' else 'arrowDown',
                    'text': f"вход {t['side']}"})
        out.append({'time': t['exit_time'],
                    'position': 'aboveBar' if t['pnl'] >= 0 else 'belowBar',
                    'color': '#198754' if t['pnl'] >= 0 else '#dc3545',
                    'shape': 'circle',
                    'text': f"{t['pnl']:+.0f} ₽"})
    out.sort(key=lambda m: m['time'])
    return out


# ── проверка эффективности по универсуму ────────────────────


def _data_span(tickers: list[str], from_ns: int, to_ns: int) -> tuple[int, int] | None:
    """Фактические границы данных (по минуткам) для набора инструментов."""
    from app import screener

    if not tickers:
        return None
    placeholders = ','.join('?' * len(tickers))
    conn = screener.get_connection()
    try:
        row = conn.execute(
            f'SELECT MIN(minute_ns) AS f, MAX(minute_ns) AS t FROM market_bar_1m '
            f'WHERE minute_ns >= ? AND minute_ns < ? AND ticker IN ({placeholders})',
            [from_ns, to_ns, *tickers]).fetchone()
    finally:
        conn.close()
    if not row or row['f'] is None or row['t'] is None or row['t'] <= row['f']:
        return None
    return int(row['f']), int(row['t']) + 60 * 1_000_000_000


def scan(tickers: list[str], from_ns: int, to_ns: int, strategy: str,
         params: dict | None = None, *, interval: int = 1, notional: float = 100_000.0,
         fee_pct: float = 0.05, split: float = 0.6) -> dict:
    """Прогнать стратегию по списку инструментов и сравнить две половины периода.

    Параметры одни и те же на всём периоде: первая часть (in-sample) показывает,
    что стратегия «видит», вторая (out-of-sample) — сохраняется ли эффект.
    Иначе оптимизация под историю выдаёт красивые цифры без смысла.
    """
    if not 0.1 <= split <= 0.9:
        raise BacktestError('split должен быть в диапазоне 0.1…0.9')
    # Делим не запрошенный календарный диапазон, а фактический период данных:
    # иначе при range=today до начала сбора in-sample оказывается пустой.
    from_ns, to_ns = _data_span(tickers, from_ns, to_ns) or (from_ns, to_ns)
    mid_ns = int(from_ns + (to_ns - from_ns) * split)

    rows = []
    for ticker in tickers:
        try:
            full = run(ticker, from_ns, to_ns, strategy, params, interval=interval,
                       notional=notional, fee_pct=fee_pct)
        except (BacktestError, Exception):
            continue
        if not full['bars']:
            continue
        try:
            in_sample = run(ticker, from_ns, mid_ns, strategy, params, interval=interval,
                            notional=notional, fee_pct=fee_pct)
        except Exception:
            in_sample = {'metrics': _metrics([], 0.0, notional)}
        try:
            out_sample = run(ticker, mid_ns, to_ns, strategy, params, interval=interval,
                             notional=notional, fee_pct=fee_pct)
        except Exception:
            out_sample = {'metrics': _metrics([], 0.0, notional)}

        rows.append({
            'ticker': ticker.upper(),
            'trades': full['metrics']['trades'],
            'gross': full['metrics']['gross_pnl'],
            'fees': full['metrics']['fees'],
            'net': full['metrics']['net_pnl'],
            'win_rate': full['metrics']['win_rate'],
            'profit_factor': full['metrics']['profit_factor'],
            'max_drawdown': full['metrics']['max_drawdown'],
            'in_net': in_sample['metrics']['net_pnl'],
            'out_net': out_sample['metrics']['net_pnl'],
            'in_trades': in_sample['metrics']['trades'],
            'out_trades': out_sample['metrics']['trades'],
        })

    tracked = [r for r in rows if r['trades'] > 0]
    total_trades = sum(r['trades'] for r in rows)
    total_gross = sum(r['gross'] for r in rows)
    total_fees = sum(r['fees'] for r in rows)
    total_net = sum(r['net'] for r in rows)
    wins = [r for r in tracked if r['net'] > 0]
    return {
        'strategy': strategy,
        'label': STRATEGIES[strategy]['label'],
        'params': _resolve_params(strategy, params),
        'interval': interval,
        'notional': notional,
        'fee_pct': fee_pct,
        'split': split,
        'tickers': len(rows),
        'traded_tickers': len(tracked),
        'total_trades': total_trades,
        'gross': round(total_gross, 2),
        'fees': round(total_fees, 2),
        'net': round(total_net, 2),
        'win_tickers': len(wins),
        'out_net': round(sum(r['out_net'] for r in rows), 2),
        'in_net': round(sum(r['in_net'] for r in rows), 2),
        'rows': sorted(rows, key=lambda r: r['net'], reverse=True),
    }


def _print_scan(result: dict) -> None:
    p = result['params']
    print(f"Стратегия: {result['label']} ({result['strategy']}) · {result['interval']}м · "
          f"номинал {result['notional']:,.0f} ₽ · комиссия {result['fee_pct']}%")
    print('Параметры: ' + ', '.join(f'{k}={v:g}' for k, v in p.items()))
    print()
    print(f"{'тикер':8}{'сдел':>5}{'gross':>10}{'комис':>9}{'net':>10}{'винрейт':>9}"
          f"{'PF':>6}{'просад':>9}{'in':>9}{'out':>9}")
    for r in result['rows']:
        print(f"{r['ticker']:8}{r['trades']:>5}{r['gross']:>10.0f}{r['fees']:>9.0f}"
              f"{r['net']:>10.0f}{r['win_rate']:>8.0f}%{r['profit_factor']:>6.2f}"
              f"{r['max_drawdown']:>9.0f}{r['in_net']:>9.0f}{r['out_net']:>9.0f}")
    print()
    print(f"Итого по {result['tickers']} инструментам (торговали {result['traded_tickers']}): "
          f"сделок {result['total_trades']}, gross {result['gross']:,.0f} ₽, "
          f"комиссии {result['fees']:,.0f} ₽, net {result['net']:,.0f} ₽")
    print(f"In-sample: {result['in_net']:,.0f} ₽ · Out-of-sample: {result['out_net']:,.0f} ₽ "
          f"· прибыльных инструментов: {result['win_tickers']}")
    print('(in/out — независимые прогоны: сделки на границе периода в сумму не входят)')


def _universe_tickers(top: int) -> list[str]:
    from app import screener

    conn = screener.get_connection()
    try:
        return [r['ticker'] for r in conn.execute(
            'SELECT ticker FROM screener_instrument WHERE selected=1 '
            'ORDER BY rank LIMIT ?', (top,))]
    finally:
        conn.close()


def _cli(argv: list[str] | None = None) -> int:
    import argparse

    from app.db import init_db
    from app.envfile import load_env_file
    from app import chartdata, screener

    parser = argparse.ArgumentParser(prog='python -m app.backtest',
                                     description='Проверка intraday-стратегий')
    sub = parser.add_subparsers(dest='command', required=True)

    scan_p = sub.add_parser('scan', help='прогнать стратегию по универсуму')
    scan_p.add_argument('--strategy', default='vwap_reversion', choices=list(STRATEGIES))
    scan_p.add_argument('--top', type=int, default=20, help='сколько инструментов брать')
    scan_p.add_argument('--ticker', default='', help='список тикеров через запятую')
    scan_p.add_argument('--tickers-file', default='', help='файл со списком тикеров')
    scan_p.add_argument('--range', default='today', choices=['today', '3d', '7d', '30d', '90d'])
    scan_p.add_argument('--interval', type=int, default=1, choices=list(chartdata.INTERVALS))
    scan_p.add_argument('--notional', type=float, default=100_000.0)
    scan_p.add_argument('--fee', type=float, default=0.05)
    scan_p.add_argument('--split', type=float, default=0.6)
    scan_p.add_argument('--param', action='append', default=[],
                        help='переопределить параметр: --param deviation_sigma=1.5')

    sweep_p = sub.add_parser('sweep', help='перебор параметров на универсуме')
    sweep_p.add_argument('--strategy', default='vwap_reversion', choices=list(STRATEGIES))
    sweep_p.add_argument('--top', type=int, default=20)
    sweep_p.add_argument('--range', default='today', choices=['today', '3d', '7d', '30d', '90d'])
    sweep_p.add_argument('--interval', type=int, default=1, choices=list(chartdata.INTERVALS))
    sweep_p.add_argument('--notional', type=float, default=100_000.0)
    sweep_p.add_argument('--fee', type=float, default=0.05)
    sweep_p.add_argument('--split', type=float, default=0.6)
    sweep_p.add_argument('--grid', action='append', default=[],
                         help='key=v1,v2[,v3]: параметр и список значений')
    sweep_p.add_argument('--sort', default='net', choices=['net', 'out_net', 'gross', 'trades'])
    sweep_p.add_argument('--param', action='append', default=[],
                         help='фиксированный параметр: --param take_profit_pct=1')

    one = sub.add_parser('run', help='подробный прогон по одному инструменту')
    one.add_argument('ticker')
    one.add_argument('--strategy', default='vwap_reversion', choices=list(STRATEGIES))
    one.add_argument('--range', default='today', choices=['today', '3d', '7d', '30d', '90d'])
    one.add_argument('--interval', type=int, default=1, choices=list(chartdata.INTERVALS))
    one.add_argument('--param', action='append', default=[])

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format='%(levelname)s %(message)s')
    load_env_file()
    init_db()

    params = {}
    for raw in getattr(args, 'param', []):
        if '=' not in raw:
            print(f'Ожидается key=value, получено: {raw}')
            return 2
        key, value = raw.split('=', 1)
        params[key.strip()] = value.strip()

    from_ns, to_ns, _ = chartdata.resolve_range(args.range)

    if args.command == 'sweep':
        import itertools

        grid: dict[str, list[str]] = {}
        for raw in args.grid:
            if '=' not in raw:
                print(f'Ожидается key=v1,v2, получено: {raw}')
                return 2
            key, values = raw.split('=', 1)
            grid[key.strip()] = [v.strip() for v in values.split(',') if v.strip()]
        if not grid:
            print('Нужен хотя бы один --grid key=v1,v2')
            return 2

        tickers = _universe_tickers(args.top)
        if not tickers:
            print('Пустой универсум — сначала обновите его')
            return 1

        keys = list(grid)
        combos = list(itertools.product(*(grid[k] for k in keys)))
        results = []
        print(f"Перебор {len(combos)} вариантов × {len(tickers)} инструментов "
              f"({args.interval}м, комиссия {args.fee}%)…", flush=True)
        for combo in combos:
            candidate = dict(params)
            candidate.update(dict(zip(keys, combo)))
            res = scan(tickers, from_ns, to_ns, args.strategy, candidate,
                       interval=args.interval, notional=args.notional,
                       fee_pct=args.fee, split=args.split)
            results.append((dict(zip(keys, combo)), res))

        results.sort(key=lambda pair: pair[1][args.sort], reverse=True)
        header = ''.join(f'{k:>16}' for k in keys)
        print(f"{header}{'сдел':>6}{'gross':>9}{'комис':>9}{'net':>9}{'in':>9}{'out':>9}{'плюс':>6}")
        for values, res in results:
            row = ''.join(f'{values[k]:>16}' for k in keys)
            print(f"{row}{res['total_trades']:>6}{res['gross']:>9.0f}{res['fees']:>9.0f}"
                  f"{res['net']:>9.0f}{res['in_net']:>9.0f}{res['out_net']:>9.0f}"
                  f"{res['win_tickers']:>6}")
        return 0

    if args.command == 'run':
        result = run(args.ticker, from_ns, to_ns, args.strategy, params, interval=args.interval)
        m = result['metrics']
        print(f"{result['label']} · {args.ticker.upper()} · {result['interval']}м · "
              f"баров {result['bars']}")
        print('Параметры: ' + ', '.join(f'{k}={v:g}' for k, v in result['params'].items()))
        print(f"Сделок {m['trades']}, gross {m['gross_pnl']:,.0f} ₽, комиссии {m['fees']:,.0f} ₽, "
              f"net {m['net_pnl']:,.0f} ₽, винрейт {m['win_rate']}%, PF {m['profit_factor']}, "
              f"просадка {m['max_drawdown']:,.0f} ₽")
        for t in result['trades']:
            print(f"  {t['side']:5} {t['entry_price']:>9} → {t['exit_price']:>9} "
                  f"({t['bars']:>2} бар., {t['reason']:6}) pnl {t['pnl']:>8.0f} ₽")
        return 0

    if args.ticker:
        tickers = [t.strip().upper() for t in args.ticker.split(',') if t.strip()]
    elif args.tickers_file:
        with open(args.tickers_file, encoding='utf-8') as fh:
            tickers = [line.strip().upper() for line in fh if line.strip()]
    else:
        tickers = _universe_tickers(args.top)

    if not tickers:
        print('Нет инструментов: сначала запустите сборщик и обновите универсум')
        return 1

    result = scan(tickers, from_ns, to_ns, args.strategy, params, interval=args.interval,
                  notional=args.notional, fee_pct=args.fee, split=args.split)
    _print_scan(result)
    return 0


if __name__ == '__main__':
    raise SystemExit(_cli())
