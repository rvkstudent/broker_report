"""Сигналы стратегии по торговым дням — детерминированно и с журналом.

Требования к сигналам:

* сигнал за конкретный день не зависит от диапазона и таймфрейма графика;
* сигналы закрытого дня **замораживаются** в БД и больше не меняются — это
  позволяет оценивать их историческую ценность (адаптация параметров меняет
  поведение только вперёд, а не переписывает прошлое);
* текущий день считается по индивидуальным параметрам инструмента
  (``app.adaptive``) и обновляется не чаще раза в ``TODAY_TTL`` секунд.

Записи о закрытых сделках попадают в ``adaptive.strategy_trade_log``; это
служит основанием для донастройки параметров после каждой закрытой сделки.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta

from app import adaptive, signal_journal, strategy_levels
from app.screener import MSK, get_connection

MAX_DAYS = 10          # сколько последних торговых дней считаем по запросу
TODAY_TTL = 60.0       # как часто обновлять сигналы текущего дня

_lock = threading.Lock()
_mem: dict = {}        # (ticker, day, params_key, explicit) -> (data, computed_at)
_schema_ready = False


def _conn():
    global _schema_ready
    conn = get_connection()
    if not _schema_ready:
        conn.execute("""CREATE TABLE IF NOT EXISTS strategy_day_signals (
            ticker TEXT NOT NULL,
            day TEXT NOT NULL,
            params TEXT NOT NULL,
            signals TEXT NOT NULL,
            zones TEXT NOT NULL,
            trades TEXT NOT NULL DEFAULT '[]',
            bars INTEGER NOT NULL DEFAULT 0,
            metrics TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
            PRIMARY KEY (ticker, day))""")
        cols = {row[1] for row in conn.execute(
            'PRAGMA table_info(strategy_day_signals)')}
        migrated = False
        for name, ddl in (('trades', "TEXT NOT NULL DEFAULT '[]'"),
                          ('bars', "INTEGER NOT NULL DEFAULT 0"),
                          ('metrics', "TEXT NOT NULL DEFAULT '{}'")):
            if name not in cols:
                conn.execute(f'ALTER TABLE strategy_day_signals ADD COLUMN {name} {ddl}')
                migrated = True
        if migrated:
            # Старые строки без сделок пересоберём с полным журналом.
            conn.execute('DELETE FROM strategy_day_signals')
        conn.commit()
        _schema_ready = True
    return conn


def _day_list(from_ns: int, to_ns: int) -> list:
    first = datetime.fromtimestamp(from_ns / 1_000_000_000, MSK).date()
    last = datetime.fromtimestamp(to_ns / 1_000_000_000, MSK).date()
    days = []
    day = first
    while day <= last:
        days.append(day)
        day += timedelta(days=1)
    return days[-MAX_DAYS:]


def _day_bounds(day) -> tuple[int, int]:
    start = datetime(day.year, day.month, day.day, tzinfo=MSK)
    end = start + timedelta(days=1)
    return int(start.timestamp() * 1_000_000_000), int(end.timestamp() * 1_000_000_000)


def _params_key(params) -> str:
    return json.dumps(strategy_levels._resolve(params), sort_keys=True, default=str)


def _load_frozen(ticker: str, day: str):
    try:
        conn = _conn()
        row = conn.execute(
            'SELECT signals, zones, trades, bars, metrics FROM strategy_day_signals '
            'WHERE ticker=? AND day=?', (ticker.upper(), day)).fetchone()
        conn.close()
        if not row:
            return None
        return {'signals': json.loads(row['signals']),
                'zones': json.loads(row['zones']),
                'trades': json.loads(row['trades'] or '[]'),
                'bars': int(row['bars'] or 0),
                'metrics': json.loads(row['metrics'] or '{}')}
    except Exception:
        return None


def _store_frozen(ticker: str, day: str, params: dict, data: dict) -> None:
    try:
        conn = _conn()
        conn.execute(
            """INSERT OR IGNORE INTO strategy_day_signals
               (ticker, day, params, signals, zones, trades, bars, metrics)
               VALUES (?,?,?,?,?,?,?,?)""",
            (ticker.upper(), day, json.dumps(params, sort_keys=True),
             json.dumps(data.get('signals') or [], default=str),
             json.dumps(data.get('zones') or [], default=str),
             json.dumps(data.get('trades') or [], default=str),
             int(data.get('bars') or 0),
             json.dumps(data.get('metrics') or {}, default=str)))
        conn.commit()
        conn.close()
    except Exception:
        pass


def _analyze_day(ticker: str, day, from_ns: int, to_ns: int, raw_params):
    explicit = raw_params is not None
    params = raw_params if explicit else adaptive.params_for(ticker)
    today = datetime.now(MSK).date()
    key = (ticker.upper(), day.isoformat(), _params_key(params), explicit)

    with _lock:
        hit = _mem.get(key)
        if hit is not None:
            data, computed_at = hit
            if day < today or (time.time() - computed_at) < TODAY_TTL:
                return data

    if day < today and not explicit:
        frozen = _load_frozen(ticker, day.isoformat())
        if frozen is not None:
            # Замороженный день мог быть посчитан до появления журнала —
            # дозаписываем его, чтобы история была полной (idempotent).
            signal_journal.record(ticker, frozen.get('signals') or [])
            with _lock:
                _mem[key] = (frozen, time.time())
            return frozen

    try:
        data = strategy_levels.analyze_range(ticker, from_ns, to_ns, params,
                                             interval=1)
    except Exception:
        data = None

    if data:
        # Ручные параметры — это «что если», такие сигналы в историю не пишем.
        if not explicit:
            signal_journal.record(ticker, data.get('signals') or [])
        adaptive.record_trades(ticker, data.get('trades') or [])
        if day < today and not explicit:
            _store_frozen(ticker, day.isoformat(), params, data)

    with _lock:
        _mem[key] = (data, time.time())
    return data


def day_results(ticker: str, from_ns: int, to_ns: int, raw_params=None) -> list:
    """Результаты по каждому торговому дню диапазона (сигналы, сделки, зоны)."""
    now_ns = int(time.time() * 1_000_000_000)
    to_ns = min(to_ns, now_ns)
    results = []
    for day in _day_list(from_ns, to_ns):
        day_from, day_to = _day_bounds(day)
        day_to = min(day_to, now_ns)
        if day_from >= day_to:
            continue
        data = _analyze_day(ticker, day, day_from, day_to, raw_params)
        if data:
            results.append(data)
    return results


def signals_for_range(ticker: str, from_ns: int, to_ns: int, raw_params=None):
    """Сигналы за все торговые дни диапазона и зоны последнего дня с данными."""
    signals = []
    zones = []
    for data in day_results(ticker, from_ns, to_ns, raw_params):
        signals.extend(data['signals'])
        zones = data['zones']
    signals.sort(key=lambda item: item['time'])
    return signals, zones
