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
from datetime import datetime

from app.tinkoff_api import TinkoffAPIError, TinkoffClient, _quotation_to_float

log = logging.getLogger(__name__)

REFRESH_SECONDS = 20
MAX_INSTRUMENTS = 200
DETAIL_METHODS = {
    'bond': 'BondBy', 'future': 'FutureBy', 'share': 'ShareBy',
    'etf': 'EtfBy', 'currency': 'CurrencyBy',
}

_lock = threading.Lock()
_fetch_lock = threading.Lock()

_client: TinkoffClient | None = None
_client_error = ''
_uid_cache: dict = {}      # (sec_code, class_code) -> InstrumentShort | None
_detail_cache: dict = {}   # uid -> полная карточка
_prices_cache: dict = {}   # sec_code -> row
_prices_at = 0.0


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


def _find(client: TinkoffClient, sec_code: str, class_code: str) -> dict | None:
    """Найти инструмент по коду QUIK (тикер или ISIN)."""
    key = (sec_code.upper(), class_code or '')
    if key in _uid_cache:
        return _uid_cache[key]

    found = None
    try:
        instruments = client.find_instrument(sec_code)
        wanted = sec_code.upper()
        best = None
        for ins in instruments:
            ticker = (ins.get('ticker') or '').upper()
            isin = (ins.get('isin') or '').upper()
            if ticker != wanted and isin != wanted:
                continue
            if class_code and ins.get('classCode') == class_code:
                best = ins
                break
            if best is None:
                best = ins
        if best is None and instruments:
            best = instruments[0]
        found = best
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
