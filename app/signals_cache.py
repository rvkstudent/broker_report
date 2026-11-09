"""Кэш сигналов стратегии за сегодня — для таблицы скринера.

Считать сигналы на каждый запрос нельзя: на 50 бумаг это уровни (профиль по
цене) + минутки + симуляция, единицы секунд. Поэтому считаем в фоновом потоке
и отдаём последний готовый снимок: страница опрашивает API каждые 15 секунд,
но тяжёлую работу делает один поток раз в TTL секунд.

Пока идёт первый расчёт, отдаётся пустой снимок — это лучше, чем блокировать
запрос на несколько секунд.
"""

from __future__ import annotations

import threading
import time

from app import chartdata, screener, strategy_levels

TTL = 300.0

_lock = threading.Lock()
_data: dict[str, dict] = {}
_updated_at = 0.0
_refreshing = False


def _compute() -> dict[str, dict]:
    from_ns, to_ns, _ = chartdata.resolve_range('today')
    conn = screener.get_connection()
    try:
        tickers = [r['ticker'] for r in conn.execute(
            'SELECT ticker FROM screener_instrument WHERE selected = 1 ORDER BY rank')]
    finally:
        conn.close()

    result: dict[str, dict] = {}
    for ticker in tickers:
        try:
            data = strategy_levels.analyze_range(ticker, from_ns, to_ns, None)
        except Exception:
            continue
        signals = data['signals']
        result[ticker] = {
            'count': len(signals),
            'last': signals[-1]['time'] if signals else None,
            'kinds': sorted({s['kind_label'] for s in signals}),
        }
    return result


def _worker() -> None:
    global _data, _updated_at, _refreshing
    try:
        computed = _compute()
        with _lock:
            _data = computed
            _updated_at = time.time()
    except Exception:
        pass
    finally:
        with _lock:
            _refreshing = False


def refresh(force: bool = False) -> None:
    """Запустить пересчёт в фоне, если снимок устарел."""
    global _refreshing
    with _lock:
        if _refreshing:
            return
        if not force and time.time() - _updated_at < TTL:
            return
        _refreshing = True
    threading.Thread(target=_worker, daemon=True, name='signals-cache').start()


def snapshot(force: bool = False) -> tuple[dict[str, dict], float]:
    """Последний снимок (и время расчёта). Пересчёт — в фоне."""
    refresh(force=force)
    with _lock:
        return dict(_data), _updated_at
