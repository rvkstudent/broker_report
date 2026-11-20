"""Клиент T-Invest API (Т-Банк) для скринера объёмного анализа.

Используется только REST-транспорт (grpc-gateway) и `requests`, который уже
стоит в образе вместе с firebase-admin. gRPC-SDK не тянем: для скринера
достаточно unary-методов, а лишние зависимости (grpcio + сгенерированные
proto) утяжеляют образ в разы.

Проверенные контракты (src/docs/contracts/marketdata.proto):

* ``MarketDataService/GetLastTrades`` — обезличенные сделки. Метод гарантирует
  данные за последний час, поэтому глубже часа копать нельзя.
* ``Trade.quantity`` — КОЛИЧЕСТВО ЛОТОВ, а не акций. Чтобы получить акции,
  умножаем на ``Share.lot``.
* ``Trade.direction`` может быть ``TRADE_DIRECTION_UNSPECIFIED`` — тогда
  направление восстанавливается правилом тика уже в скринере.
* ``Trade.trade_source``: ``TRADE_SOURCE_EXCHANGE`` — биржевые сделки,
  ``TRADE_SOURCE_DEALER`` — сделки дилера (адресные, печатаются по
  фиксированной цене и портят объёмную статистику).

Лимит сервиса котировок — 600 unary-запросов в минуту на все методы сразу,
поэтому клиент сам держит общий ограничитель темпа.
"""

from __future__ import annotations

import atexit
import logging
import os
import random
import ssl
import tempfile
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone

import requests
from requests.adapters import HTTPAdapter

logger = logging.getLogger(__name__)

BASE_URL = 'https://invest-public-api.tinkoff.ru/rest/'
SHARES_URL = 'tinkoff.public.invest.api.contract.v1.InstrumentsService/Shares'
CANDLES_URL = 'tinkoff.public.invest.api.contract.v1.MarketDataService/GetCandles'
TRADES_URL = 'tinkoff.public.invest.api.contract.v1.MarketDataService/GetLastTrades'
LAST_PRICES_URL = 'tinkoff.public.invest.api.contract.v1.MarketDataService/GetLastPrices'
FIND_INSTRUMENT_URL = 'tinkoff.public.invest.api.contract.v1.InstrumentsService/FindInstrument'
INSTRUMENT_BY_URL = 'tinkoff.public.invest.api.contract.v1.InstrumentsService/{method}'

# Сервис котировок: 600 запросов/мин. Берём с запасом — тем же токеном
# могут пользоваться другие клиенты, а за превышение лимита прилетает 429.
DEFAULT_REQUESTS_PER_MINUTE = 480

_RETRY_HTTP_CODES = (429, 500, 502, 503, 504)


class TinkoffAPIError(RuntimeError):
    """Ошибка вызова T-Invest API."""

    def __init__(self, message: str, status: int | None = None, code: str | None = None):
        super().__init__(message)
        self.status = status
        self.code = code


class RateLimiter:
    """Скользящее окно: не больше N запросов за 60 секунд.

    Ограничитель общий для всех потоков процесса — иначе сборщик сделок и
    обновление универсума суммарно вылетят за лимит сервиса.
    """

    def __init__(self, per_minute: int = DEFAULT_REQUESTS_PER_MINUTE):
        self.per_minute = max(1, int(per_minute))
        self._stamps: deque[float] = deque()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                while self._stamps and now - self._stamps[0] >= 60.0:
                    self._stamps.popleft()
                if len(self._stamps) < self.per_minute:
                    self._stamps.append(now)
                    return
                wait = 60.0 - (now - self._stamps[0]) + 0.01
            time.sleep(min(max(wait, 0.01), 5.0))


def _quotation_to_float(q) -> float:
    """Quotation ``{units, nano}`` → float."""
    if q is None:
        return 0.0
    if isinstance(q, (int, float)):
        return float(q)
    try:
        units = int(q.get('units') or 0)
        nano = int(q.get('nano') or 0)
    except (TypeError, ValueError):
        return 0.0
    return units + nano / 1_000_000_000


