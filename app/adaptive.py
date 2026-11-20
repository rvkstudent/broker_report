"""Индивидуальная автокорректируемая стратегия на инструмент.

Идея: параметры торговли (подтверждение дельтой, объёмный фильтр, hold rate,
стоп/тейк, ATR, кулдаун, R:R) подбираются для КАЖДОГО инструмента отдельно по
его собственной истории и плавно донастраиваются после закрытия сделок.

Поведенческие факторы уже встроены в сам сигнал ``strategy_levels``:
* горизонтальный профиль объёма и полки крупных игроков (level_stats);
* касания уровня, hold rate, проколы/пробои (behavior);
* активность по ленте — дельта и объём относительно среднего;
* волатильность инструмента — ATR, через который задаётся стоп.

Что делает этот модуль:
1. Хранит индивидуальные параметры в ``strategy_params`` (screener.db).
2. Пишет закрытые сделки в ``strategy_trade_log`` — это и есть история
   качества сигналов по инструменту.
3. После каждой новой закрытой сделки ставит инструмент в очередь на
   walk-forward донастройку: in-sample на прошлых днях, out-of-sample на
   последних. Параметры меняются только при устойчивом улучшении и плавно
   (гистерезис + смешивание), чтобы не «дёргаться» на шуме.
"""

from __future__ import annotations

import json
import math
import os
import queue
import threading
import time
from datetime import datetime, timedelta

from app import strategy_levels
from app.screener import MSK, get_connection

DEFAULTS = {p['key']: p['default'] for p in strategy_levels.PARAMS}

# Безопасные границы: алгоритм не выходит за них даже при «улучшении».
TUNABLE = {
    'confirm_delta':   {'default': 0.15, 'low': 0.10, 'high': 0.30, 'step': 0.05},
    'volume_factor':   {'default': 1.2,  'low': 1.0,  'high': 1.8,  'step': 0.1},
    'min_hold_rate':   {'default': 0.0,  'low': 0.0,  'high': 0.40, 'step': 0.05},
    'cooldown_min':    {'default': 30,   'low': 15,   'high': 60,   'step': 5},
    'min_rr':          {'default': 1.5,  'low': 1.2,  'high': 1.8,  'step': 0.1},
    'stop_loss_pct':   {'default': 0.25, 'low': 0.15, 'high': 0.40, 'step': 0.05},
    'take_profit_pct': {'default': 0.4,  'low': 0.30, 'high': 0.70, 'step': 0.05},
    'atr_mult':        {'default': 1.0,  'low': 0.6,  'high': 1.4,  'step': 0.2},
    'breakeven_atr':   {'default': 0.0,  'low': 0.0,  'high': 0.6,  'step': 0.2},
    'trail_atr':       {'default': 0.0,  'low': 0.0,  'high': 0.6,  'step': 0.2},
}

MIN_TRADES = int(os.environ.get('ADAPT_MIN_TRADES', '5') or 5)  # порог выборки
ADAPT_DAYS = int(os.environ.get('ADAPT_DAYS', '5') or 5)        # окно истории
DEBOUNCE_SEC = float(os.environ.get('ADAPT_DEBOUNCE_SEC', '600') or 600)
IMPROVE_MARGIN = float(os.environ.get('ADAPT_IMPROVE_MARGIN', '1.15') or 1.15)

_lock = threading.Lock()
_params_cache: dict = {}      # ticker -> (params, expires_at)
_last_adapt: dict = {}        # ticker -> timestamp последней попытки
_scheduled: set = set()
_queue: queue.Queue = queue.Queue()
_worker_started = False
_worker_lock = threading.Lock()
_schema_ready = False


# ── схема и параметры ────────────────────────────────────────

def _conn():
    conn = get_connection()
    _ensure_schema(conn)
    return conn


