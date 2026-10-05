"""
Cloud replication for broker.db via Firebase Realtime Database.

АРХИТЕКТУРА (Firebase = source of truth, таблицы как в SQLite)
────────────────────────────────────────────────────────────
Firebase хранит ТОЧНО такие же таблицы, как локальный SQLite.
Каждая запись — отдельный узел в Firebase под своей таблицей.

  Firebase RTDB structure:
    broker_db/
        report/
            {filename}: { ... }           # key = filename (UNIQUE в SQLite)
        trade/
            {source}${deal_number}: {...} # key = source$deal_number (unique index)
        repo/
            {source}${deal_number}: {...}
        cash_flow/
            {id}: { ... }
        portfolio/
            {report_id}__{sec_name}: {...}
        financial_result/
            {report_id}: { ... }
        quik_trade/
            {source}${trade_num}: { ... }
        current_price/
            {sec_code}__{class_code}: {...}
        instrument/
            {sec_code}__{class_code}: {...}
        _meta/
            hostname: PC-1
            updated: ISO-8601

Правила синхронизации:
- Push: читает ВСЕ строки из локального SQLite, пишет их в Firebase
         под соответствующими таблицами. Использует update(), который
         НЕ удаляет данные от других инстансов — только добавляет/обновляет.
- Pull: читает ВСЕ строки из Firebase, INSERT OR IGNORE в локальный SQLite.
         Таким образом облачные данные накапливаются, локальные не теряются.

Настройки в файле app/firebase_config.py.
Ключ сервисного аккаунта Firebase: app/firebase-key.json
"""

import os
import re
import socket
import time
import threading
import logging
import sqlite3
import contextlib
from urllib.parse import urlparse
from datetime import datetime, timezone

from app import firebase_config
from app.db import DB_PATH, _get_db_dir

logger = logging.getLogger(__name__)

SYNC_STATE_PATH = os.path.join(_get_db_dir(), '.broker_sync_state')

# Предел размера узла, который забираем из Firebase за один pull.
# Firebase отдаёт узлы целиком, поэтому крупный узел — это и десятки
# мегабайт трафика, и рост SQLite. Превышение — пропуск с предупреждением.
PULL_MAX_ROWS = int(os.environ.get('BROKER_PULL_MAX_ROWS', '200000'))

# Узлы, пропущенные из-за размера: не дёргаем их повторно в этом процессе
_pull_skipped_tables = set()

# Firebase Admin SDK инициализируется лениво (lazy)
_firebase_initialized = False
_firebase_lock = threading.Lock()

# Ссылка на корневой узел БД в RTDB
_rtdb_root = None

# Хостнейм для идентификации источника данных
_HOSTNAME = os.environ.get('HOSTNAME') or os.environ.get('COMPUTERNAME') or 'unknown'

# Кеш: один раз проверяем доступность прокси при старте
_proxy_usable_cache = None


# ── Список таблиц для синхронизации: (имя_таблицы, функция_ключа, SQL_запрос) ──

def _fb_key(s: str) -> str:
    """Очистить строку для использования в качестве ключа Firebase RTDB.

    Запрещённые символы: . $ # [ ] /
    """
    return re.sub(r'[\.$#\[\]/]', '_', s)


_TABLES = [
    ('report',       lambda r: _fb_key(str(r['filename'])),
     'SELECT * FROM report'),
    ('trade',        lambda r: _fb_key(
        f"{r.get('source','')}_{r.get('deal_number','')}" if r.get('deal_number')
        else str(r['id'])
    ),
     'SELECT * FROM trade ORDER BY id'),
    ('repo',         lambda r: _fb_key(f"{r['source']}_{r.get('deal_number','') or r['id']}"),
     'SELECT * FROM repo'),
    ('cash_flow',    lambda r: _fb_key(str(r['id'])),
     'SELECT * FROM cash_flow'),
    ('portfolio',    lambda r: _fb_key(f"{r['report_id']}_{r['security_name']}"),
     'SELECT * FROM portfolio'),
    ('financial_result', lambda r: _fb_key(str(r['report_id'])),
     'SELECT * FROM financial_result'),
    ('quik_trade',   lambda r: _fb_key(f"{r['source']}_{r.get('trade_num','') or r['id']}"),
     'SELECT * FROM quik_trade'),
    ('current_price', lambda r: _fb_key(f"{r['sec_code']}_{r.get('class_code','')}"),
     'SELECT * FROM current_price'),
    ('instrument',   lambda r: _fb_key(f"{r['sec_code']}_{r.get('class_code','')}"),
     'SELECT * FROM instrument'),
    ('nalog',        lambda r: _fb_key(f"{r['year']}_{r.get('deal_number','') or r['id']}"),
     'SELECT * FROM nalog'),
]