def _timestamp_to_ns(ts) -> int:
    """google.protobuf.Timestamp → наносекунды эпохи."""
    if ts is None:
        return 0
    if isinstance(ts, str):
        dt = datetime.fromisoformat(ts.replace('Z', '+00:00'))
        return int(dt.timestamp() * 1_000_000_000)
    try:
        seconds = int(ts.get('seconds') or 0)
        nanos = int(ts.get('nanos') or 0)
    except (TypeError, ValueError):
        return 0
    return seconds * 1_000_000_000 + nanos


def _to_rfc3339(dt: datetime) -> str:
    """RFC 3339 в UTC — каноничная форма Timestamp для grpc-gateway."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'


def _normalize_direction(value) -> str:
    """Привести направление сделки к 'buy' / 'sell' / '' (неизвестно)."""
    text = str(value or '').upper()
    if 'BUY' in text:
        return 'buy'
    if 'SELL' in text:
        return 'sell'
    return ''


# ── доверие TLS ──────────────────────────────────────────────
#
# Сертификат invest-public-api.tinkoff.ru выдан УЦ Минцифры
# (Russian Trusted Sub CA). Этого корня нет в certifi, поэтому «из коробки»
# Python не может проверить цепочку, хотя curl и браузер её проверяют через
# системное хранилище. Отключать проверку сертификата нельзя — вместо этого
# берём доверенные корни из ОС (truststore) или из явно указанного файла.

_bundle_lock = threading.Lock()
_bundle_cache: dict = {}


def _combined_ca_bundle(extra_path: str) -> str:
    """certifi + дополнительный CA в одном временном файле (кэшируется)."""
    path = os.path.abspath(os.path.expanduser(extra_path))
    if os.path.isdir(path):
        # Docker создаёт каталог, если монтируемого файла нет на хосте
        raise TinkoffAPIError(
            f'TINKOFF_CA_BUNDLE указывает на каталог: {path}. '
            'Файл сертификата не смонтирован — выполните на хосте '
            'sh deploy/export-tinkoff-ca.sh')
    if not os.path.isfile(path):
        raise TinkoffAPIError(
            f'TINKOFF_CA_BUNDLE: файл не найден — {path}. '
            'Создайте его командой sh deploy/export-tinkoff-ca.sh')
    key = (path, os.path.getmtime(path))
    with _bundle_lock:
        if _bundle_cache.get('key') == key:
            return _bundle_cache['path']
    try:
        import certifi
        with open(certifi.where(), 'rb') as fh:
            base = fh.read()
    except Exception:  # certifi может отсутствовать в урезанной сборке
        base = b''
    with open(path, 'rb') as fh:
        extra = fh.read()
    fd, combined = tempfile.mkstemp(prefix='tinkoff-ca-', suffix='.pem')
    with os.fdopen(fd, 'wb') as fh:
        fh.write(base + b'\n' + extra + b'\n')
    with _bundle_lock:
        _bundle_cache['key'] = key
        _bundle_cache['path'] = combined
    atexit.register(lambda: os.path.exists(combined) and os.unlink(combined))
    return combined


class _TruststoreAdapter(HTTPAdapter):
    """Проверка TLS по хранилищу сертификатов ОС вместо certifi."""

    def __init__(self, ssl_context, **kwargs):
        self._ssl_context = ssl_context
        super().__init__(**kwargs)

    def init_poolmanager(self, *args, **kwargs):
        kwargs['ssl_context'] = self._ssl_context
        return super().init_poolmanager(*args, **kwargs)

    def proxy_manager_for(self, *args, **kwargs):
        kwargs['ssl_context'] = self._ssl_context
        return super().proxy_manager_for(*args, **kwargs)


def _system_ssl_context():
    """SSL-контекст с корнями ОС или None, если truststore недоступен."""
    try:
        import truststore
    except Exception:
        return None
    try:
        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except Exception:
        return None


class TinkoffClient:
    """Минимальный клиент нужных методов сервиса котировок и инструментов."""

    def __init__(self, token: str, *, base_url: str = BASE_URL,
                 requests_per_minute: int = DEFAULT_REQUESTS_PER_MINUTE,
                 timeout: float = 20.0, max_retries: int = 3,
                 session: requests.Session | None = None,
                 ca_bundle: str | None = None):
        if not token:
            raise TinkoffAPIError('Не задан токен T-Invest (TINKOFF_TOKEN)')
        self.token = token
        self.base_url = base_url
        self.timeout = timeout
        self.max_retries = max(1, int(max_retries))
        self.limiter = RateLimiter(requests_per_minute)
        # Источник доверенных корней: явный файл (TINKOFF_CA_BUNDLE) поверх
        # certifi → хранилище ОС (truststore) → certifi по умолчанию.
        bundle = ca_bundle or os.environ.get('TINKOFF_CA_BUNDLE') or ''
        self.verify = _combined_ca_bundle(bundle) if bundle else True
        self._ssl_context = None if bundle else _system_ssl_context()
        self._provided_session = session
        self._local = threading.local()

    # ── транспорт ────────────────────────────────────────────

    def _new_session(self) -> requests.Session:
        sess = requests.Session()
        if self._ssl_context is not None:
            sess.mount('https://', _TruststoreAdapter(self._ssl_context))
        return sess

    def _session(self) -> requests.Session:
        # requests.Session не потокобезопасен; в фоновом сборщике и в вебе
        # запросы идут из разных потоков, поэтому сессия на поток.
        if self._provided_session is not None:
            return self._provided_session
        sess = getattr(self._local, 'session', None)
        if sess is None:
            sess = self._new_session()
            self._local.session = sess
        return sess

    def _post(self, path: str, payload: dict) -> dict:
        url = path if path.startswith('http') else self.base_url + path
        headers = {
            'Authorization': f'Bearer {self.token}',
            'Content-Type': 'application/json',
            'Accept': 'application/json',
        }
        last_error: Exception | None = None
        for attempt in range(self.max_retries):
            self.limiter.acquire()
            try:
                resp = self._session().post(url, json=payload, headers=headers,
                                            timeout=self.timeout, verify=self.verify)
            except requests.exceptions.SSLError as exc:
                # Ретраи бессмысленны: сертификат не станет доверенным сам.
                raise TinkoffAPIError(
                    'TLS: не удалось проверить сертификат T-Invest. Он выдан УЦ '
                    'Минцифры (Russian Trusted CA) и отсутствует в наборе certifi. '
                    'Поставьте пакет truststore (корни из хранилища ОС) либо '
                    'укажите TINKOFF_CA_BUNDLE с PEM-файлом корневого сертификата. '
                    f'Детали: {exc}') from exc
            except requests.RequestException as exc:
                last_error = TinkoffAPIError(f'Сеть недоступна: {exc}')
                self._backoff(attempt)
                continue

            if resp.status_code == 200:
                try:
                    return resp.json()
                except ValueError as exc:
                    raise TinkoffAPIError(f'Некорректный JSON в ответе: {exc}',
                                          status=resp.status_code) from exc

            if resp.status_code in _RETRY_HTTP_CODES:
                last_error = TinkoffAPIError(
                    f'HTTP {resp.status_code}: {resp.text[:200]}',
                    status=resp.status_code)
                self._backoff(attempt)
                continue

            raise TinkoffAPIError(f'HTTP {resp.status_code}: {resp.text[:300]}',
                                  status=resp.status_code)
        raise last_error or TinkoffAPIError('Не удалось выполнить запрос')

    @staticmethod
    def _backoff(attempt: int) -> None:
        time.sleep(min(2 ** attempt, 8) + random.uniform(0, 0.3))

    # ── методы ───────────────────────────────────────────────

    def shares(self, instrument_status: str = 'INSTRUMENT_STATUS_BASE') -> list[dict]:
        """Список акций, доступных для торговли через API."""
        data = self._post(SHARES_URL, {'instrumentStatus': instrument_status})
        result = []
        for inst in data.get('instruments') or []:
            uid = inst.get('uid') or inst.get('figi')
            if not uid:
                continue
            try:
                lot = int(inst.get('lot') or 1)
            except (TypeError, ValueError):
                lot = 1
            result.append({
                'uid': uid,
                'figi': inst.get('figi') or '',
                'ticker': inst.get('ticker') or '',
                'class_code': inst.get('classCode') or inst.get('class_code') or '',
                'name': inst.get('name') or '',
                'lot': max(1, lot),
                'currency': (inst.get('currency') or 'RUB').upper(),
                'exchange': inst.get('exchange') or '',
                'api_trade_available': bool(inst.get('apiTradeAvailableFlag', True)),
            })
        return result

    def find_instrument(self, query: str) -> list[dict]:
        """Поиск инструмента по тикеру/ISIN/FIGI (InstrumentsService)."""
        data = self._post(FIND_INSTRUMENT_URL, {'query': query})
        return data.get('instruments') or []

    def instrument_details(self, method: str, uid: str) -> dict:
        """Полная карточка инструмента (BondBy/FutureBy/ShareBy/EtfBy)."""
        data = self._post(INSTRUMENT_BY_URL.format(method=method),
                          {'idType': 'INSTRUMENT_ID_TYPE_UID', 'id': uid})
        return data.get('instrument') or {}

    def last_prices(self, instrument_ids: list[str]) -> dict[str, float]:
        """Последние цены по списку uid → ``{uid: price}`` (пачками по 100)."""
        result: dict[str, float] = {}
        for index in range(0, len(instrument_ids), 100):
            chunk = instrument_ids[index:index + 100]
            data = self._post(LAST_PRICES_URL, {'instrumentId': chunk})
            for item in data.get('lastPrices') or []:
                uid = item.get('instrumentUid') or item.get('figi')
                price = _quotation_to_float(item.get('price'))
                if uid and price > 0:
                    result[uid] = price
        return result

    def candles(self, instrument_id: str, from_dt: datetime, to_dt: datetime,
                interval: str = 'CANDLE_INTERVAL_DAY') -> list[dict]:
        """Свечи инструмента. Объём в свече — в лотах."""
        payload = {
            'instrumentId': instrument_id,
            'from': _to_rfc3339(from_dt),
            'to': _to_rfc3339(to_dt),
            'interval': interval,
        }
        data = self._post(CANDLES_URL, payload)
        candles = []
        for c in data.get('candles') or []:
            try:
                volume_lots = int(c.get('volume') or 0)
            except (TypeError, ValueError):
                volume_lots = 0
            candles.append({
                'ts_ns': _timestamp_to_ns(c.get('time')),
                'open': _quotation_to_float(c.get('open')),
                'high': _quotation_to_float(c.get('high')),
                'low': _quotation_to_float(c.get('low')),
                'close': _quotation_to_float(c.get('close')),
                'volume_lots': volume_lots,
                'is_complete': bool(c.get('isComplete', True)),
            })
        candles.sort(key=lambda x: x['ts_ns'])
        return candles

    def last_trades(self, instrument_id: str, from_dt: datetime, to_dt: datetime,
                    trade_source: str = 'TRADE_SOURCE_EXCHANGE') -> list[dict]:
        """Обезличенные сделки за период (API гарантирует только последний час)."""
        payload = {
            'instrumentId': instrument_id,
            'from': _to_rfc3339(from_dt),
            'to': _to_rfc3339(to_dt),
            'tradeSource': trade_source,
        }
        data = self._post(TRADES_URL, payload)
        trades = []
        for t in data.get('trades') or []:
            ts_ns = _timestamp_to_ns(t.get('time'))
            price = _quotation_to_float(t.get('price'))
            try:
                lots = int(t.get('quantity') or 0)
            except (TypeError, ValueError):
                lots = 0
            if ts_ns <= 0 or price <= 0 or lots <= 0:
                continue
            trades.append({
                'ts_ns': ts_ns,
                'price': price,
                'lots': lots,
                'direction': _normalize_direction(t.get('direction')),
                'trade_source': t.get('tradeSource') or trade_source,
            })
        trades.sort(key=lambda x: x['ts_ns'])
        return trades


def trading_days_ago(days: int) -> datetime:
    """Грубая отсечка «N календарных дней назад» для запросов свечей."""
    return datetime.now(timezone.utc) - timedelta(days=days)
