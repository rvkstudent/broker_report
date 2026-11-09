"""Скринер акций: сбор обезличенных сделок из T-Invest API и объёмная аналитика.

Логика в двух частях.

1. **Сборщик** (фоновый поток). Раз в ``SCREENER_POLL_SECONDS`` опрашивает
   ``GetLastTrades`` по каждому инструменту универсума и складывает сделки в
   ``market_trade``. API отдаёт только последний час, поэтому опрашивать нужно
   постоянно; перекрытие окон не страшно — уникальный индекс отсекает дубли.

2. **Аналитика**. По накопленной ленте считаются объёмные метрики: дельта
   (перевес покупок над продажами), крупные принты, VWAP, аномалия объёма
   относительно среднего дневного оборота, тренд дельты. На их основе —
   оценка «кто доминирует» и сигнал.

Направление сделки берётся из ``Trade.direction``. Если биржа его не отдала
(``TRADE_DIRECTION_UNSPECIFIED``), восстанавливаем правилом тика: сделка по
цене выше предыдущей — покупка, ниже — продажа, по той же цене — как у
предыдущей.
"""

from __future__ import annotations

import logging
import math
import os
import sqlite3
import threading
import time
import traceback
import dataclasses
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.db import DB_PATH
from app.tinkoff_api import (TinkoffAPIError, TinkoffClient, trading_days_ago)

logger = logging.getLogger(__name__)

# Москва — UTC+3 круглый год (переходов на летнее время нет с 2014 года),
# поэтому фиксированное смещение надёжнее zoneinfo: в slim-образе может не
# оказаться базы часовых поясов.
MSK = timezone(timedelta(hours=3))
MINUTE_NS = 60 * 1_000_000_000

SIGNAL_LABELS = {
    'strong_buy': 'Активная покупка',
    'strong_sell': 'Активная продажа',
    'large_buy': 'Крупные покупки',
    'large_sell': 'Крупные продажи',
    'accumulation': 'Накопление',
    'distribution': 'Распределение',
    'volume_spike': 'Аномальный объём',
    'neutral': 'Без сигнала',
}


# ── хранилище ────────────────────────────────────────────────
#
# Тики и минутки живут в ОТДЕЛЬНОМ файле БД (screener.db), а не в broker.db.
# Месяц сырых сделок топ-100 — это ~22 ГБ; если бы они лежали в основной базе,
# то раздували бы бэкап, rsync на сервер и репликацию в Firebase.

_SCREENER_SCHEMA = (
    # Лента обезличенных сделок. UNIQUE(instrument_uid, ts_ns, price, lots) —
    # ключ дедупликации: API не даёт идентификатора сделки, а окна опроса
    # перекрываются. Наносекундная метка делает совпадение ключа у разных
    # сделок невозможным.
    """CREATE TABLE IF NOT EXISTS market_trade (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        instrument_uid  TEXT NOT NULL,
        ticker          TEXT NOT NULL,
        class_code      TEXT DEFAULT '',
        ts_ns           INTEGER NOT NULL,
        trade_date      TEXT NOT NULL,
        trade_time      TEXT NOT NULL,
        price           REAL NOT NULL,
        lots            INTEGER NOT NULL,
        lot_size        INTEGER NOT NULL DEFAULT 1,
        shares          INTEGER NOT NULL,
        value           REAL NOT NULL,
        side            TEXT NOT NULL CHECK(side IN ('buy','sell')),
        direction       TEXT DEFAULT '',
        trade_source    TEXT DEFAULT '',
        created_at      TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
        UNIQUE(instrument_uid, ts_ns, price, lots)
    )""",
    """CREATE INDEX IF NOT EXISTS idx_market_trade_ts ON market_trade(ts_ns)""",
    """CREATE INDEX IF NOT EXISTS idx_market_trade_uid_ts ON market_trade(instrument_uid, ts_ns)""",
    """CREATE INDEX IF NOT EXISTS idx_market_trade_date ON market_trade(trade_date)""",
    # Минутки — то, что остаётся после чистки сырого тика. Хватает для
    # бэктестов и графиков, весит в 30–40 раз меньше.
    """CREATE TABLE IF NOT EXISTS market_bar_1m (
        instrument_uid TEXT NOT NULL,
        ticker         TEXT NOT NULL,
        minute_ns      INTEGER NOT NULL,
        trade_date     TEXT NOT NULL,
        trade_time     TEXT NOT NULL,
        open           REAL,
        high           REAL,
        low            REAL,
        close          REAL,
        vwap           REAL,
        volume         INTEGER,
        turnover       REAL,
        buy_volume     INTEGER,
        sell_volume    INTEGER,
        buy_turnover   REAL,
        sell_turnover  REAL,
        trades              INTEGER,
        large_turnover      REAL,
        large_count         INTEGER,
        large_buy_turnover  REAL,
        large_sell_turnover REAL,
        PRIMARY KEY (instrument_uid, minute_ns)
    )""",
    """CREATE INDEX IF NOT EXISTS idx_market_bar_minute ON market_bar_1m(minute_ns)""",
    """CREATE INDEX IF NOT EXISTS idx_market_bar_date ON market_bar_1m(trade_date)""",
    # Универсум: какие инструменты опрашивать и насколько они ликвидны.
    """CREATE TABLE IF NOT EXISTS screener_instrument (
        instrument_uid  TEXT PRIMARY KEY,
        figi            TEXT DEFAULT '',
        ticker          TEXT NOT NULL,
        class_code      TEXT DEFAULT '',
        name            TEXT DEFAULT '',
        lot             INTEGER DEFAULT 1,
        currency        TEXT DEFAULT 'RUB',
        avg_daily_value REAL DEFAULT 0,
        rank            INTEGER,
        selected        INTEGER DEFAULT 0,
        last_ts_ns      INTEGER DEFAULT 0,
        last_price      REAL,
        updated_at      TEXT
    )""",
    """CREATE INDEX IF NOT EXISTS idx_screener_instr_selected ON screener_instrument(selected, rank)""",
    # Накопленный объём по ценовым полкам: переживает чистку сырой ленты,
    # поэтому статистика уровней копится месяцами.
    """CREATE TABLE IF NOT EXISTS level_stats (
        instrument_uid TEXT NOT NULL,
        day            TEXT NOT NULL,
        bucket         INTEGER NOT NULL,
        price          REAL NOT NULL,
        buy_rub        REAL DEFAULT 0,
        sell_rub       REAL DEFAULT 0,
        large_buy      REAL DEFAULT 0,
        large_sell     REAL DEFAULT 0,
        trades         INTEGER DEFAULT 0,
        PRIMARY KEY (instrument_uid, day, bucket)
    )""",
    """CREATE INDEX IF NOT EXISTS idx_level_stats_uid ON level_stats(instrument_uid, day)""",
    """CREATE TABLE IF NOT EXISTS screener_state (
        key        TEXT PRIMARY KEY,
        value      TEXT,
        updated_at TEXT
    )""",
)

class ScreenerStorageError(RuntimeError):
    """Не хватает места на диске — сбор приостановлен."""


def free_disk_mb(path: str | None = None) -> float:
    """Свободно мегабайт рядом с БД скринера (inf, если проверить нельзя)."""
    import shutil
    target = path or os.path.dirname(screener_db_path()) or '.'
    try:
        return shutil.disk_usage(target).free / (1024 * 1024)
    except OSError:
        return float('inf')


_schema_ready: set = set()
_schema_lock = threading.Lock()


def screener_db_path() -> str:
    """Путь к БД скринера. По умолчанию — screener.db рядом с broker.db."""
    override = (os.environ.get('SCREENER_DB_PATH') or '').strip()
    if override:
        return os.path.abspath(os.path.expanduser(override))
    return os.path.join(os.path.dirname(os.path.abspath(DB_PATH)), 'screener.db')