def _detect_proxy_url() -> str | None:
    """Определить URL прокси из настроек системы.

    Порядок поиска:
    1. Системные настройки Windows (реестр Internet Settings) —
       проверяем ProxyEnable. Если прокси отключён в системе — возвращаем None,
       игнорируя «висящие» переменные окружения.
    2. Переменные окружения HTTPS_PROXY / HTTP_PROXY (если реестр нечитаем
       или система не Windows).

    Returns:
        URL прокси (например 'http://proxy:8080') или None.
    """
    # 1. Системные настройки Windows — самый авторитетный источник
    if os.name == 'nt':
        try:
            import winreg
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r'Software\Microsoft\Windows\CurrentVersion\Internet Settings'
            ) as key:
                enabled, _ = winreg.QueryValueEx(key, 'ProxyEnable')
                if not enabled:
                    logger.debug('Windows proxy is DISABLED in registry')
                    return None  # прокси выключен — игнорируем env vars
                # Прокси включён — читаем адрес из реестра
                server, _ = winreg.QueryValueEx(key, 'ProxyServer')
                if not server:
                    return None
                if '=' in server:
                    for part in server.split(';'):
                        part = part.strip()
                        if part.startswith('https='):
                            proxy_url = f'http://{part[6:]}'
                            logger.debug(f'Proxy from registry (https): {proxy_url}')
                            return proxy_url
                    first = server.split(';')[0].strip()
                    if '=' in first:
                        proxy_url = f'http://{first.split("=", 1)[1]}'
                    else:
                        proxy_url = f'http://{first}'
                    logger.debug(f'Proxy from registry: {proxy_url}')
                    return proxy_url
                proxy_url = f'http://{server}'
                logger.debug(f'Proxy from registry: {proxy_url}')
                return proxy_url
        except Exception:
            pass  # fallback к env vars

    # 2. Переменные окружения (если не смогли прочитать реестр)
    for key in ('HTTPS_PROXY', 'https_proxy', 'HTTP_PROXY', 'http_proxy'):
        val = os.environ.get(key)
        if val:
            logger.debug(f'Proxy from env {key}={val}')
            return val

    return None


def _is_windows_proxy_disabled() -> bool:
    """Проверить, отключён ли прокси в настройках Windows.

    Если пользователь явно выключил прокси в «Параметры → Сеть → Прокси»,
    реестр выставляет ProxyEnable=0. При этом переменные окружения
    HTTP_PROXY/HTTPS_PROXY могут всё ещё висеть (они не очищаются
    автоматически). Нужно принудительно очистить их для requests.
    """
    if os.name != 'nt':
        return False
    try:
        import winreg
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r'Software\Microsoft\Windows\CurrentVersion\Internet Settings'
        ) as key:
            enabled, _ = winreg.QueryValueEx(key, 'ProxyEnable')
            return not enabled
    except Exception:
        return False


def _proxy_is_usable(proxy_url: str | None = None) -> bool:
    """Проверить, доступен ли прокси-сервер (TCP-connect).

    Args:
        proxy_url: URL прокси. Если None — определяется автоматически.

    Returns:
        True если прокси не задан или доступен, False если недоступен.
    """
    global _proxy_usable_cache
    if _proxy_usable_cache is not None:
        return _proxy_usable_cache

    if proxy_url is None:
        proxy_url = _detect_proxy_url()

    if not proxy_url:
        _proxy_usable_cache = True  # нет прокси — считаем доступным
        return True

    try:
        parsed = urlparse(proxy_url)
        host = parsed.hostname
        port = parsed.port or (443 if parsed.scheme == 'https' else 80)

        sock = socket.create_connection((host, port), timeout=2)
        sock.close()
        logger.info(f'Proxy {proxy_url} is reachable')
        _proxy_usable_cache = True
        return True
    except (OSError, socket.timeout) as e:
        logger.warning(f'Proxy {proxy_url} is unreachable ({e}), will bypass')
        _proxy_usable_cache = False
        return False