def _ensure_schema(conn) -> None:
    global _schema_ready
    if _schema_ready:
        return
    conn.execute("""CREATE TABLE IF NOT EXISTS strategy_params (
        ticker TEXT PRIMARY KEY,
        params TEXT NOT NULL,
        version INTEGER NOT NULL DEFAULT 0,
        updated_at TEXT NOT NULL,
        in_trades INTEGER DEFAULT 0, in_net REAL DEFAULT 0,
        oos_trades INTEGER DEFAULT 0, oos_net REAL DEFAULT 0)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS strategy_trade_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ticker TEXT NOT NULL,
        entry_time INTEGER NOT NULL,
        exit_time INTEGER,
        entry_price REAL, exit_price REAL,
        kind TEXT, pnl REAL,
        created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
        UNIQUE(ticker, entry_time))""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_strategy_log_ticker "
                 "ON strategy_trade_log(ticker, exit_time)")
    conn.commit()
    _schema_ready = True


def _clamp(key: str, value) -> float:
    spec = TUNABLE[key]
    try:
        value = float(value)
    except (TypeError, ValueError):
        value = spec['default']
    value = max(spec['low'], min(spec['high'], value))
    step = spec['step']
    return round(round(value / step) * step, 6)


def params_for(ticker: str, use_cache: bool = True) -> dict:
    """Действующие параметры инструмента (базовые + индивидуальные)."""
    ticker = (ticker or '').upper()
    now = time.time()
    if use_cache:
        with _lock:
            hit = _params_cache.get(ticker)
            if hit and hit[1] > now:
                return dict(hit[0])

    params = dict(DEFAULTS)
    try:
        conn = _conn()
        row = conn.execute('SELECT params FROM strategy_params WHERE ticker=?',
                           (ticker,)).fetchone()
        conn.close()
        if row:
            overrides = json.loads(row['params'])
            for key, value in overrides.items():
                if key in TUNABLE:
                    params[key] = _clamp(key, value)
    except Exception:
        pass

    with _lock:
        _params_cache[ticker] = (dict(params), now + 60)
    return params


def summary(ticker: str) -> dict:
    """Короткая сводка индивидуальных параметров для интерфейса."""
    params = params_for(ticker)
    return {key: params[key] for key in TUNABLE}


# ── журнал сделок и качество ─────────────────────────────────

def record_trades(ticker: str, trades: list) -> int:
    """Записать закрытые сделки; новые — повод для донастройки."""
    if not trades:
        return 0
    ticker = (ticker or '').upper()
    inserted = 0
    try:
        conn = _conn()
        for trade in trades:
            entry = int(trade.get('entry_time') or 0)
            if not entry:
                continue
            cur = conn.execute(
                """INSERT OR IGNORE INTO strategy_trade_log
                   (ticker, entry_time, exit_time, entry_price, exit_price, kind, pnl)
                   VALUES (?,?,?,?,?,?,?)""",
                (ticker, entry, int(trade.get('exit_time') or 0),
                 float(trade.get('entry_price') or 0),
                 float(trade.get('exit_price') or 0),
                 trade.get('kind') or '', float(trade.get('pnl') or 0)))
            inserted += cur.rowcount
        conn.commit()
        conn.close()
    except Exception:
        return 0
    if inserted:
        schedule_adapt(ticker)
    return inserted


def _quality_from_rows(rows: list) -> dict:
    trades = len(rows)
    if not trades:
        return {'trades': 0, 'wins': 0, 'win_rate': 0.0, 'net': 0.0,
                'profit_factor': 0.0, 'expectancy': 0.0, 'score': 0.0}
    pnls = [float(r['pnl'] or 0) for r in rows]
    wins = sum(1 for p in pnls if p > 0)
    net = sum(pnls)
    gross_win = sum(p for p in pnls if p > 0)
    gross_loss = -sum(p for p in pnls if p <= 0)
    pf = (gross_win / gross_loss) if gross_loss > 0 else (99.0 if gross_win else 0.0)
    expectancy = net / trades
    score = expectancy * (trades / (trades + 5.0))
    return {'trades': trades, 'wins': wins, 'win_rate': wins / trades,
            'net': round(net, 2), 'profit_factor': round(pf, 2),
            'expectancy': round(expectancy, 2), 'score': round(score, 2)}


def quality_map() -> dict:
    """Качество по каждому инструменту (по журналу закрытых сделок)."""
    result = {}
    try:
        conn = _conn()
        rows = conn.execute(
            'SELECT ticker, pnl FROM strategy_trade_log WHERE exit_time > 0').fetchall()
        conn.close()
    except Exception:
        return result
    grouped: dict = {}
    for row in rows:
        grouped.setdefault(row['ticker'], []).append(row)
    for ticker, items in grouped.items():
        result[ticker] = _quality_from_rows(items)
    return result


# ── walk-forward донастройка ─────────────────────────────────

def _day_bounds(day_str: str):
    day = datetime.strptime(day_str, '%Y-%m-%d').replace(tzinfo=MSK)
    return (int(day.timestamp() * 1e9),
            int((day + timedelta(days=1)).timestamp() * 1e9))


def _recent_days(ticker: str) -> list:
    try:
        conn = _conn()
        rows = conn.execute(
            'SELECT DISTINCT trade_date FROM market_bar_1m WHERE ticker = ? '
            'ORDER BY trade_date DESC LIMIT ?', (ticker, ADAPT_DAYS)).fetchall()
        conn.close()
    except Exception:
        return []
    return sorted(r['trade_date'] for r in rows)


def _simulate_days(ticker: str, days: list, params: dict) -> list:
    trades = []
    for day in days:
        from_ns, to_ns = _day_bounds(day)
        try:
            data = strategy_levels.analyze_range(ticker, from_ns, to_ns, params,
                                                 interval=1)
        except Exception:
            continue
        trades.extend(data.get('trades') or [])
    return trades


def _candidates(current: dict) -> list:
    """Безопасные направления настройки (без выхода за границы)."""
    def shifted(overrides):
        params = dict(current)
        for key, delta in overrides.items():
            spec = TUNABLE[key]
            params[key] = _clamp(key, params.get(key, spec['default']) + delta)
        return params

    return [
        ('selective', shifted({'confirm_delta': 0.05, 'volume_factor': 0.1,
                               'min_hold_rate': 0.05, 'cooldown_min': 5})),
        ('tight_risk', shifted({'stop_loss_pct': -0.05, 'atr_mult': -0.2,
                                'breakeven_atr': 0.2})),
        ('let_profit_run', shifted({'take_profit_pct': 0.05, 'trail_atr': 0.2})),
        ('looser', shifted({'confirm_delta': -0.05, 'min_hold_rate': -0.05,
                            'min_rr': -0.1, 'cooldown_min': -5})),
    ]


def _blend(current: dict, target: dict) -> dict:
    """Плавный сдвиг: половина шага к цели, по сетке допустимых значений."""
    result = dict(current)
    for key in TUNABLE:
        cur = current.get(key, TUNABLE[key]['default'])
        tgt = target.get(key, cur)
        result[key] = _clamp(key, cur + 0.5 * (tgt - cur))
    return result


def _logged_trades(ticker: str) -> int:
    try:
        conn = _conn()
        row = conn.execute(
            'SELECT COUNT(*) AS n FROM strategy_trade_log '
            'WHERE ticker=? AND exit_time > 0', (ticker.upper(),)).fetchone()
        conn.close()
        return int(row['n'] or 0) if row else 0
    except Exception:
        return 0


def adapt(ticker: str) -> dict | None:
    """Одна попытка донастройки инструмента. Возвращает изменения или None."""
    ticker = (ticker or '').upper()
    # Сначала дешёвая проверка: пока по инструменту мало закрытых сделок,
    # не гоняем дорогую оценку кандидатов.
    if _logged_trades(ticker) < MIN_TRADES:
        return None
    days = _recent_days(ticker)
    if len(days) < 2:
        return None
    split = max(1, int(round(len(days) * 0.7)))
    in_days, oos_days = days[:split], days[split:]
    if not oos_days:
        return None

    current = params_for(ticker, use_cache=False)

    def evaluate(params):
        in_metrics = _quality_from_rows(
            [{'pnl': t['pnl']} for t in _simulate_days(ticker, in_days, params)])
        oos_trades = _simulate_days(ticker, oos_days, params)
        oos_metrics = _quality_from_rows([{'pnl': t['pnl']} for t in oos_trades])
        return in_metrics, oos_metrics

    cur_in, cur_oos = evaluate(current)
    if cur_oos['trades'] < 1:
        return None
    # Слишком мало сделок — не меняем параметры: иначе ловим шум.
    if cur_in['trades'] + cur_oos['trades'] < MIN_TRADES:
        return None

    best = None
    for name, params in _candidates(current):
        in_m, oos_m = evaluate(params)
        if oos_m['trades'] < 1 or oos_m['net'] <= 0 or in_m['net'] <= 0:
            continue
        if best is None or oos_m['score'] > best[2]['score']:
            best = (name, params, oos_m, in_m)

    if best is None:
        return None
    name, target, oos_m, in_m = best
    if oos_m['score'] <= cur_oos['score'] * IMPROVE_MARGIN:
        return None

    blended = _blend(current, target)
    overrides = {key: blended[key] for key in TUNABLE}
    try:
        conn = _conn()
        conn.execute(
            """INSERT INTO strategy_params
               (ticker, params, version, updated_at, in_trades, in_net, oos_trades, oos_net)
               VALUES (?,?,1,datetime('now','localtime'),?,?,?,?)
               ON CONFLICT(ticker) DO UPDATE SET
                 params=excluded.params,
                 version=strategy_params.version + 1,
                 updated_at=excluded.updated_at,
                 in_trades=excluded.in_trades, in_net=excluded.in_net,
                 oos_trades=excluded.oos_trades, oos_net=excluded.oos_net""",
            (ticker, json.dumps(overrides, sort_keys=True),
             in_m['trades'], in_m['net'], oos_m['trades'], oos_m['net']))
        conn.commit()
        conn.close()
    except Exception:
        return None

    with _lock:
        _params_cache.pop(ticker, None)
    return {'ticker': ticker, 'profile': name, 'params': blended,
            'oos': oos_m, 'in_sample': in_m}


def _worker_loop() -> None:
    while True:
        ticker = _queue.get()
        try:
            adapt(ticker)
        except Exception:
            pass
        finally:
            with _lock:
                _last_adapt[ticker] = time.time()
                _scheduled.discard(ticker)
            _queue.task_done()


def schedule_adapt(ticker: str) -> bool:
    """Поставить инструмент в очередь на донастройку (с дебаунсом)."""
    ticker = (ticker or '').upper()
    if not ticker:
        return False
    if not start_background_adaptation():
        return False
    with _lock:
        if ticker in _scheduled:
            return False
        if time.time() - _last_adapt.get(ticker, 0.0) < DEBOUNCE_SEC:
            return False
        _scheduled.add(ticker)
    _queue.put(ticker)
    return True


def start_background_adaptation() -> bool:
    """Запустить фоновый воркер донастройки (один на процесс)."""
    global _worker_started
    with _worker_lock:
        if _worker_started:
            return True
        _worker_started = True
    threading.Thread(target=_worker_loop, daemon=True, name='adaptive-strategy').start()
    return True


def seed_universe() -> int:
    """При старте поставить в очередь инструменты с накопленной историей."""
    try:
        conn = _conn()
        rows = conn.execute(
            "SELECT ticker FROM screener_instrument WHERE selected=1 ORDER BY rank"
        ).fetchall()
        conn.close()
    except Exception:
        return 0
    count = 0
    for row in rows:
        if schedule_adapt(row['ticker']):
            count += 1
    return count
