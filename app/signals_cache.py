"""Кэш сигналов стратегии за сегодня — для таблицы скринера.

Считать сигналы на каждый запрос нельзя: на 50 бумаг это уровни (профиль по
цене) + минутки + симуляция, единицы секунд. Поэтому считаем в фоновом потоке
и отдаём последний готовый снимок: страница опрашивает API каждые 15 секунд,
но тяжёлую работу делает один поток раз в TTL секунд.

Пока идёт первый расчёт, результат публикуется по мере готовности бумаг: таблица
скринера заполняется постепенно, а не остаётся пустой до конца (расчёт по 50
бумагам может идти несколько минут).
"""

from __future__ import annotations

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from app import adaptive, chartdata, screener, strategy_levels

# Через сколько секунд после завершения прохода начинать новый.
TTL = float(os.environ.get('SIGNALS_TTL_SECONDS', '300') or 300)
# Сколько бумаг считаем параллельно. Анализ упирается в чтение SQLite,
# поэтому потоки дают ускорение почти линейно.
WORKERS = max(1, min(int(os.environ.get('SIGNALS_WORKERS', '3') or 3), 8))

_lock = threading.Lock()
_data: dict[str, dict] = {}
_updated_at = 0.0
_refreshing = False
_started = False
_start_lock = threading.Lock()


def _current_snapshot() -> dict[str, dict]:
    """Копия прошлого снимка: новый проход дополняет его, а не стирает.

    Иначе в начале каждого пересчёта таблица на секунды пустеет, и сигналы
    «исчезают», чтобы потом появиться заново.
    """
    with _lock:
        return dict(_data)


def _analyze_one(ticker: str, from_ns: int, to_ns: int):
    params = adaptive.params_for(ticker)
    try:
        data = strategy_levels.analyze_range(ticker, from_ns, to_ns, params, interval=1)
    except Exception:
        return ticker, None
    adaptive.record_trades(ticker, data.get('trades') or [])
    signals = data['signals']
    taken = sum(1 for s in signals if s.get('taken', True))
    return ticker, {
        'count': len(signals),
        'taken': taken,
        'skipped': len(signals) - taken,
        'last': signals[-1]['time'] if signals else None,
        'kinds': sorted({s['kind_label'] for s in signals}),
        # Полные строки сигналов — для отдельной таблицы: у каждого видно
        # момент появления (дата и время бара) и статус входа.
        'signals': [{
            'time': s['time'],
            'date': s.get('date'),
            'time_label': s.get('time_label'),
            'kind': s['kind'],
            'kind_label': s['kind_label'],
            'price': s['price'],
            'zone_price': s['zone_price'],
            'delta': s['delta'],
            'volume_ratio': s['volume_ratio'],
            'taken': bool(s.get('taken', True)),
            'rr': s.get('rr'),
        } for s in signals],
    }


def _compute(publish=None) -> dict[str, dict]:
    from_ns, to_ns, _ = chartdata.resolve_range('today')
    conn = screener.get_connection()
    try:
        tickers = [r['ticker'] for r in conn.execute(
            'SELECT ticker FROM screener_instrument WHERE selected = 1 ORDER BY rank')]
    finally:
        conn.close()

    result: dict[str, dict] = _current_snapshot()
    with ThreadPoolExecutor(max_workers=WORKERS,
                            thread_name_prefix='signals-calc') as pool:
        futures = [pool.submit(_analyze_one, ticker, from_ns, to_ns)
                   for ticker in tickers]
        for future in as_completed(futures):
            ticker, info = future.result()
            if info is None:
                continue
            result[ticker] = info
            # Публикуем частичный снимок: сигналы появляются в скринере по
            # мере расчёта, а не только после обхода всего универсума.
            if publish is not None:
                publish(dict(result))
    # Бумаги, выпавшие из универсума, из снимка убираем.
    allowed = set(tickers)
    return {ticker: info for ticker, info in result.items() if ticker in allowed}


def _worker() -> None:
    global _data, _updated_at, _refreshing

    def publish(partial: dict) -> None:
        global _data
        with _lock:
            _data = partial

    try:
        computed = _compute(publish=publish)
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


def signal_rows(snapshot_data: dict[str, dict], names: dict[str, str] | None = None) -> list[dict]:
    """Плоский список сигналов по всем бумагам, по времени появления.

    Из него на странице скринера строится отдельная таблица: у каждого сигнала
    своя дата и время, поэтому видно, когда он появился, а не только счётчик.
    """
    names = names or {}
    rows = []
    for ticker, info in (snapshot_data or {}).items():
        for sig in info.get('signals') or []:
            rows.append({**sig, 'ticker': ticker, 'name': names.get(ticker, '')})
    rows.sort(key=lambda item: item['time'])
    return rows


def start_background_refresh(interval: float = 30.0) -> None:
    """Постоянный фоновый пересчёт сигналов (запускается при старте приложения).

    Раньше расчёт стартовал только при открытии страницы, поэтому до первого
    захода сигналы были пустыми. Теперь проход идёт сразу после старта и
    повторяется, как только предыдущий завершился и истёк TTL: к моменту
    открытия скринера данные уже посчитаны.
    """
    global _started
    with _start_lock:
        if _started:
            return
        _started = True

    def loop() -> None:
        while True:
            try:
                refresh()
            except Exception:
                pass
            time.sleep(max(5.0, interval))

    threading.Thread(target=loop, daemon=True, name='signals-refresh').start()