@contextlib.contextmanager
def _proxy_scope():
    """Адаптивный контекстный менеджер прокси.

    Очищает HTTP_PROXY/HTTPS_PROXY из окружения, если прокси:
    - отключён в настройках Windows (ProxyEnable=0) — переменные env vars
      могли остаться, но requests будет их использовать, вызывая ошибки;
    - настроен, но TCP-недоступен (таймаут).

    После выхода из блока переменные окружения восстанавливаются.
    """
    clear_vars = []

    # Определяем URL прокси
    proxy_url = _detect_proxy_url()
    win_proxy_off = _is_windows_proxy_disabled()

    if win_proxy_off or (proxy_url and not _proxy_is_usable(proxy_url)):
        # Прокси отключён в Windows или настроен но недоступен —
        # очищаем переменные, чтобы requests не подхватил мёртвый прокси
        for key in ('HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy'):
            val = os.environ.pop(key, None)
            if val is not None:
                clear_vars.append((key, val))
        if win_proxy_off:
            logger.debug('Windows proxy is OFF — env vars cleared for Firebase scope')
        else:
            logger.debug('Proxy unreachable — env vars cleared for Firebase scope')

    try:
        yield
    finally:
        for key, val in clear_vars:
            os.environ[key] = val


def _init_firebase():
    """Инициализировать Firebase Admin SDK (однократно)."""
    global _firebase_initialized, _rtdb_root
    if _firebase_initialized:
        return

    with _firebase_lock:
        if _firebase_initialized:
            return

        if not firebase_config.is_enabled():
            raise FileNotFoundError(
                f'Файл сервисного аккаунта Firebase не найден:\n'
                f'  {firebase_config.SERVICE_ACCOUNT_PATH}\n'
                f'Положите firebase-key.json в папку app/ или отключите репликацию.'
            )

        try:
            import firebase_admin
            from firebase_admin import credentials, db
        except ImportError:
            logger.error(
                'firebase-admin не установлен. Выполните: pip install firebase-admin'
            )
            raise

        with _proxy_scope():
            cred = credentials.Certificate(firebase_config.SERVICE_ACCOUNT_PATH)
            database_url = firebase_config.get_database_url()

            firebase_admin.initialize_app(cred, {
                'databaseURL': database_url,
            })

            _rtdb_root = db.reference(firebase_config.DB_PATH_IN_RTDB)
        _firebase_initialized = True
        logger.info(f'Firebase initialized (RTDB: {database_url})')


def _checkpoint_db():
    """Принудительно сбросить WAL в основной файл для консистентного снепшота."""
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.close()
        return True
    except Exception as e:
        logger.error(f'Checkpoint WAL failed: {e}')
        return False


def _read_local_state() -> str | None:
    """Прочитать локальный timestamp последней синхронизации."""
    try:
        if os.path.exists(SYNC_STATE_PATH):
            with open(SYNC_STATE_PATH, 'r') as f:
                return f.read().strip()
    except Exception:
        pass
    return None


def _write_local_state(ts: str):
    """Записать локальный timestamp последней синхронизации."""
    try:
        with open(SYNC_STATE_PATH, 'w') as f:
            f.write(ts)
    except Exception as e:
        logger.warning(f'Failed to write sync state: {e}')


def _read_table(table_name: str, key_fn, sql: str) -> dict:
    """Прочитать таблицу из локального SQLite и вернуть как dict {key: row_dict}.

    Каждый ключ формируется key_fn(row), значения — вся строка как словарь.
    """
    import sqlite3 as _sqlite3
    conn = _sqlite3.connect(DB_PATH)
    conn.row_factory = _sqlite3.Row
    try:
        rows = conn.execute(sql).fetchall()
        result = {}
        for r in rows:
            rd = dict(r)
            # Убираем бинарные/несериализуемые поля
            for k, v in list(rd.items()):
                if isinstance(v, bytes):
                    rd[k] = base64.b64encode(v).decode()
                elif isinstance(v, (datetime,)):
                    rd[k] = v.isoformat()
            key = key_fn(rd)
            result[key] = rd
        return result
    finally:
        conn.close()


