"""Журнал сигналов: запись в момент появления, статус больше не меняется.

Сигнал, который стратегия увидела на закрытии бара, должен остаться в истории
ровно таким, каким он был в тот момент: дата и время появления, уровень, цена,
цена входа и статус (принят / отсеян). Повторный расчёт дня (новый проход
кэша, донастройка параметров, открытие графика) не имеет права переписывать
прошлое — иначе «история» показывает не то, что реально было.

Поэтому журнал append-only: первичный ключ ``(ticker, signal_ts)`` и
``INSERT OR IGNORE``. Первая запись побеждает, последующие расчёты ту же
строку не трогают. ``first_seen_at`` — момент, когда сигнал реально попал в
базу; ``signal_ts`` — время бара (МСК), на котором сигнал возник.
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone

from app.screener import get_connection

_lock = threading.Lock()
_schema_ready = False


def _ensure_schema(conn) -> None:
    global _schema_ready
    if _schema_ready:
        return
    conn.execute("""CREATE TABLE IF NOT EXISTS strategy_signal_log (
        ticker TEXT NOT NULL,
        signal_ts INTEGER NOT NULL,
        day TEXT NOT NULL,
        date_label TEXT NOT NULL DEFAULT '',
        time_label TEXT NOT NULL DEFAULT '',
        kind TEXT NOT NULL DEFAULT '',
        kind_label TEXT NOT NULL DEFAULT '',
        zone_price REAL, zone_low REAL, zone_high REAL,
        price REAL, delta REAL, volume_ratio REAL,
        taken INTEGER NOT NULL DEFAULT 1,
        rr REAL,
        first_seen_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
        PRIMARY KEY (ticker, signal_ts))""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_signal_log_day "
                 "ON strategy_signal_log(day, signal_ts)")
    conn.commit()
    _schema_ready = True


def _conn():
    conn = get_connection()
    _ensure_schema(conn)
    return conn


def day_of(signal_ts: int) -> str:
    """Календарный день МСК для ``signal_ts`` (эпоха + 3 часа, как в UI)."""
    return datetime.fromtimestamp(int(signal_ts), timezone.utc).strftime('%Y-%m-%d')


def _num(value):
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def record(ticker: str, signals: list) -> int:
    """Записать новые сигналы. Уже записанные строки не меняются."""
    if not signals:
        return 0
    ticker = (ticker or '').upper()
    inserted = 0
    try:
        with _lock:
            conn = _conn()
            try:
                for item in signals:
                    ts = int(item.get('time') or 0)
                    if not ts:
                        continue
                    cur = conn.execute(
                        """INSERT OR IGNORE INTO strategy_signal_log
                           (ticker, signal_ts, day, date_label, time_label, kind,
                            kind_label, zone_price, zone_low, zone_high, price,
                            delta, volume_ratio, taken, rr)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (ticker, ts, day_of(ts), item.get('date') or '',
                         item.get('time_label') or '', item.get('kind') or '',
                         item.get('kind_label') or '',
                         _num(item.get('zone_price')), _num(item.get('zone_low')),
                         _num(item.get('zone_high')), _num(item.get('price')),
                         _num(item.get('delta')), _num(item.get('volume_ratio')),
                         1 if item.get('taken', True) else 0,
                         _num(item.get('rr'))))
                    inserted += cur.rowcount
                conn.commit()
            finally:
                conn.close()
    except Exception:
        return 0
    return inserted


def _row_to_dict(row) -> dict:
    item = dict(row)
    item['taken'] = bool(item.get('taken'))
    item['date'] = item.get('date_label') or ''
    return item


def rows(ticker: str | None = None, day: str | None = None,
         from_ts: int | None = None, to_ts: int | None = None,
         limit: int = 500) -> list[dict]:
    """Сигналы по времени появления (от ранних к поздним)."""
    sql = ['SELECT ticker, signal_ts, day, date_label, time_label, kind, kind_label,'
           ' zone_price, zone_low, zone_high, price, delta, volume_ratio, taken, rr,'
           ' first_seen_at FROM strategy_signal_log']
    where: list[str] = []
    params: list = []
    if ticker:
        where.append('ticker = ?')
        params.append(ticker.upper())
    if day:
        where.append('day = ?')
        params.append(day)
    if from_ts is not None:
        where.append('signal_ts >= ?')
        params.append(int(from_ts))
    if to_ts is not None:
        where.append('signal_ts <= ?')
        params.append(int(to_ts))
    if where:
        sql.append('WHERE ' + ' AND '.join(where))
    sql.append('ORDER BY signal_ts')
    sql.append('LIMIT ?')
    params.append(max(1, int(limit)))
    conn = _conn()
    try:
        return [_row_to_dict(r) for r in conn.execute(' '.join(sql), params)]
    finally:
        conn.close()


def counts(day: str | None = None, from_ts: int | None = None,
           to_ts: int | None = None) -> dict[str, dict]:
    """Сводка по тикерам: сколько сигналов, принято, отсеяно, типы, последний."""
    sql = ["SELECT ticker, COUNT(*) AS count,"
           " SUM(CASE WHEN taken = 1 THEN 1 ELSE 0 END) AS taken,"
           " MAX(signal_ts) AS last,"
           " group_concat(DISTINCT kind_label) AS kinds"
           " FROM strategy_signal_log"]
    where: list[str] = []
    params: list = []
    if day:
        where.append('day = ?')
        params.append(day)
    if from_ts is not None:
        where.append('signal_ts >= ?')
        params.append(int(from_ts))
    if to_ts is not None:
        where.append('signal_ts <= ?')
        params.append(int(to_ts))
    if where:
        sql.append('WHERE ' + ' AND '.join(where))
    sql.append('GROUP BY ticker')
    conn = _conn()
    try:
        result = {}
        for row in conn.execute(' '.join(sql), params):
            total = int(row['count'] or 0)
            taken = int(row['taken'] or 0)
            result[row['ticker']] = {
                'count': total,
                'taken': taken,
                'skipped': total - taken,
                'last': row['last'],
                'kinds': sorted(filter(None, (row['kinds'] or '').split(','))),
            }
        return result
    finally:
        conn.close()
