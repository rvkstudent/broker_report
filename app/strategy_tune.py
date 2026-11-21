"""Подбор параметров стратегии под конкретный инструмент.

Идея: одинаковые пороги не работают на разных бумагах — у SIBN и SBER разная
волатильность, объём и поведение у уровней. Поэтому параметры подбираются на
истории инструмента (сетка по ключевым порогам: дельта, объём, минимальный
R:R), и торгуются сигналы только той настройкой, которая на этой истории дала
положительное ожидание при достаточном числе сделок.

Результат кэшируется в ``strategy_tuned`` (на инструмент), пересчитывается по
кнопке. Если ни одна конфигурация не прибыльна — так и записывается: сигналы
по инструменту не торгуются.
"""

from __future__ import annotations

import itertools
import json
from datetime import datetime

from app.db import get_connection

# Сетка намеренно небольшая: полный перебор по 12 параметрам даёт тысячи
# комбинаций и подгонку под шум. Меняем то, что реально влияет на вход.
GRID = {
    'confirm_delta': (0.15, 0.3, 0.45),
    'volume_factor': (1.2, 1.8),
    'min_rr': (1.5, 2.5),
}
MIN_TRADES = 5          # меньше — статистики нет
MAX_COMBOS = 12         # 3×2×2


def _combos() -> list[dict]:
    keys = list(GRID)
    out = []
    for values in itertools.product(*(GRID[k] for k in keys)):
        out.append(dict(zip(keys, values)))
    return out[:MAX_COMBOS]


def load(ticker: str) -> dict | None:
    try:
        conn = get_connection()
        row = conn.execute('SELECT params, metrics, updated_at FROM strategy_tuned '
                           'WHERE ticker = ?', ((ticker or '').upper(),)).fetchone()
        conn.close()
    except Exception:  # noqa: BLE001
        return None
    if not row:
        return None
    try:
        return {'params': json.loads(row['params'] or '{}'),
                'metrics': json.loads(row['metrics'] or '{}'),
                'updated_at': row['updated_at'] or ''}
    except json.JSONDecodeError:
        return None


def _save(ticker: str, params: dict, metrics: dict) -> None:
    try:
        conn = get_connection()
        conn.execute('INSERT OR REPLACE INTO strategy_tuned (ticker, params, metrics, updated_at) '
                     'VALUES (?, ?, ?, ?)',
                     ((ticker or '').upper(), json.dumps(params, ensure_ascii=False),
                      json.dumps(metrics, ensure_ascii=False),
                      datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
        conn.commit()
        conn.close()
    except Exception:  # noqa: BLE001
        pass


def tune(ticker: str, from_ns: int, to_ns: int, *, interval: int = 1) -> dict:
    """Перебрать сетку на истории и сохранить лучшую конфигурацию.

    Лучшая = положительное ожидание на сделку при >= MIN_TRADES сделках;
    среди них — максимальное ожидание. Если таких нет — profitable=False.
    """
    from app import strategy_levels

    best: dict | None = None
    results = []
    for params in _combos():
        try:
            res = strategy_levels.analyze_range(ticker, from_ns, to_ns,
                                                raw_params=params, interval=interval)
        except Exception:  # noqa: BLE001
            continue
        m = res.get('metrics') or {}
        trades = int(m.get('trades') or 0)
        expectancy = float(m.get('expectancy') or 0)
        results.append({'params': params, 'trades': trades,
                        'expectancy': round(expectancy, 2),
                        'win_rate': m.get('win_rate'), 'net_pnl': m.get('net_pnl')})
        if trades >= MIN_TRADES and expectancy > 0:
            if best is None or expectancy > best['expectancy']:
                best = {'params': params, 'trades': trades,
                        'expectancy': round(expectancy, 2),
                        'win_rate': m.get('win_rate'), 'net_pnl': m.get('net_pnl')}

    payload = {'profitable': best is not None,
               'best': best,
               'tried': len(results),
               'results': sorted(results, key=lambda r: -(r['expectancy'] or 0))[:5],
               'min_trades': MIN_TRADES}
    if best:
        _save(ticker, best['params'], {'trades': best['trades'],
                                       'expectancy': best['expectancy'],
                                       'win_rate': best['win_rate'],
                                       'net_pnl': best['net_pnl']})
    else:
        # Явно фиксируем, что прибыльной настройки нет: сигналы не торгуем.
        _save(ticker, {'profitable': False}, {'trades': 0, 'expectancy': 0})
    return payload
