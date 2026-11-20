"""Резервные цены для дашборда из T-Invest API.

QUIK остаётся основным источником цен. Если он не активен (в ``current_price``
нет записей моложе 10 секунд), дашборд подставляет последние цены из T-Invest
для бумаг с открытыми позициями. Токен — ``TINKOFF_TOKEN`` (тот же, что у
скринера).

Клиент, лимиты запросов и доверие TLS берутся из ``app.tinkoff_api``; здесь
только кэш и приведение цены к единицам ``current_price``: облигации — рубли
за бумагу (из % от номинала), фьючерсы — стоимость контракта.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from app.tinkoff_api import (TinkoffAPIError, TinkoffClient, _quotation_to_float,
                             trading_days_ago)

log = logging.getLogger(__name__)

REFRESH_SECONDS = 20
MAX_INSTRUMENTS = 200
DETAIL_METHODS = {
    'bond': 'BondBy', 'future': 'FutureBy', 'share': 'ShareBy',
    'etf': 'EtfBy', 'currency': 'CurrencyBy',
}
# Классы MOEX в порядке предпочтения — используются, когда класс бумаги
# неизвестен (в отчётах брокеров поле class_code часто пустое).
PREFERRED_CLASSES = ('TQBR', 'TQTF', 'TQIF', 'TQCB', 'TQOB', 'TQTD',
                     'TQTC', 'TQIR', 'SPBFUT', 'SPBOPT')

_lock = threading.Lock()
_fetch_lock = threading.Lock()

_client: TinkoffClient | None = None
_client_error = ''
_uid_cache: dict = {}      # (sec_code, class_code) -> InstrumentShort | None
_detail_cache: dict = {}   # uid -> полная карточка
_prices_cache: dict = {}   # sec_code -> row
_prices_at = 0.0
_prev_close_cache: dict = {}   # sec_code -> (дата МСК, закрытие пред. дня | None)
_prev_close_lock = threading.Lock()
_prev_close_day = ''           # день, для которого кэш уже прочитан из БД
_prev_close_refreshing = False


def token() -> str:
    """Токен T-Invest из окружения (тот же, что использует скринер)."""
    for name in ('TINKOFF_TOKEN', 'TINKOFF_INVEST_TOKEN', 'INVEST_TOKEN'):
        value = (os.environ.get(name) or '').strip()
        if value:
            return value
    return ''


def enabled() -> bool:
    return bool(token())


def last_error() -> str:
    return _client_error


def cached_count() -> int:
    with _lock:
        return len(_prices_cache)


def _get_client() -> TinkoffClient | None:
    global _client, _client_error
    if _client is not None:
        return _client
    tok = token()
    if not tok:
        _client_error = 'токен TINKOFF_TOKEN не задан'
        return None
    try:
        _client = TinkoffClient(tok)
    except Exception as exc:  # noqa: BLE001
        _client_error = str(exc)
        log.warning('T-Invest клиент для дашборда не создан: %s', exc)
        return None
    return _client


def _class_preference(ins: dict) -> int:
    """Порядок предпочтения классов: MOEX-акции, затем фонды, облигации, срочный."""
    cls = (ins.get('classCode') or '').upper()
    return PREFERRED_CLASSES.index(cls) if cls in PREFERRED_CLASSES else len(PREFERRED_CLASSES)


def _find(client: TinkoffClient, sec_code: str, class_code: str) -> dict | None:
    """Найти инструмент по коду QUIK (тикер или ISIN).

    Один тикер в T-Invest может встречаться в нескольких классах (например,
    BELU есть и на MOEX — TQBR, и в классе PTEQ). FindInstrument — это поиск,
    поэтому порядок выдачи произвольный, а LastPrice по чужой листинговой
    записи отдаёт цену её последней сделки: она может быть неликвидна и
    «стоять» месяцами, выдавая 243.40 вместо 316 руб. Поэтому при известном
    классе берём только точное совпадение, а без класса — предпочитаем
    биржевые классы MOEX вместо «первой попавшейся» записи.
    """
    key = (sec_code.upper(), class_code or '')
    if key in _uid_cache:
        return _uid_cache[key]

    found = None
    try:
        instruments = client.find_instrument(sec_code)
        wanted = sec_code.upper()
        matches = []
        for ins in instruments:
            ticker = (ins.get('ticker') or '').upper()
            isin = (ins.get('isin') or '').upper()
            if ticker == wanted or isin == wanted:
                matches.append(ins)

        if class_code:
            wanted_class = class_code.upper()
            for ins in matches:
                if (ins.get('classCode') or '').upper() == wanted_class:
                    found = ins
                    break
            if found is None and matches:
                # Класс из QUIK может не совпадать с классом T-Invest (ОФЗ в
                # QUIK помечены SIRMR_BND, облигация RU000A10C3M0 — TQBR).
                # Тогда берём MOEX-запись, как и при неизвестном классе, но
                # чужие листинги (PTEQ и т.п.) по-прежнему не подставляем.
                matches.sort(key=_class_preference)
                best = matches[0]
                if (best.get('classCode') or '').upper() in PREFERRED_CLASSES:
                    found = best
            if found is None:
                log.warning(
                    'FindInstrument %s: нет инструмента в классе %s — '
                    'цену из T-Invest не подставляю', sec_code, wanted_class)
        elif matches:
            matches.sort(key=_class_preference)
            best = matches[0]
            if (best.get('classCode') or '').upper() in PREFERRED_CLASSES:
                found = best
            else:
                log.warning(
                    'FindInstrument %s: нет записи в классах MOEX (есть только %s) — '
                    'цену из T-Invest не подставляю',
                    sec_code, (best.get('classCode') or '—'))
    except TinkoffAPIError as exc:
        log.warning('FindInstrument %s: %s', sec_code, exc)
        found = None

    _uid_cache[key] = found
    return found


def _details(client: TinkoffClient, ins: dict) -> dict:
    """Полная карточка инструмента (номинал облигации, шаг цены фьючерса)."""
    uid = ins.get('uid')
    if not uid:
        return ins
    if uid in _detail_cache:
        return _detail_cache[uid]

    method = DETAIL_METHODS.get((ins.get('instrumentType') or '').lower())
    detail = ins
    if method:
        try:
            detail = client.instrument_details(method, uid) or ins
        except TinkoffAPIError as exc:
            log.warning('%s %s: %s', method, ins.get('ticker'), exc)
    _detail_cache[uid] = detail
    return detail


def _convert(client: TinkoffClient, ins: dict, price: float) -> float | None:
    """Привести цену T-Invest к рублёвой за бумагу (как в current_price)."""
    kind = (ins.get('instrumentType') or '').lower()
    if kind == 'bond':
        detail = _details(client, ins)
        nominal_obj = detail.get('nominal') or {}
        nominal = _quotation_to_float(nominal_obj)
        currency = (nominal_obj.get('currency') or '').lower()
        if nominal <= 0 or currency not in ('', 'rub'):
            # Валютный номинал без курса пересчитать нечем — не подменяем цену.
            return None
        return round(price / 100.0 * nominal, 4)
    if kind == 'future':
        detail = _details(client, ins)
        step = _quotation_to_float(detail.get('minPriceIncrement'))
        amount = _quotation_to_float(detail.get('minPriceIncrementAmount'))
        if step > 0 and amount > 0:
            return round(price * amount / step, 2)
        return None
    return price


def _fetch(client: TinkoffClient, instruments: dict) -> dict:
    items = []
    for sec_code, class_code in list(instruments.items())[:MAX_INSTRUMENTS]:
        ins = _find(client, sec_code, class_code or '')
        if ins and ins.get('uid'):
            items.append((sec_code, class_code or ins.get('classCode', ''), ins))
    if not items:
        return {}

    by_uid = {ins['uid']: (sec_code, class_code, ins)
              for sec_code, class_code, ins in items}
    raw = client.last_prices(list(by_uid))
    stamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    result = {}
    for uid, price in raw.items():
        item = by_uid.get(uid)
        if not item:
            continue
        sec_code, class_code, ins = item
        converted = _convert(client, ins, price)
        if not converted or converted <= 0:
            continue
        result[sec_code] = {
            'sec_code': sec_code,
            'class_code': class_code,
            'price': converted,
            'qty': 0,
            'value': 0,
            'timestamp': stamp,
            'source': 'tinkoff',
        }
    return result


def get_prices(instruments: dict, force: bool = False) -> dict:
    """Последние цены для ``{sec_code: class_code}`` с кэшем на REFRESH_SECONDS.

    Возвращает ``{sec_code: row}`` с полями ``current_price``. Если другой поток
    уже обновляет кэш — отдаём предыдущий снимок, не блокируя запрос страницы.
    """
    global _prices_at
    if not instruments or not enabled():
        return {}

    now = time.time()
    with _lock:
        if not force and now - _prices_at < REFRESH_SECONDS:
            return dict(_prices_cache)

    client = _get_client()
    if client is None:
        return {}

    if not _fetch_lock.acquire(blocking=False):
        with _lock:
            return dict(_prices_cache)
    try:
        with _lock:
            if not force and time.time() - _prices_at < REFRESH_SECONDS:
                return dict(_prices_cache)
        try:
            rows = _fetch(client, instruments)
        except Exception as exc:  # noqa: BLE001
            log.warning('T-Invest last prices: %s', exc)
            rows = {}
        with _lock:
            if rows:
                _prices_cache.update(rows)
                _prices_at = time.time()
        return dict(rows)
    finally:
        _fetch_lock.release()


# ── Изменение к предыдущему дню ──────────────────────────────
# В ``current_price`` хранится только последняя цена, истории в БД нет,
# поэтому закрытие предыдущего торгового дня берём из дневных свечей T-Invest.
# Закрытие за сутки не меняется — кэш на календарный день по Москве.

def _msk_now() -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=3)


def _candle_day(ts_ns: int) -> str:
    """Дата свечи (МСК) в формате YYYY-MM-DD."""
    utc = datetime.fromtimestamp(ts_ns / 1_000_000_000, tz=timezone.utc)
    return (utc.replace(tzinfo=None) + timedelta(hours=3)).strftime('%Y-%m-%d')


def _prev_close(client: TinkoffClient, ins: dict) -> float | None:
    """Закрытие предыдущего торгового дня в рублях за бумагу (как current_price)."""
    uid = ins.get('uid')
    if not uid:
        return None
    try:
        candles = client.candles(uid, trading_days_ago(10), datetime.now(timezone.utc))
    except TinkoffAPIError as exc:
        log.warning('Дневные свечи %s: %s', ins.get('ticker'), exc)
        return None

    today = _msk_now().strftime('%Y-%m-%d')
    prev = None
    for candle in candles:
        if _candle_day(candle['ts_ns']) < today:
            prev = candle
    if prev is None:
        return None
    converted = _convert(client, ins, prev['close'])
    return converted if converted and converted > 0 else None


def _prev_close_conn():
    from app.db import get_connection
    return get_connection()


def _load_prev_closes(today: str) -> None:
    """Прочитать закрытия из БД, чтобы не тянуть свечи при каждом рестарте."""
    global _prev_close_day
    with _prev_close_lock:
        if _prev_close_day == today:
            return
        _prev_close_day = today
    try:
        conn = _prev_close_conn()
        conn.execute('CREATE TABLE IF NOT EXISTS prev_close ('
                     'sec_code TEXT PRIMARY KEY, day TEXT NOT NULL, price REAL NOT NULL)')
        conn.commit()
        rows = conn.execute('SELECT sec_code, day, price FROM prev_close').fetchall()
        conn.close()
    except Exception as exc:  # noqa: BLE001
        log.warning('Закрытия предыдущего дня из БД: %s', exc)
        return
    with _prev_close_lock:
        _prev_close_cache.clear()
        for row in rows:
            if row['day'] == today:
                _prev_close_cache[row['sec_code']] = (row['day'], row['price'])


def _store_prev_closes(today: str, values: dict) -> None:
    if not values:
        return
    try:
        conn = _prev_close_conn()
        conn.execute('CREATE TABLE IF NOT EXISTS prev_close ('
                     'sec_code TEXT PRIMARY KEY, day TEXT NOT NULL, price REAL NOT NULL)')
        conn.executemany(
            'INSERT INTO prev_close (sec_code, day, price) VALUES (?,?,?) '
            'ON CONFLICT(sec_code) DO UPDATE SET day=excluded.day, price=excluded.price',
            [(code, today, price) for code, price in values.items()])
        conn.commit()
        conn.close()
    except Exception as exc:  # noqa: BLE001
        log.warning('Сохранение закрытий предыдущего дня: %s', exc)


def _refresh_prev_closes(today: str, items: list) -> None:
    """Дозагрузить закрытия по бумагам, которых нет в кэше (фоновый поток)."""
    global _prev_close_refreshing
    try:
        client = _get_client()
        if client is None:
            return

        # Свечи по каждой бумаге — отдельный запрос: последовательно это
        # несколько секунд, поэтому поток фоновый, а страница берёт кэш.
        def load(item):
            sec_code, class_code = item
            ins = _find(client, sec_code, class_code or '')
            return sec_code, (_prev_close(client, ins) if ins else None)

        values = {}
        with ThreadPoolExecutor(max_workers=min(4, len(items))) as pool:
            for sec_code, value in pool.map(load, items):
                with _prev_close_lock:
                    _prev_close_cache[sec_code] = (today, value)
                if value:
                    values[sec_code] = value
        _store_prev_closes(today, values)
    finally:
        with _prev_close_lock:
            _prev_close_refreshing = False


_fv_cache: dict = {}       # sec_code -> (дата МСК, номинал | None)
_fv_lock = threading.Lock()
_aci_cache: dict = {}      # sec_code -> (дата МСК, НКД на бумагу | None)


def face_values(instruments: dict) -> dict:
    """``{sec_code: номинал в рублях}`` — только для облигаций.

    Нужен для сценария «закрыть по номиналу». Номинал берётся из карточки
    T-Invest (оттуда же, откуда пересчёт % → рубли для цен). Кэш — на день:
    номинал за сутки не меняется, а запросов к API лишних не хочется.
    """
    if not instruments or not enabled():
        return {}
    client = _get_client()
    if client is None:
        return {}

    today = _msk_now().strftime('%Y-%m-%d')
    result = {}
    for sec_code, class_code in list(instruments.items())[:MAX_INSTRUMENTS]:
        with _fv_lock:
            cached = _fv_cache.get(sec_code)
        if cached and cached[0] == today:
            if cached[1]:
                result[sec_code] = cached[1]
            continue

        ins = _find(client, sec_code, class_code or '')
        nominal = None
        if ins and (ins.get('instrumentType') or '').lower() == 'bond':
            detail = _details(client, ins)
            nom = detail.get('nominal') or {}
            value = _quotation_to_float(nom)
            currency = (nom.get('currency') or '').lower()
            # Валютный номинал без курса пересчитать нечем — сценарий не считаем.
            if value > 0 and currency in ('', 'rub'):
                nominal = round(value, 2)

        with _fv_lock:
            _fv_cache[sec_code] = (today, nominal)
        if nominal:
            result[sec_code] = nominal
    return result


def accrued_interests(instruments: dict) -> dict:
    """``{sec_code: НКД в рублях на одну бумагу}`` — только для облигаций.

    НКД (накопленный купонный доход) берётся из карточки T-Invest (поле ``aci``).
    Нужен для оценки «с НКД» (грязная цена = текущая цена + НКД). Кэш — на день,
    как у номиналов: НКД меняется плавно, лишние запросы к API не нужны.
    """
    if not instruments or not enabled():
        return {}
    client = _get_client()
    if client is None:
        return {}

    today = _msk_now().strftime('%Y-%m-%d')
    result = {}
    for sec_code, class_code in list(instruments.items())[:MAX_INSTRUMENTS]:
        with _fv_lock:
            cached = _aci_cache.get(sec_code)
        if cached and cached[0] == today:
            if cached[1]:
                result[sec_code] = cached[1]
            continue

        ins = _find(client, sec_code, class_code or '')
        aci = None
        if ins and (ins.get('instrumentType') or '').lower() == 'bond':
            detail = _details(client, ins)
            aci_obj = detail.get('aci') or {}
            value = _quotation_to_float(aci_obj)
            currency = (aci_obj.get('currency') or '').lower()
            if value > 0 and currency in ('', 'rub'):
                aci = round(value, 4)

        with _fv_lock:
            _aci_cache[sec_code] = (today, aci)
        if aci:
            result[sec_code] = aci
    return result


def previous_closes(instruments: dict) -> dict:
    """``{sec_code: закрытие предыдущего дня}`` для ``{sec_code: class_code}``.

    Нужно для процента изменения текущей цены к предыдущему дню. Ответ
    собирается из кэша (и из БД — переживает рестарт), а недостающие бумаги
    догружаются в фоне: страница не ждёт сеть.
    """
    global _prev_close_refreshing
    if not instruments or not enabled():
        return {}

    today = _msk_now().strftime('%Y-%m-%d')
    _load_prev_closes(today)

    items = [(code, cc) for code, cc in list(instruments.items())[:MAX_INSTRUMENTS]]
    with _prev_close_lock:
        missing = [(code, cc) for code, cc in items
                   if _prev_close_cache.get(code, ('', None))[0] != today]
        start_worker = bool(missing) and not _prev_close_refreshing
        if start_worker:
            _prev_close_refreshing = True
        result = {code: value for code, (day, value) in _prev_close_cache.items()
                  if day == today and value}

    if start_worker:
        threading.Thread(target=_refresh_prev_closes, args=(today, missing),
                         daemon=True, name='prev-close').start()
    return result