def _write_table_firebase(table_name: str, data: dict):
    """Записать таблицу в Firebase под broker_db/{table_name}.

    Использует update(), который добавляет/обновляет записи,
    НО НЕ удаляет существующие от других инстансов.
    """
    ref = _rtdb_root.child(table_name)
    # Отправляем пачками по 500 записей (лимит RTDB)
    items = list(data.items())
    for i in range(0, len(items), 500):
        chunk = dict(items[i:i + 500])
        ref.update(chunk)


def _push_table(table_name: str, key_fn, sql: str):
    """Прочитать таблицу из SQLite и записать в Firebase (append).

    Пишем через update() — только свои ключи, ничего не удаляя. Раньше для
    trade стоял set() (перезапись узла целиком), но теперь в облаке два
    экземпляра приложения — сервер и рабочая машина с QUIK. Перезапись узла
    означала бы, что экземпляр с неполной базой затирает данные второго.
    Дубликаты, которые раньше вычищал set(), отсекаются при pull()
    по паре (source, deal_number).
    """
    data = _read_table(table_name, key_fn, sql)
    if not data:
        logger.debug(f'Table {table_name}: no rows to push')
        return
    _write_table_firebase(table_name, data)
    logger.debug(f'Pushed {len(data)} rows to Firebase/{table_name}')


def _read_cloud_node(table_name: str, ref):
    """Прочитать узел RTDB как словарь {ключ: строка}.

    Firebase отдаёт массив (list), если все ключи узла — целые числа подряд:
    так происходит, когда ключом выбрано числовое поле (id строки).
    Обычный код с .items() на таком узле падает с
    «'list' object has no attribute 'items'», поэтому приводим список
    к словарю.

    Перед полным чтением делается «мелкий» запрос — только ключи: так
    узел на миллион записей не тянется в память и не кладётся в SQLite.
    """
    if table_name in _pull_skipped_tables:
        return None

    node_keys = ref.get(shallow=True)
    if not node_keys:
        return {}

    if len(node_keys) > PULL_MAX_ROWS:
        _pull_skipped_tables.add(table_name)
        logger.warning(
            'Узел %s содержит %d записей — больше лимита %d. Пропускаю узел: '
            'это десятки мегабайт трафика и рост базы. Проверьте, нет ли там '
            'дублей от повторных импортов; лимит меняется переменной '
            'BROKER_PULL_MAX_ROWS', table_name, len(node_keys), PULL_MAX_ROWS)
        return None

    cloud_data = ref.get()
    if not cloud_data:
        return {}
    if isinstance(cloud_data, list):
        logger.info('%s: узел пришёл массивом (%d элементов) — читаю по индексам',
                    table_name, len(cloud_data))
        return {str(i): row for i, row in enumerate(cloud_data) if row is not None}
    return cloud_data


def _pull_table(table_name: str, insert_sql: str, insert_params_template: tuple):
    """Прочитать таблицу из Firebase и влить в локальный SQLite.

    insert_sql — INSERT OR IGNORE с параметрами.
    insert_params_template — кортеж с именами колонок для подстановки.
    """
    import sqlite3 as _sqlite3
    ref = _rtdb_root.child(table_name)
    cloud_data = _read_cloud_node(table_name, ref)
    if not cloud_data:
        return 0

    conn = _sqlite3.connect(DB_PATH)
    try:
        cur = conn.cursor()
        count = 0

        # Для trade: собираем существующие (source, deal_number) для дедупликации
        existing_trades = set()
        if table_name == 'trade':
            for row in conn.execute(
                "SELECT source, deal_number FROM trade WHERE deal_number IS NOT NULL AND deal_number != ''"
            ).fetchall():
                existing_trades.add((row[0] or '', row[1] or ''))

        for key, row in cloud_data.items():
            if not isinstance(row, dict):
                continue

            # Проверка на дубликат по (source, deal_number) для trade
            if table_name == 'trade':
                src = str(row.get('source', '') or '')
                dn = str(row.get('deal_number', '') or '')
                if dn and (src, dn) in existing_trades:
                    continue  # уже есть — пропускаем

            # Восстанавливаем bytes из base64
            params = []
            for col in insert_params_template:
                val = row.get(col)
                if isinstance(val, str) and len(val) > 100 and val.startswith(('A', 'B', 'C', 'D', 'E', 'F', 'G', 'H', 'I', 'J', 'K', 'L', 'M', 'N', 'O', 'P', 'Q', 'R', 'S', 'T', 'U', 'V', 'W', 'X', 'Y', 'Z', 'a', 'b', 'c', 'd', 'e', 'f', 'g', 'h', 'i', 'j', 'k', 'l', 'm', 'n', 'o', 'p', 'q', 'r', 's', 't', 'u', 'v', 'w', 'x', 'y', 'z', '0', '1', '2', '3', '4', '5', '6', '7', '8', '9', '+', '/')):
                    # Примерная проверка на base64 — если похоже, пробуем декодировать
                    try:
                        decoded = base64.b64decode(val, validate=True)
                        params.append(decoded)
                    except Exception:
                        params.append(val)
                else:
                    params.append(val)
            try:
                cur.execute(insert_sql, params)
                if cur.rowcount > 0:
                    count += 1
                    # Добавляем в existing, чтобы следующие дубликаты тоже пропустить
                    if table_name == 'trade' and dn:
                        existing_trades.add((src, dn))
            except Exception as e:
                logger.debug(f'Pull {table_name}/{key}: {e}')
        conn.commit()
        return count
    finally:
        conn.close()