def get_connection() -> sqlite3.Connection:
    """Соединение с БД скринера (отдельный файл от отчётов)."""
    path = screener_db_path()
    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    conn = sqlite3.connect(path, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    # Тики — поток, потеря последних секунд при сбое не критична: их доберёт
    # следующий цикл (окно опроса перекрывается).
    conn.execute('PRAGMA synchronous=NORMAL')
    conn.execute('PRAGMA temp_store=MEMORY')
    _ensure_schema(conn, path)
    return conn


_BAR_EXTRA_COLUMNS = (
    ('large_buy_turnover', 'REAL'),
    ('large_sell_turnover', 'REAL'),
)


def _migrate_bar_columns(conn: sqlite3.Connection) -> None:
    """Добавить недостающие колонки в market_bar_1m (старые БД)."""
    existing = {row['name'] for row in conn.execute('PRAGMA table_info(market_bar_1m)')}
    for name, sql_type in _BAR_EXTRA_COLUMNS:
        if name not in existing:
            conn.execute(f'ALTER TABLE market_bar_1m ADD COLUMN {name} {sql_type}')
    conn.commit()


def _ensure_schema(conn: sqlite3.Connection, path: str) -> None:
    if path in _schema_ready:
        return
    with _schema_lock:
        if path in _schema_ready:
            return
        for ddl in _SCREENER_SCHEMA:
            conn.execute(ddl)
        conn.commit()
        _schema_ready.add(path)
    _migrate_bar_columns(conn)
    _migrate_from_report_db(conn)


def _migrate_from_report_db(conn: sqlite3.Connection) -> int:
    """Перенести уже собранные тики из broker.db в отдельный файл (один раз)."""
    report_path = os.path.abspath(DB_PATH)
    if os.path.abspath(screener_db_path()) == report_path or not os.path.isfile(report_path):
        return 0
    if _get_state(conn, 'migrated_from_report_db'):
        return 0
    if conn.execute('SELECT 1 FROM market_trade LIMIT 1').fetchone():
        _set_state(conn, 'migrated_from_report_db', 'already-own-data')
        return 0
    try:
        conn.execute('ATTACH DATABASE ? AS report_db', (report_path,))
    except sqlite3.Error as exc:
        logger.warning('screener: не удалось открыть broker.db для переноса тиков: %s', exc)
        return 0
    moved = 0
    try:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM report_db.sqlite_master WHERE type='table'")}
        if 'market_trade' in tables:
            before = conn.total_changes
            conn.execute(
                '''INSERT OR IGNORE INTO market_trade
                   (instrument_uid, ticker, class_code, ts_ns, trade_date, trade_time,
                    price, lots, lot_size, shares, value, side, direction, trade_source,
                    created_at)
                   SELECT instrument_uid, ticker, class_code, ts_ns, trade_date, trade_time,
                          price, lots, lot_size, shares, value, side, direction, trade_source,
                          created_at
                   FROM report_db.market_trade''')
            moved = conn.total_changes - before
        if 'screener_instrument' in tables:
            conn.execute(
                '''INSERT OR REPLACE INTO screener_instrument
                   (instrument_uid, figi, ticker, class_code, name, lot, currency,
                    avg_daily_value, rank, selected, last_ts_ns, last_price, updated_at)
                   SELECT instrument_uid, figi, ticker, class_code, name, lot, currency,
                          avg_daily_value, rank, selected, last_ts_ns, last_price, updated_at
                   FROM report_db.screener_instrument''')
        if 'screener_state' in tables:
            conn.execute(
                '''INSERT OR REPLACE INTO screener_state (key, value, updated_at)
                   SELECT key, value, updated_at FROM report_db.screener_state''')
        conn.commit()
    except sqlite3.Error as exc:
        logger.warning('screener: перенос тиков из broker.db не удался: %s', exc)
        return 0
    finally:
        try:
            conn.execute('DETACH DATABASE report_db')
        except sqlite3.Error:
            pass
    _set_state(conn, 'migrated_from_report_db',
               datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S'))
    conn.commit()
    if moved:
        logger.info('screener: перенесено %s сделок из broker.db в %s',
                    moved, screener_db_path())
    return moved


# ── конфигурация ─────────────────────────────────────────────


def _env_str(name: str, default: str = '') -> str:
    return (os.environ.get(name) or default).strip()


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    try:
        return int(str(raw).strip()) if raw not in (None, '') else default
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    try:
        return float(str(raw).replace(',', '.')) if raw not in (None, '') else default
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == '':
        return default
    return str(raw).strip().lower() in ('1', 'true', 'yes', 'on')


@dataclass(frozen=True)
class ScreenerConfig:
    token: str
    enabled: bool
    universe: str
    class_codes: tuple[str, ...]
    max_instruments: int
    poll_seconds: int
    requests_per_minute: int
    retention_days: int
    bar_retention_days: int
    min_free_disk_mb: float
    large_trade_rub: float
    universe_ttl_hours: float
    lookback_minutes: int
    market_hours_only: bool
    min_signal_turnover: float


def get_config() -> ScreenerConfig:
    """Настройки из окружения. Токен читается только из переменной среды."""
    token = _env_str('TINKOFF_TOKEN') or _env_str('TINKOFF_INVEST_TOKEN')
    classes = tuple(c.strip().upper() for c in
                    _env_str('SCREENER_CLASS_CODES', 'TQBR').split(',') if c.strip())
    max_instruments = max(1, _env_int('SCREENER_MAX_INSTRUMENTS', 100))
    poll_seconds = max(5, _env_int('SCREENER_POLL_SECONDS', 30))
    # 480 запросов/мин на поллинг + запас на обновление универсума и веб-запросы
    rpm = max(30, min(_env_int('SCREENER_RPM', 480), 580))
    return ScreenerConfig(
        token=token,
        enabled=_env_bool('SCREENER_ENABLED', bool(token)),
        universe=_env_str('SCREENER_UNIVERSE', 'auto') or 'auto',
        class_codes=classes or ('TQBR',),
        max_instruments=max_instruments,
        poll_seconds=poll_seconds,
        requests_per_minute=rpm,
        retention_days=max(1, _env_int('SCREENER_RETENTION_DAYS', 10)),
        # Минутки — то, что остаётся после чистки тика: год истории это ~1 ГБ
        # на топ-100 и полноценный набор данных для бэктестов.
        bar_retention_days=max(1, _env_int('SCREENER_BAR_RETENTION_DAYS', 365)),
        # Порог свободного места: на маленьком VPS лента легко съест диск и
        # положит всё приложение вместе с отчётами.
        min_free_disk_mb=max(50.0, _env_float('SCREENER_MIN_FREE_MB', 500.0)),
        large_trade_rub=max(0.0, _env_float('SCREENER_LARGE_TRADE_RUB', 1_000_000.0)),
        universe_ttl_hours=max(1.0, _env_float('SCREENER_UNIVERSE_TTL_HOURS', 12.0)),
        lookback_minutes=max(5, min(_env_int('SCREENER_LOOKBACK_MINUTES', 55), 60)),
        market_hours_only=_env_bool('SCREENER_MARKET_HOURS_ONLY', True),
        # На выходных торгах оборот в разы ниже: без порога «100% покупок»
        # на трёх тысячах рублей выглядели бы как сигнал.
        min_signal_turnover=max(0.0, _env_float('SCREENER_MIN_SIGNAL_TURNOVER', 1_000_000.0)),
    )


def is_enabled() -> bool:
    cfg = get_config()
    return bool(cfg.enabled and cfg.token)


# ── служебное состояние сборщика ─────────────────────────────

_state_lock = threading.Lock()
_thread: threading.Thread | None = None
_stop_event = threading.Event()
_metrics_cache: dict = {}
_metrics_cache_lock = threading.Lock()
_cache_lock_file = None

_status = {
    'running': False,
    'started_at': None,
    'last_cycle_at': None,
    'last_cycle_seconds': None,
    'last_error': None,
    'cycles': 0,
    'trades_saved': 0,
    'universe_at': None,
    'instruments': 0,
}


def collector_status() -> dict:
    """Снимок состояния сборщика для UI и API."""
    with _state_lock:
        status = dict(_status)
    cfg = get_config()
    status.update({
        'enabled': bool(cfg.enabled),
        'has_token': bool(cfg.token),
        'poll_seconds': cfg.poll_seconds,
        'universe': cfg.universe,
        'max_instruments': cfg.max_instruments,
        'market_open': _market_is_open(datetime.now(timezone.utc)),
        'universe_refreshing': _universe_refresh_lock.locked(),
        'free_disk_mb': round(free_disk_mb(), 0),
        'db_path': screener_db_path(),
    })
    return status


def _market_is_open(now_utc: datetime) -> bool:
    """Идёт ли торговая сессия MOEX.

    Будни: основная и вечерняя сессии 09:40–23:55 МСК.
    Выходные: выходные торги 09:50–19:00 МСК (акции торгуются и в сб/вс).
    """
    local = now_utc.astimezone(MSK)
    minutes = local.hour * 60 + local.minute
    if local.weekday() >= 5:
        return 9 * 60 + 50 <= minutes <= 19 * 60
    return 9 * 60 + 40 <= minutes <= 23 * 60 + 55


# ── универсум ────────────────────────────────────────────────


def _get_state(conn, key: str) -> str | None:
    row = conn.execute('SELECT value FROM screener_state WHERE key=?', (key,)).fetchone()
    return row['value'] if row else None


def _set_state(conn, key: str, value: str) -> None:
    conn.execute(
        'INSERT INTO screener_state(key, value, updated_at) VALUES(?,?,datetime(\'now\',\'localtime\')) '
        'ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at',
        (key, value))


def _avg_daily_value(client: TinkoffClient, uid: str, lot: int) -> float:
    """Средний дневной оборот по завершённым дневным свечам (в рублях)."""
    candles = client.candles(uid, trading_days_ago(45), datetime.now(timezone.utc))
    values = []
    for c in candles:
        if not c['is_complete'] or c['volume_lots'] <= 0 or c['close'] <= 0:
            continue
        values.append(c['volume_lots'] * lot * c['close'])
    if not values:
        return 0.0
    values = values[-20:]
    return sum(values) / len(values)


def refresh_universe(client: TinkoffClient, cfg: ScreenerConfig) -> int:
    """Пересобрать список инструментов и выбрать топ-N по обороту."""
    shares = client.shares()
    by_ticker = {s['ticker'].upper(): s for s in shares}

    if cfg.universe.lower() == 'auto':
        candidates = [s for s in shares
                      if s['class_code'].upper() in cfg.class_codes
                      and s['currency'] == 'RUB'
                      and s['api_trade_available']]
    else:
        wanted = [t.strip().upper() for t in cfg.universe.split(',') if t.strip()]
        candidates = []
        for ticker in wanted:
            found = by_ticker.get(ticker)
            if found is None:
                logger.warning('screener: тикер %s не найден в T-Invest API', ticker)
                continue
            candidates.append(found)

    if not candidates:
        raise TinkoffAPIError(
            'универсум пуст: API не вернул ни одной подходящей бумаги '
            f'(секции {",".join(cfg.class_codes)}, режим {cfg.universe})')

    conn = get_connection()
    try:
        known = {row['instrument_uid']: row for row in
                 conn.execute('SELECT instrument_uid, avg_daily_value, updated_at '
                              'FROM screener_instrument')}
        ttl_cutoff = (datetime.now(timezone.utc)
                      - timedelta(hours=cfg.universe_ttl_hours)).strftime('%Y-%m-%d %H:%M:%S')

        scored = []
        for inst in candidates:
            row = known.get(inst['uid'])
            avg_value = float(row['avg_daily_value'] or 0) if row else 0.0
            fresh = bool(row and row['updated_at'] and row['updated_at'] >= ttl_cutoff)
            if not fresh or avg_value <= 0:
                try:
                    avg_value = _avg_daily_value(client, inst['uid'], inst['lot'])
                except TinkoffAPIError as exc:
                    logger.warning('screener: свечи %s: %s', inst['ticker'], exc)
                    avg_value = avg_value or 0.0
            scored.append((avg_value, inst))

        scored.sort(key=lambda pair: pair[0], reverse=True)
        if cfg.universe.lower() != 'auto':
            limit = len(scored)
        else:
            limit = min(cfg.max_instruments, len(scored))

        selected_uids = {inst['uid'] for _, inst in scored[:limit]}
        now_text = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')

        for rank, (avg_value, inst) in enumerate(scored, start=1):
            conn.execute(
                '''INSERT INTO screener_instrument
                   (instrument_uid, figi, ticker, class_code, name, lot, currency,
                    avg_daily_value, rank, selected, updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(instrument_uid) DO UPDATE SET
                     ticker=excluded.ticker,
                     class_code=excluded.class_code,
                     name=excluded.name,
                     lot=excluded.lot,
                     currency=excluded.currency,
                     avg_daily_value=excluded.avg_daily_value,
                     rank=excluded.rank,
                     selected=excluded.selected,
                     updated_at=excluded.updated_at''',
                (inst['uid'], inst['figi'], inst['ticker'], inst['class_code'],
                 inst['name'], inst['lot'], inst['currency'], avg_value, rank,
                 1 if inst['uid'] in selected_uids else 0, now_text))

        # Инструменты, выпавшие из универсума, больше не опрашиваем
        if cfg.universe.lower() == 'auto':
            placeholders = ','.join('?' * len(selected_uids)) or "''"
            conn.execute(
                f'UPDATE screener_instrument SET selected=0 '
                f'WHERE instrument_uid NOT IN ({placeholders})', tuple(selected_uids))

        _set_state(conn, 'universe_updated_at', now_text)
        conn.commit()
    finally:
        conn.close()

    with _state_lock:
        _status['universe_at'] = datetime.now(timezone.utc).isoformat(timespec='seconds')
        _status['instruments'] = len(selected_uids)
    logger.info('screener: универсум обновлён, инструментов %s', len(selected_uids))
    return len(selected_uids)


def _universe_is_stale(cfg: ScreenerConfig) -> bool:
    conn = get_connection()
    try:
        value = _get_state(conn, 'universe_updated_at')
    finally:
        conn.close()
    if not value:
        return True
    try:
        updated = datetime.strptime(value, '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)
    except ValueError:
        return True
    return datetime.now(timezone.utc) - updated > timedelta(hours=cfg.universe_ttl_hours)


# ── сбор сделок ──────────────────────────────────────────────


def _load_selected_instruments() -> list[dict]:
    conn = get_connection()
    try:
        rows = conn.execute(
            'SELECT instrument_uid, ticker, class_code, lot, last_ts_ns, last_price '
            'FROM screener_instrument WHERE selected=1 ORDER BY rank').fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _resolve_sides(trades: list[dict], prev_price: float) -> None:
    """Проставить ``side`` каждой сделке, восстановив неизвестное правило тика."""
    last_side = 'buy'
    for trade in trades:
        side = trade.get('direction') or ''
        if not side:
            if prev_price and trade['price'] > prev_price:
                side = 'buy'
            elif prev_price and trade['price'] < prev_price:
                side = 'sell'
            else:
                side = last_side
        trade['side'] = side
        last_side = side
        prev_price = trade['price']


def collect_instrument(client: TinkoffClient, inst: dict, cfg: ScreenerConfig) -> int:
    """Забрать новые сделки по одному инструменту. Возвращает число новых строк."""
    now = datetime.now(timezone.utc)
    now_ns = int(now.timestamp() * 1_000_000_000)
    lookback_ns = cfg.lookback_minutes * 60 * 1_000_000_000
    # Перекрытие в 10 минут защищает от гонок по времени сервера: повторно
    # полученные сделки отсекает UNIQUE(instrument_uid, ts_ns, price, lots).
    cursor_ns = int(inst['last_ts_ns'] or 0)
    from_ns = max(cursor_ns - 10 * MINUTE_NS, now_ns - lookback_ns)
    from_dt = datetime.fromtimestamp(from_ns / 1_000_000_000, tz=timezone.utc)

    trades = client.last_trades(inst['instrument_uid'], from_dt, now)
    if not trades:
        return 0

    _resolve_sides(trades, inst['last_price'] or 0.0)

    lot = int(inst['lot'] or 1)
    rows = []
    for t in trades:
        local = datetime.fromtimestamp(t['ts_ns'] / 1_000_000_000, tz=MSK)
        shares = t['lots'] * lot
        rows.append((
            inst['instrument_uid'], inst['ticker'], inst['class_code'] or '',
            t['ts_ns'], local.strftime('%Y-%m-%d'), local.strftime('%H:%M:%S.%f')[:-3],
            t['price'], t['lots'], lot, shares, round(t['price'] * shares, 2),
            t['side'], t.get('direction') or '', t.get('trade_source') or '',
        ))

    conn = get_connection()
    try:
        before = conn.total_changes
        conn.executemany(
            '''INSERT OR IGNORE INTO market_trade
               (instrument_uid, ticker, class_code, ts_ns, trade_date, trade_time,
                price, lots, lot_size, shares, value, side, direction, trade_source)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)''', rows)
        inserted = conn.total_changes - before
        new_last_ns = max(cursor_ns, trades[-1]['ts_ns'])
        conn.execute(
            'UPDATE screener_instrument SET last_ts_ns=?, last_price=? WHERE instrument_uid=?',
            (new_last_ns, trades[-1]['price'], inst['instrument_uid']))
        conn.commit()
    finally:
        conn.close()

    with _state_lock:
        _status['trades_saved'] += inserted
    return inserted


_ROLLUP_UPSERT = """
INSERT INTO market_bar_1m
    (instrument_uid, ticker, minute_ns, trade_date, trade_time, open, high, low, close,
     vwap, volume, turnover, buy_volume, sell_volume, buy_turnover, sell_turnover,
     trades, large_turnover, large_count, large_buy_turnover, large_sell_turnover)
SELECT instrument_uid, ticker, minute_ns,
       strftime('%Y-%m-%d', minute_ns / 1000000000 + 10800, 'unixepoch'),
       strftime('%H:%M',    minute_ns / 1000000000 + 10800, 'unixepoch'),
       open, high, low, close, vwap, volume, turnover, buy_volume, sell_volume,
       buy_turnover, sell_turnover, trades, large_turnover, large_count,
       large_buy_turnover, large_sell_turnover
FROM (
    SELECT instrument_uid,
           MAX(ticker) AS ticker,
           m * :minute AS minute_ns,
           MIN(open_price)  AS open,
           MAX(close_price) AS close,
           MIN(price) AS low,
           MAX(price) AS high,
           CASE WHEN SUM(shares) > 0 THEN SUM(price * shares) / SUM(shares) END AS vwap,
           SUM(shares) AS volume,
           SUM(value)  AS turnover,
           SUM(CASE WHEN side='buy'  THEN shares ELSE 0 END) AS buy_volume,
           SUM(CASE WHEN side='sell' THEN shares ELSE 0 END) AS sell_volume,
           SUM(CASE WHEN side='buy'  THEN value ELSE 0 END)  AS buy_turnover,
           SUM(CASE WHEN side='sell' THEN value ELSE 0 END)  AS sell_turnover,
           COUNT(*) AS trades,
           SUM(CASE WHEN value >= :thr THEN value ELSE 0 END) AS large_turnover,
           SUM(CASE WHEN value >= :thr THEN 1 ELSE 0 END)     AS large_count,
           SUM(CASE WHEN side='buy'  AND value >= :thr THEN value ELSE 0 END) AS large_buy_turnover,
           SUM(CASE WHEN side='sell' AND value >= :thr THEN value ELSE 0 END) AS large_sell_turnover
    FROM (
        SELECT instrument_uid, ticker, ts_ns / :minute AS m, price, shares, value, side,
               FIRST_VALUE(price) OVER (
                   PARTITION BY instrument_uid, ts_ns / :minute ORDER BY ts_ns
                   ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING) AS open_price,
               LAST_VALUE(price) OVER (
                   PARTITION BY instrument_uid, ts_ns / :minute ORDER BY ts_ns
                   ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING) AS close_price
        FROM market_trade
        WHERE ts_ns >= :from AND ts_ns < :to
    )
    GROUP BY instrument_uid, m
)
-- SQLite иначе путает ON CONFLICT с JOIN в INSERT ... SELECT
WHERE true
ON CONFLICT(instrument_uid, minute_ns) DO UPDATE SET
    ticker=excluded.ticker, trade_date=excluded.trade_date, trade_time=excluded.trade_time,
    open=excluded.open, high=excluded.high, low=excluded.low, close=excluded.close,
    vwap=excluded.vwap, volume=excluded.volume, turnover=excluded.turnover,
    buy_volume=excluded.buy_volume, sell_volume=excluded.sell_volume,
    buy_turnover=excluded.buy_turnover, sell_turnover=excluded.sell_turnover,
    trades=excluded.trades, large_turnover=excluded.large_turnover,
    large_count=excluded.large_count,
    large_buy_turnover=excluded.large_buy_turnover,
    large_sell_turnover=excluded.large_sell_turnover
"""


def rollup_bars(until_ns: int | None = None, *, from_ns: int | None = None,
                cfg: ScreenerConfig | None = None) -> int:
    """Свернуть сделки в минутки. Возвращает число минутных строк.

    Идемпотентно: минута пересчитывается целиком, поэтому повторный запуск
    (и опоздавшие сделки) не ломают агрегат.
    """
    cfg = cfg or get_config()
    conn = get_connection()
    try:
        now_ns = int(datetime.now(timezone.utc).timestamp() * 1_000_000_000)
        # Текущую минуту не сворачиваем: сделки ещё приходят.
        end_ns = until_ns if until_ns is not None else (now_ns // MINUTE_NS) * MINUTE_NS
        if from_ns is None:
            cursor = int(_get_state(conn, 'bar_rollup_ts') or 0)
            if not cursor:
                row = conn.execute('SELECT MIN(ts_ns) AS m FROM market_trade').fetchone()
                cursor = int(row['m'] or 0)
            from_ns = max(cursor, 0)
        from_ns = (from_ns // MINUTE_NS) * MINUTE_NS
        if end_ns <= from_ns:
            return 0
        before = conn.total_changes
        conn.execute(_ROLLUP_UPSERT, {
            'minute': MINUTE_NS, 'from': from_ns, 'to': end_ns,
            'thr': cfg.large_trade_rub,
        })
        rows = conn.total_changes - before
        _set_state(conn, 'bar_rollup_ts', str(end_ns))
        conn.commit()
    finally:
        conn.close()
    return rows


def prune_old_trades(cfg: ScreenerConfig) -> int:
    """Свернуть в минутки и удалить сырые сделки старше срока хранения."""
    cutoff_ns = int((datetime.now(timezone.utc)
                     - timedelta(days=cfg.retention_days)).timestamp() * 1_000_000_000)
    # Агрегаты считаем ДО удаления — иначе история за прошедшие дни потеряется.
    try:
        rollup_bars(until_ns=cutoff_ns, cfg=cfg)
    except sqlite3.Error as exc:
        logger.warning('screener: не удалось свернуть минутки перед чисткой: %s', exc)
    conn = get_connection()
    try:
        cur = conn.execute('DELETE FROM market_trade WHERE ts_ns < ?', (cutoff_ns,))
        deleted = cur.rowcount or 0
        bar_cutoff = int((datetime.now(timezone.utc)
                          - timedelta(days=cfg.bar_retention_days)).timestamp()
                         * 1_000_000_000)
        conn.execute('DELETE FROM market_bar_1m WHERE minute_ns < ?', (bar_cutoff,))
        _set_state(conn, 'last_prune_at',
                   datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S'))
        conn.commit()
    finally:
        conn.close()
    if deleted:
        logger.info('screener: удалено старых сделок %s', deleted)
    return deleted


def _maybe_accumulate_levels(cfg: ScreenerConfig) -> None:
    """Раз в час раскладывать сделки по ценовым полкам (level_stats)."""
    conn = get_connection()
    try:
        last = _get_state(conn, 'levels_accumulated_at')
        instruments = conn.execute(
            'SELECT instrument_uid, ticker FROM screener_instrument WHERE selected=1').fetchall()
    finally:
        conn.close()

    if last:
        try:
            when = datetime.strptime(last, '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) - when < timedelta(hours=1):
                return
        except ValueError:
            pass

    from app import behavior

    now_ns = int(datetime.now(timezone.utc).timestamp() * 1_000_000_000)
    since_ns = now_ns - cfg.retention_days * 86400 * 1_000_000_000
    total = 0
    for inst in instruments:
        try:
            total += behavior.accumulate_level_stats(inst['instrument_uid'], inst['ticker'],
                                                     since_ns, now_ns,
                                                     cfg.large_trade_rub)
        except sqlite3.Error as exc:
            logger.warning('screener: level_stats %s: %s', inst['ticker'], exc)

    conn = get_connection()
    try:
        _set_state(conn, 'levels_accumulated_at',
                   datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S'))
        conn.commit()
    finally:
        conn.close()
    if total:
        logger.info('screener: накоплено ценовых полок %s', total)


def _maybe_prune(cfg: ScreenerConfig, force: bool = False) -> int:
    """Чистка не чаще раза в час — DELETE по большой таблице не бесплатный."""
    if force:
        return prune_old_trades(cfg)
    conn = get_connection()
    try:
        last = _get_state(conn, 'last_prune_at')
    finally:
        conn.close()
    if last:
        try:
            when = datetime.strptime(last, '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) - when < timedelta(hours=1):
                return 0
        except ValueError:
            pass
    return prune_old_trades(cfg)


_EXPORT_TICK_COLUMNS = ('ts_ns', 'ticker', 'price', 'lots', 'shares', 'value',
                         'side', 'direction', 'trade_source')
_EXPORT_BAR_COLUMNS = ('minute_ns', 'trade_time', 'ticker', 'open', 'high', 'low', 'close',
                       'vwap', 'volume', 'turnover', 'buy_volume', 'sell_volume',
                       'trades', 'large_turnover')


def _export_source(from_ns: int, to_ns: int, ticker: str | None, source: str) -> str:
    if source != 'auto':
        return source
    conn = get_connection()
    try:
        params = [from_ns, to_ns]
        extra = ''
        if ticker:
            extra = ' AND ticker = ?'
            params.append(ticker.upper())
        raw = conn.execute(f'SELECT 1 FROM market_trade WHERE ts_ns >= ? AND ts_ns < ?{extra} '
                           f'LIMIT 1', params).fetchone()
        if raw:
            return 'trades'
        bars = conn.execute(f'SELECT 1 FROM market_bar_1m WHERE minute_ns >= ? AND minute_ns < ?'
                            f'{extra} LIMIT 1', params).fetchone()
        if bars:
            return 'bars'
    finally:
        conn.close()
    return 'trades'


def _export_batches(source: str, from_ns: int, to_ns: int, ticker: str | None,
                    chunk: int = 200_000):
    cols = _EXPORT_TICK_COLUMNS if source == 'trades' else _EXPORT_BAR_COLUMNS
    ts_col = 'ts_ns' if source == 'trades' else 'minute_ns'
    table = 'market_trade' if source == 'trades' else 'market_bar_1m'
    sql = f'SELECT {", ".join(cols)} FROM {table} WHERE {ts_col} >= ? AND {ts_col} < ?'
    params: list = [from_ns, to_ns]
    if ticker:
        sql += ' AND ticker = ?'
        params.append(ticker.upper())
    sql += f' ORDER BY {ts_col}'

    conn = get_connection()
    try:
        cur = conn.execute(sql, params)
        while True:
            rows = cur.fetchmany(chunk)
            if not rows:
                return
            yield cols, rows
    finally:
        conn.close()


def export_trades(out_path: str, from_ns: int, to_ns: int, *, ticker: str | None = None,
                  source: str = 'auto') -> dict:
    """Выгрузить ленту (или минутки) за период для исследования.

    Формат: Parquet с zstd, если установлен pyarrow; иначе CSV в zstd (если
    есть zstandard) или в gzip. Parquet экономит в 5–10 раз сильнее.
    """
    import csv
    import gzip
    import io

    out_path = os.path.abspath(os.path.expanduser(out_path))
    os.makedirs(os.path.dirname(out_path) or '.', exist_ok=True)
    source = _export_source(from_ns, to_ns, ticker, source)

    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
        parquet = True
    except Exception:
        parquet = False

    zstd = None
    if not parquet:
        try:
            import zstandard
            zstd = zstandard
        except Exception:
            zstd = None

    if parquet:
        if not out_path.endswith('.parquet'):
            out_path += '.parquet'
    elif zstd is not None:
        if not out_path.endswith('.csv.zst'):
            out_path += '.csv.zst'
    elif not out_path.endswith('.csv.gz'):
        out_path += '.csv.gz'

    rows_written = 0
    if parquet:
        schema = None
        writer = None
        try:
            for cols, batch in _export_batches(source, from_ns, to_ns, ticker):
                table = pa.Table.from_pylist([dict(zip(cols, row)) for row in batch])
                if writer is None:
                    schema = table.schema
                    writer = pq.ParquetWriter(out_path, schema, compression='zstd')
                writer.write_table(table.cast(schema))
                rows_written += len(batch)
        finally:
            if writer is not None:
                writer.close()
        fmt = 'parquet+zstd'
    else:
        raw = open(out_path, 'wb')
        stream = zstd.ZstdCompressor(level=10).stream_writer(raw, closefd=False)             if zstd is not None else gzip.GzipFile(fileobj=raw, mode='wb', compresslevel=6)
        text = io.TextIOWrapper(stream, encoding='utf-8', newline='')
        try:
            writer_csv = csv.writer(text)
            header = None
            for cols, batch in _export_batches(source, from_ns, to_ns, ticker):
                if header is None:
                    header = cols
                    writer_csv.writerow(header)
                writer_csv.writerows(batch)
                rows_written += len(batch)
        finally:
            text.flush()
            text.close()
            raw.close()
        fmt = 'csv+zstd' if zstd is not None else 'csv+gzip'

    size = os.path.getsize(out_path)
    return {
        'path': out_path,
        'rows': rows_written,
        'bytes': size,
        'bytes_per_row': round(size / rows_written, 1) if rows_written else 0,
        'format': fmt,
        'source': source,
    }


def export_month(year: int, month: int, out_dir: str, *, ticker: str | None = None,
                 source: str = 'auto') -> dict:
    """Выгрузка за календарный месяц (границы — по МСК)."""
    start = datetime(year, month, 1, tzinfo=MSK)
    end = datetime(year + (month == 12), month % 12 + 1, 1, tzinfo=MSK)
    name = f'screener_{year}{month:02d}' + (f'_{ticker.upper()}' if ticker else '')
    return export_trades(os.path.join(out_dir, name),
                         int(start.timestamp() * 1_000_000_000),
                         int(end.timestamp() * 1_000_000_000),
                         ticker=ticker, source=source)


def run_cycle(client: TinkoffClient, cfg: ScreenerConfig) -> None:
    """Один проход по универсуму: обновить список (если надо) и дособрать сделки."""
    started = time.monotonic()
    free_mb = free_disk_mb()
    if free_mb < cfg.min_free_disk_mb:
        # Пробуем освободить место сразу, не дожидаясь часового таймера.
        _maybe_prune(cfg, force=True)
        free_mb = free_disk_mb()
        if free_mb < cfg.min_free_disk_mb and cfg.retention_days > 1:
            # Аварийный режим: сырой тик ужимаем до суток. Минутки остаются,
            # история не теряется, а сбор продолжается.
            logger.warning('screener: мало места (%.0f МБ) — сокращаю срок '
                           'хранения тика до 1 дня', free_mb)
            prune_old_trades(dataclasses.replace(cfg, retention_days=1))
            free_mb = free_disk_mb()
        if free_mb < cfg.min_free_disk_mb:
            raise ScreenerStorageError(
                f'мало места на диске: свободно {free_mb:.0f} МБ '
                f'(порог {cfg.min_free_disk_mb:.0f} МБ) — сбор приостановлен')
    if _universe_is_stale(cfg):
        refresh_universe(client, cfg)

    instruments = _load_selected_instruments()
    with _state_lock:
        _status['instruments'] = len(instruments)

    for inst in instruments:
        try:
            collect_instrument(client, inst, cfg)
        except TinkoffAPIError as exc:
            # По одному инструменту падать не должны: остальные в универсуме
            # важнее, а конкретная бумага может быть недоступна.
            logger.warning('screener: %s: %s', inst['ticker'], exc)

    elapsed = time.monotonic() - started
    with _state_lock:
        _status['last_cycle_at'] = datetime.now(timezone.utc).isoformat(timespec='seconds')
        _status['last_cycle_seconds'] = round(elapsed, 2)
        _status['cycles'] += 1

    # Минутки доворачиваем каждый цикл: это секунды работы, зато сырой тик можно
    # чистить без потери истории.
    try:
        rollup_bars(cfg=cfg)
    except sqlite3.Error as exc:
        logger.warning('screener: свёртка минуток не удалась: %s', exc)

    try:
        _maybe_accumulate_levels(cfg)
    except sqlite3.Error as exc:
        logger.warning('screener: накопление уровней не удалось: %s', exc)

    _maybe_prune(cfg)


def _acquire_process_lock():
    """Не дать двум процессам собирать сделки одновременно.

    В Docker воркер ровно один, но при перекрытии рестартов или запуске
    ``run.py`` параллельно с gunicorn второй сборщик удвоил бы расход лимита
    API и наплодил дубли в ленте.
    """
    try:
        import fcntl
    except ImportError:  # Windows — полагаемся на единственный процесс
        return None
    path = f'{DB_PATH}.screener.lock'
    try:
        handle = open(path, 'a+')
    except OSError:
        return None
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


def _collector_loop(cfg: ScreenerConfig) -> None:
    global _cache_lock_file
    _cache_lock_file = _acquire_process_lock()
    if _cache_lock_file is None:
        logger.info('screener: сборщик уже запущен в другом процессе — выходим')
        with _state_lock:
            _status['running'] = False
            _status['last_error'] = 'Сборщик уже работает в другом процессе'
        return

    client = TinkoffClient(cfg.token, requests_per_minute=cfg.requests_per_minute)
    backoff = 5
    while not _stop_event.is_set():
        if cfg.market_hours_only and not _market_is_open(datetime.now(timezone.utc)):
            _stop_event.wait(300)
            continue
        try:
            run_cycle(client, cfg)
            backoff = 5
            with _state_lock:
                _status['last_error'] = None
        except ScreenerStorageError as exc:
            logger.error('screener: %s', exc)
            with _state_lock:
                _status['last_error'] = str(exc)
            _stop_event.wait(300)
            continue
        except TinkoffAPIError as exc:
            logger.warning('screener: цикл не выполнен: %s', exc)
            with _state_lock:
                _status['last_error'] = str(exc)[:300]
            _stop_event.wait(backoff)
            backoff = min(backoff * 2, 300)
            continue
        except Exception as exc:  # noqa: BLE001 — поток не должен умирать
            logger.error('screener: непредвиденная ошибка: %s', exc)
            logger.debug(traceback.format_exc())
            with _state_lock:
                _status['last_error'] = str(exc)[:300]
            _stop_event.wait(backoff)
            backoff = min(backoff * 2, 300)
            continue
        _stop_event.wait(cfg.poll_seconds)


def start_collector() -> bool:
    """Запустить фоновый сборщик (идемпотентно)."""
    global _thread
    cfg = get_config()
    if not (cfg.enabled and cfg.token):
        logger.info('screener: выключен (нет TINKOFF_TOKEN или SCREENER_ENABLED=false)')
        return False
    with _state_lock:
        if _thread is not None and _thread.is_alive():
            return False
        _stop_event.clear()
        _status['running'] = True
        _status['started_at'] = datetime.now(timezone.utc).isoformat(timespec='seconds')
        _thread = threading.Thread(target=_collector_loop, args=(cfg,),
                                   name='screener-collector', daemon=True)
        _thread.start()
    logger.info('screener: сборщик запущен (универсум=%s, опрос %sс)',
                cfg.universe, cfg.poll_seconds)
    return True


def stop_collector() -> None:
    _stop_event.set()


# ── аналитика ────────────────────────────────────────────────


def _window_bounds(window: str) -> tuple[int, int, str]:
    """Границы окна анализа. ``session`` — с начала текущих суток по МСК."""
    now = datetime.now(timezone.utc)
    to_ns = int(now.timestamp() * 1_000_000_000)
    window = (window or 'session').strip().lower()
    if window in ('', 'session', 'day'):
        local_now = now.astimezone(MSK)
        start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        return int(start.timestamp() * 1_000_000_000), to_ns, 'С начала дня'
    try:
        minutes = max(1, min(int(window), 24 * 60))
    except ValueError:
        minutes = 60
    return to_ns - minutes * MINUTE_NS, to_ns, f'{minutes} мин'


def _instrument_meta(conn) -> dict:
    rows = conn.execute(
        'SELECT instrument_uid, ticker, name, lot, class_code, avg_daily_value, '
        'last_price, selected FROM screener_instrument').fetchall()
    return {r['instrument_uid']: dict(r) for r in rows}


def _classify(metrics: dict) -> str:
    if metrics.get('turnover', 0) < metrics.get('signal_turnover_floor', 0):
        # Оборот слишком мал: дельта на десятке сделок ничего не значит.
        return 'neutral'
    delta_ratio = metrics['delta_ratio']
    large_ratio = metrics['large_delta_ratio']
    activity = metrics['activity_ratio']

    # Сильное доминирование читается и по маленькой выборке: если сделок
    # достаточно для статистики, объёмный фон вторичен.
    liquid_enough = metrics.get('trades', 0) >= 20 or activity >= 0.8
    if abs(delta_ratio) >= 0.35 and liquid_enough:
        return 'strong_buy' if delta_ratio > 0 else 'strong_sell'
    # Один-два принта — ещё не «крупные деньги», нужна серия.
    if abs(large_ratio) >= 0.5 and metrics.get('large_count', 0) >= 3:
        return 'large_buy' if large_ratio > 0 else 'large_sell'
    if activity >= 1.5 and abs(delta_ratio) < 0.2:
        # Объём идёт, а перевеса нет: крупный игрок набирает или сбрасывает
        # позицию, не сдвигая цену заметно.
        return 'accumulation' if delta_ratio >= 0 else 'distribution'
    if activity >= 1.5:
        return 'volume_spike'
    return 'neutral'


def compute_metrics(window: str = 'session', min_turnover: float = 0.0,
                    large_rub: float | None = None) -> list[dict]:
    """Метрики объёмного анализа по всем инструментам универсума."""
    cfg = get_config()
    threshold = cfg.large_trade_rub if large_rub is None else large_rub
    from_ns, to_ns, label = _window_bounds(window)
    mid_ns = from_ns + (to_ns - from_ns) // 2

    cache_key = (from_ns // (5 * 1_000_000_000), to_ns // (5 * 1_000_000_000),
                 window, round(threshold or 0))
    with _metrics_cache_lock:
        cached = _metrics_cache.get(cache_key)
        if cached and cached[0] > time.monotonic():
            result = cached[1]
            return [m for m in result if m['turnover'] >= min_turnover]

    conn = get_connection()
    try:
        meta = _instrument_meta(conn)
        rows = conn.execute(
            '''SELECT instrument_uid,
                      COUNT(*) AS trades,
                      SUM(shares) AS total_shares,
                      SUM(value) AS turnover,
                      SUM(CASE WHEN side='buy' THEN shares ELSE 0 END) AS buy_shares,
                      SUM(CASE WHEN side='sell' THEN shares ELSE 0 END) AS sell_shares,
                      SUM(CASE WHEN side='buy' THEN value ELSE 0 END) AS buy_value,
                      SUM(CASE WHEN side='sell' THEN value ELSE 0 END) AS sell_value,
                      SUM(CASE WHEN side='buy' THEN 1 ELSE 0 END) AS buy_count,
                      SUM(CASE WHEN side='sell' THEN 1 ELSE 0 END) AS sell_count,
                      SUM(CASE WHEN side='buy' AND value >= :thr THEN value ELSE 0 END) AS large_buy,
                      SUM(CASE WHEN side='sell' AND value >= :thr THEN value ELSE 0 END) AS large_sell,
                      SUM(CASE WHEN value >= :thr THEN 1 ELSE 0 END) AS large_count,
                      SUM(CASE WHEN ts_ns >= :mid AND side='buy' THEN shares
                               WHEN ts_ns >= :mid THEN -shares ELSE 0 END) AS delta_second,
                      SUM(CASE WHEN ts_ns < :mid AND side='buy' THEN shares
                               WHEN ts_ns < :mid THEN -shares ELSE 0 END) AS delta_first,
                      SUM(price * shares) AS vwap_num,
                      SUM(CASE WHEN direction='' THEN 1 ELSE 0 END) AS dir_unknown
               FROM market_trade
               WHERE ts_ns >= :from AND ts_ns <= :to
               GROUP BY instrument_uid''',
            {'thr': threshold, 'mid': mid_ns, 'from': from_ns, 'to': to_ns}).fetchall()

        first_prices = {r['instrument_uid']: (r['price'], r['fts']) for r in conn.execute(
            '''SELECT instrument_uid, price, MIN(ts_ns) AS fts
               FROM market_trade WHERE ts_ns >= :from AND ts_ns <= :to
               GROUP BY instrument_uid''',
            {'from': from_ns, 'to': to_ns})}
        last_prices = {r['instrument_uid']: (r['price'], r['lts']) for r in conn.execute(
            '''SELECT instrument_uid, price, MAX(ts_ns) AS lts
               FROM market_trade WHERE ts_ns >= :from AND ts_ns <= :to
               GROUP BY instrument_uid''',
            {'from': from_ns, 'to': to_ns})}
    finally:
        conn.close()

    session_minutes = 14 * 60  # 09:50–23:50 МСК, грубая длительность сессии

    result = []
    for row in rows:
        m = dict(row)
        info = meta.get(m['instrument_uid'], {})
        turnover = float(m['turnover'] or 0)
        total_shares = float(m['total_shares'] or 0)
        buy_value = float(m['buy_value'] or 0)
        sell_value = float(m['sell_value'] or 0)
        delta_shares = float(m['buy_shares'] or 0) - float(m['sell_shares'] or 0)
        delta_value = buy_value - sell_value
        large_buy = float(m['large_buy'] or 0)
        large_sell = float(m['large_sell'] or 0)
        large_turnover = large_buy + large_sell

        last_pair = last_prices.get(m['instrument_uid'])
        first_pair = first_prices.get(m['instrument_uid'])
        price = (last_pair[0] if last_pair else None) or info.get('last_price') or 0.0
        first_price = (first_pair[0] if first_pair else None) or price
        vwap = (float(m['vwap_num'] or 0) / total_shares) if total_shares else price

        # Оборот сравниваем не с календарным временем с начала сессии, а с
        # фактическим периодом наблюдения: иначе после старта сборщика в
        # середине дня любая бумага выглядела бы «мёртвой» и сильные сигналы
        # не срабатывали бы вовсе.
        if first_pair and last_pair:
            covered_minutes = max(1.0, (last_pair[1] - first_pair[1]) / MINUTE_NS)
        else:
            covered_minutes = 1.0
        avg_daily = float(info.get('avg_daily_value') or 0)
        expected = (avg_daily * min(1.0, covered_minutes / session_minutes)
                    if avg_daily else 0.0)
        activity = (turnover / expected) if expected > 0 else 0.0

        delta_first = float(m['delta_first'] or 0)
        delta_second = float(m['delta_second'] or 0)
        base = total_shares or 1.0

        metrics = {
            'uid': m['instrument_uid'],
            'ticker': info.get('ticker') or '',
            'name': info.get('name') or '',
            'lot': int(info.get('lot') or 1),
            'price': round(price, 4),
            'vwap': round(vwap, 4),
            'price_change_pct': round((price - first_price) / first_price * 100, 3)
            if first_price else 0.0,
            'trades': int(m['trades'] or 0),
            'turnover': round(turnover, 2),
            'total_shares': int(total_shares),
            'buy_value': round(buy_value, 2),
            'sell_value': round(sell_value, 2),
            'buy_count': int(m['buy_count'] or 0),
            'sell_count': int(m['sell_count'] or 0),
            'avg_trade': round(turnover / (m['trades'] or 1), 2),
            'delta_value': round(delta_value, 2),
            'delta_shares': int(delta_shares),
            'delta_ratio': round(delta_shares / base, 4),
            'large_buy': round(large_buy, 2),
            'large_sell': round(large_sell, 2),
            'large_turnover': round(large_turnover, 2),
            'large_count': int(m['large_count'] or 0),
            'large_delta_ratio': round((large_buy - large_sell) / large_turnover, 4)
            if large_turnover else 0.0,
            'activity_ratio': round(activity, 3),
            'covered_minutes': round(covered_minutes, 1),
            'avg_daily_value': round(avg_daily, 2),
            'delta_trend': round((delta_second - delta_first) / base, 4),
            'direction_unknown_pct': round(
                (m['dir_unknown'] or 0) / (m['trades'] or 1) * 100, 1),
            'window': label,
            'signal_turnover_floor': cfg.min_signal_turnover,
        }
        metrics['signal'] = _classify(metrics)
        metrics['signal_label'] = SIGNAL_LABELS[metrics['signal']]

        score = (0.35 * metrics['delta_ratio']
                 + 0.25 * metrics['large_delta_ratio']
                 + 0.20 * math.tanh(activity - 1.0)
                 + 0.15 * math.tanh(metrics['price_change_pct'] / 1.0)
                 + 0.05 * math.tanh(metrics['delta_trend'] * 2))
        metrics['score'] = round(max(-1.0, min(1.0, score)) * 100, 1)
        result.append(metrics)

    result.sort(key=lambda x: x['score'], reverse=True)
    with _metrics_cache_lock:
        _metrics_cache[cache_key] = (time.monotonic() + 3.0, result)
        if len(_metrics_cache) > 64:
            for key in list(_metrics_cache)[:-32]:
                _metrics_cache.pop(key, None)
    return [m for m in result if m['turnover'] >= min_turnover]


def instrument_detail(ticker_or_uid: str, window: str = 'session',
                      limit: int = 150) -> dict | None:
    """Детализация по инструменту: метрики, лента сделок и дельта по минутам."""
    from_ns, to_ns, label = _window_bounds(window)
    conn = get_connection()
    try:
        inst = conn.execute(
            'SELECT * FROM screener_instrument WHERE ticker=? OR instrument_uid=?',
            (ticker_or_uid.upper(), ticker_or_uid)).fetchone()
        if inst is None:
            return None
        uid = inst['instrument_uid']

        series = [dict(r) for r in conn.execute(
            '''SELECT (ts_ns / 60000000000) AS bucket,
                      SUM(CASE WHEN side='buy' THEN value ELSE 0 END) AS buy_value,
                      SUM(CASE WHEN side='sell' THEN value ELSE 0 END) AS sell_value,
                      SUM(CASE WHEN side='buy' THEN shares ELSE 0 END) AS buy_shares,
                      SUM(CASE WHEN side='sell' THEN shares ELSE 0 END) AS sell_shares,
                      SUM(value) AS turnover,
                      COUNT(*) AS trades,
                      price,
                      MAX(ts_ns) AS last_ns
               FROM market_trade
               WHERE instrument_uid=? AND ts_ns >= ? AND ts_ns <= ?
               GROUP BY bucket''', (uid, from_ns, to_ns))]
        for point in series:
            point['time'] = datetime.fromtimestamp(
                point['bucket'] * 60, tz=MSK).strftime('%H:%M')
            point['delta_shares'] = (point['buy_shares'] or 0) - (point['sell_shares'] or 0)

        tape = [dict(r) for r in conn.execute(
            '''SELECT trade_time, price, lots, shares, value, side
               FROM market_trade
               WHERE instrument_uid=? AND ts_ns >= ? AND ts_ns <= ?
               ORDER BY ts_ns DESC LIMIT ?''', (uid, from_ns, to_ns, int(limit)))]
    finally:
        conn.close()

    metrics = next((m for m in compute_metrics(window) if m['uid'] == uid), None)
    if metrics is None:
        metrics = {'uid': uid, 'ticker': inst['ticker'], 'signal': 'neutral',
                   'signal_label': SIGNAL_LABELS['neutral'], 'score': 0,
                   'window': label, 'turnover': 0}

    return {
        'instrument': {
            'uid': uid,
            'ticker': inst['ticker'],
            'name': inst['name'] or '',
            'class_code': inst['class_code'] or '',
            'lot': inst['lot'] or 1,
            'avg_daily_value': inst['avg_daily_value'] or 0,
        },
        'window': label,
        'metrics': metrics,
        'series': series[-240:],
        'tape': tape,
    }


def universe_snapshot() -> list[dict]:
    conn = get_connection()
    try:
        return [dict(r) for r in conn.execute(
            'SELECT instrument_uid, ticker, name, class_code, lot, avg_daily_value, '
            'rank, selected FROM screener_instrument ORDER BY rank')]
    finally:
        conn.close()


_universe_refresh_lock = threading.Lock()


def universe_refresh_in_progress() -> bool:
    return _universe_refresh_lock.locked()


def request_universe_refresh() -> bool:
    """Пересобрать универсум в фоне (перебор ~250 бумаг занимает до минуты)."""
    cfg = get_config()
    if not (cfg.enabled and cfg.token):
        return False
    if not _universe_refresh_lock.acquire(blocking=False):
        return False

    def worker() -> None:
        try:
            client = TinkoffClient(cfg.token, requests_per_minute=cfg.requests_per_minute)
            refresh_universe(client, cfg)
            with _state_lock:
                _status['last_error'] = None
        except Exception as exc:  # noqa: BLE001 — фоновая задача, гасим ошибку
            logger.warning('screener: обновление универсума: %s', exc)
            with _state_lock:
                _status['last_error'] = str(exc)[:300]
        finally:
            _universe_refresh_lock.release()

    threading.Thread(target=worker, daemon=True, name='screener-universe').start()
    return True


# ── CLI для проверки настройки и наполнения ──────────────────
#
#   python -m app.screener check              # жив ли токен и идут ли сделки
#   python -m app.screener universe           # пересобрать топ-N
#   python -m app.screener cycle              # один проход сбора
#   python -m app.screener metrics --window 60


def _print_stats() -> int:
    """Размеры хранилища: что занимает место и сколько осталось до чистки."""
    def human(b: float) -> str:
        for unit in ('Б', 'КБ', 'МБ', 'ГБ'):
            if b < 1024 or unit == 'ГБ':
                return f'{b:,.1f} {unit}'
            b /= 1024
        return f'{b:,.1f} ГБ'

    path = screener_db_path()
    conn = get_connection()
    try:
        print(f'БД скринера: {path}')
        if os.path.exists(path):
            print(f'  файл на диске: {human(os.path.getsize(path))}')
        report = os.path.abspath(DB_PATH)
        if os.path.exists(report):
            print(f'  broker.db (отчёты): {human(os.path.getsize(report))}')
        try:
            rows = conn.execute(
                "SELECT name, SUM(pgsize) b FROM dbstat "
                "WHERE name IN ('market_trade','market_bar_1m') OR name LIKE 'idx_market%' "
                "GROUP BY name ORDER BY b DESC").fetchall()
            for r in rows:
                print(f'  {r["name"]:34} {human(r["b"]):>10}')
        except sqlite3.Error:
            pass
        raw = conn.execute('SELECT COUNT(*) c FROM market_trade').fetchone()['c']
        bars = conn.execute('SELECT COUNT(*) c FROM market_bar_1m').fetchone()['c']
        first = conn.execute('SELECT MIN(trade_date) d FROM market_trade').fetchone()['d']
        last = conn.execute('SELECT MAX(trade_date) d FROM market_trade').fetchone()['d']
        print(f'  сделок: {raw:,}  |  минуток: {bars:,}  |  период: {first or "—"} … {last or "—"}')
        cfg = get_config()
        print(f'  хранение: тик {cfg.retention_days} дн., минутки {cfg.bar_retention_days} дн.')
        if raw:
            per_row = 342
            print(f'  оценка объёма при текущем темпе: ~{human(raw * per_row)} на '
                  f'{max(1, (datetime.now(timezone.utc).date() - datetime.fromisoformat(first).date()).days + 1)} дн.')
    finally:
        conn.close()
    return 0


def _cli(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog='python -m app.screener',
                                     description='Скринер: сбор и проверка сделок T-Invest')
    parser.add_argument('command', choices=['check', 'universe', 'cycle', 'metrics',
                                            'rollup', 'export', 'stats'])
    parser.add_argument('--ticker', default='', help='тикер (check/export)')
    parser.add_argument('--window', default='session', help='окно для metrics')
    parser.add_argument('--limit', type=int, default=10, help='сколько строк печатать')
    parser.add_argument('--month', default='', help='месяц выгрузки, YYYY-MM')
    parser.add_argument('--out', default='export', help='каталог выгрузки')
    parser.add_argument('--source', default='auto', choices=['auto', 'trades', 'bars'],
                        help='что выгружать: тики, минутки или что есть')
    parser.add_argument('--full', action='store_true',
                        help='rollup: пересчитать минутки по всей ленте заново')
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')
    from app.envfile import load_env_file
    load_env_file()
    from app.db import init_db
    init_db()

    cfg = get_config()

    if args.command == 'stats':
        return _print_stats()

    if args.command == 'rollup':
        # --full пересобирает минутки по всей доступной ленте (нужно после
        # добавления новых полей в market_bar_1m)
        rows = rollup_bars(cfg=cfg, from_ns=0) if args.full else rollup_bars(cfg=cfg)
        print('свёрнуто минутных строк:', rows)
        return 0

    if args.command == 'export':
        month = args.month or datetime.now(MSK).strftime('%Y-%m')
        try:
            year, mon = (int(x) for x in month.split('-'))
        except ValueError:
            print('Месяц указывается как YYYY-MM, например 2026-11')
            return 2
        info = export_month(year, mon, args.out, ticker=args.ticker or None,
                            source=args.source)
        print(f"Выгружено {info['rows']:,} строк ({info['source']}) → {info['path']}")
        print(f"Формат {info['format']}, {info['bytes']/1024/1024:.1f} МБ, "
              f"{info['bytes_per_row']} байт/строка")
        return 0

    if args.command == 'metrics':
        rows = compute_metrics(window=args.window)[:args.limit]
        print(f"{'Тикер':8} {'Оценка':>7} {'Сигнал':16} {'Оборот':>14} {'Дельта':>14}")
        for m in rows:
            print(f"{m['ticker']:8} {m['score']:>7.1f} {m['signal']:16} "
                  f"{m['turnover']:>14,.0f} {m['delta_value']:>14,.0f}")
        return 0

    if not cfg.token:
        print('Нет TINKOFF_TOKEN: добавьте его в .env или в переменные окружения')
        return 2

    client = TinkoffClient(cfg.token, requests_per_minute=cfg.requests_per_minute)

    if args.command == 'check':
        ticker = (args.ticker or 'SBER').upper()
        shares = client.shares()
        print(f'Токен принят: доступно акций {len(shares)}')
        target = next((s for s in shares if s['ticker'].upper() == ticker), None)
        if target is None:
            print(f'Тикер {ticker} не найден')
            return 1
        now = datetime.now(timezone.utc)
        trades = client.last_trades(target['uid'], now - timedelta(minutes=55), now)
        print(f"{ticker}: сделок за 55 мин — {len(trades)}")
        for t in trades[-5:]:
            ts = datetime.fromtimestamp(t['ts_ns'] / 10 ** 9, tz=MSK)
            print(f"  {ts.strftime('%H:%M:%S')}  {t['price']:>10} x {t['lots']:>6} лот.  "
                  f"{t['direction'] or 'без направления'}")
        return 0

    if args.command == 'universe':
        count = refresh_universe(client, cfg)
        print(f'Универсум: {count} инструментов')
        for row in universe_snapshot()[:args.limit]:
            print(f"  {row['rank']:>3}. {row['ticker']:8} {row['avg_daily_value']:>16,.0f} ₽/день")
        return 0

    run_cycle(client, cfg)
    status = collector_status()
    print(f"Цикл выполнен: инструментов {status['instruments']}, "
          f"сделок сохранено {status['trades_saved']}, ошибка {status['last_error']}")
    return 0


if __name__ == '__main__':
    raise SystemExit(_cli())