def push():
    """Загрузить все локальные таблицы в Firebase (append, без удаления).

    Каждая таблица пишется под свой узел broker_db/{table}.
    Используется update(), поэтому данные от других инстансов не затираются.
    """
    if not firebase_config.is_enabled():
        return False

    if not os.path.exists(DB_PATH):
        logger.warning('Push skipped: broker.db not found')
        return False

    try:
        with _proxy_scope():
            _init_firebase()

            _checkpoint_db()

            for table_name, key_fn, sql in _TABLES:
                _push_table(table_name, key_fn, sql)

            # Обновляем мета-информацию
            now = datetime.now(timezone.utc).isoformat()
            _rtdb_root.child('_meta').update({
                'hostname': _HOSTNAME,
                'updated': now,
            })

        _write_local_state(now)

        logger.info('Pushed all tables to Firebase RTDB')
        return True
    except Exception as e:
        logger.error(f'Push to Firebase RTDB failed: {e}')
        return False


def push_async():
    """Запустить push() в фоновом потоке — не блокирует старт."""
    def _push_worker():
        try:
            push()
        except Exception as e:
            logger.error(f'Async push failed: {e}')
    th = threading.Thread(target=_push_worker, daemon=True)
    th.start()
    return th


def pull():
    """Загрузить все таблицы из Firebase и влить в локальный SQLite.

    Использует INSERT OR IGNORE — существующие строки не перезаписываются,
    новые добавляются. Данные из облака накапливаются, локальные не теряются.
    """
    if not firebase_config.is_enabled():
        return False

    try:
        with _proxy_scope():
            _init_firebase()

            meta = _rtdb_root.child('_meta').get()
            cloud_updated = (meta or {}).get('updated', '')
            if not cloud_updated:
                logger.info('Firebase RTDB is empty, nothing to pull')
                return True

            local_ts = _read_local_state()

            # Если локальная версия свежее — не тянем
            if local_ts and local_ts >= cloud_updated:
                logger.info(f'Pull skipped: local is fresher than cloud ({cloud_updated})')
                return True

            total = 0
            # Определяем колонки для INSERT для каждой таблицы
            col_map = {
                'report': ('filename', 'contract', 'investor', 'period_start', 'period_end', 'created_at'),
                'trade': ('report_id', 'trade_date', 'settle_date', 'trade_time', 'security_name',
                          'security_code', 'class_code', 'currency', 'side', 'quantity', 'price',
                          'amount', 'nkd', 'broker_fee', 'exchange_fee', 'deal_number',
                          'comment', 'status', 'source', 'broker', 'account',
                          'flags', 'operation', 'operation_type'),
                'repo': ('report_id', 'trade_date', 'trade_time', 'security_name', 'security_code',
                         'currency', 'side', 'quantity', 'price_part1', 'nkd_part1', 'amount_part1',
                         'date_part1', 'repo_rate', 'repo_interest', 'price_part2', 'nkd_part2',
                         'amount_part2', 'date_part2', 'broker_fee', 'exchange_fee', 'deal_number',
                         'status', 'source'),
                'cash_flow': ('report_id', 'date', 'description', 'currency', 'credit', 'debit'),
                'portfolio': ('report_id', 'security_name', 'isin', 'currency', 'qty_start',
                              'price_start', 'value_start', 'qty_end', 'price_end', 'value_end',
                              'qty_change', 'value_change'),
                'financial_result': ('report_id', 'income_code', 'income_amount', 'expense_code',
                                     'expense_amount', 'taxable_amount', 'tax_rate', 'tax_calculated',
                                     'tax_withheld', 'tax_due'),
                'quik_trade': ('trade_num', 'sec_code', 'class_code', 'price', 'qty', 'value',
                               'accruedint', 'yield', 'settlecode', 'reporate', 'repovalue',
                               'repo2value', 'repoterm', 'period', 'trade_date', 'trade_time',
                               'source', 'side', 'flags', 'operation',
                               'broker', 'account', 'operation_type'),
                'current_price': ('sec_code', 'class_code', 'price', 'qty', 'value', 'timestamp'),
                'instrument': ('sec_code', 'class_code', 'lotsize', 'min_step', 'short_name',
                              'full_name', 'updated_at'),
                'nalog': ('year', 'instrument_name', 'instrument_code', 'side', 'deal_date',
                          'deal_number', 'fnc_code', 'price', 'quantity', 'amount',
                          'currency', 'income', 'expense', 'source_file'),
            }

            for table_name, _, _ in _TABLES:
                cols = col_map.get(table_name)
                if not cols:
                    continue
                placeholders = ','.join(['?' for _ in cols])
                cols_str = ','.join(cols)
                insert_sql = f'INSERT OR IGNORE INTO {table_name}({cols_str}) VALUES({placeholders})'
                cnt = _pull_table(table_name, insert_sql, cols)
                total += cnt
                if cnt:
                    logger.debug(f'Pulled {cnt} new rows into {table_name}')

        if total:
            _write_local_state(cloud_updated)

        logger.info(f'Pulled {total} new rows from Firebase RTDB')
        return True
    except Exception as e:
        logger.error(f'Pull from Firebase RTDB failed: {e}')
        return False


def sync():
    """Двунаправленная синхронизация: pull затем push.

    Сначала вливаем данные из облака в локальную БД (pull),
    потом отправляем свои данные в облако (push).
    """
    if not firebase_config.is_enabled():
        return False
    pull()
    push()
    return True


def start_background_sync(interval=None):
    """Запустить фоновый поток для периодической синхронизации.

    Args:
        interval: интервал в секундах (по умолч. из firebase_config.py)
    """
    if not firebase_config.is_enabled():
        return

    if interval is None:
        interval = firebase_config.SYNC_INTERVAL

    def _sync_loop():
        while True:
            time.sleep(interval)
            try:
                # При фоновой синхронизации: сначала тянем, потом пушим
                pull()
                push()
            except Exception as e:
                logger.error(f'Background sync error: {e}')

    th = threading.Thread(target=_sync_loop, daemon=True)
    th.start()
    logger.info(f'Background sync started (interval={interval}s)')


def init_replication():
    """Инициализировать репликацию: sync при запуске + фоновый sync.

    При старте:
    1. Pull — загружает последний snapshot из Firebase в локальную БД
       (если облачная версия новее локальной).
    2. Push — создаёт новый snapshot в Firebase с текущими локальными
       данными (append, предыдущие snapshot'ы сохраняются).

    Firebase = источник истины, всё идёт append-only, ничего не удаляется.

    Вызывать после init_db() в run.py.

    Returns:
        True если репликация включена и работает, False если отключена.
    """
    if not firebase_config.is_enabled():
        logger.info(
            'Firebase replication disabled. '
            'Чтобы включить: положите firebase-key.json в папку app/'
        )
        return False

    # Pull: тянем последнюю версию из облака (синхронно — нужно для расчётов)
    # Push: отправляем свои данные асинхронно — не блокирует старт
    pull()
    push_async()

    # Фоновая синхронизация каждые 5 минут
    start_background_sync(interval=300)

    return True

    # Фоновая синхронизация каждые 5 минут
    start_background_sync(interval=300)

    return True
