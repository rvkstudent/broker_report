"""
Cloud replication for broker.db via Firebase Realtime Database.

Синхронизирует SQLite-базу с Firebase Realtime Database:
- При запуске: скачивает последнюю версию из облака (если есть)
- После изменений: загружает обновлённую БД в облако
- Конфликты: Last-Write-Wins (по времени последней записи)

Настройки в файле app/firebase_config.py.
Ключ сервисного аккаунта Firebase: app/firebase-key.json
"""

import os
import time
import base64
import threading
import logging
import sqlite3
from datetime import datetime, timezone

from app import firebase_config

logger = logging.getLogger(__name__)

DB_PATH = os.path.join(os.path.dirname(__file__), 'broker.db')
SYNC_STATE_PATH = os.path.join(os.path.dirname(__file__), '.broker_sync_state')

# Firebase Admin SDK инициализируется лениво (lazy)
_firebase_initialized = False
_firebase_lock = threading.Lock()

# Ссылка на узел в RTDB (кешируется после инициализации)
_rtdb_ref = None


def _init_firebase():
    """Инициализировать Firebase Admin SDK (однократно)."""
    global _firebase_initialized, _rtdb_ref
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

        cred = credentials.Certificate(firebase_config.SERVICE_ACCOUNT_PATH)
        database_url = firebase_config.get_database_url()

        firebase_admin.initialize_app(cred, {
            'databaseURL': database_url,
        })

        _rtdb_ref = db.reference(firebase_config.DB_PATH_IN_RTDB)
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


def _cloud_is_newer(cloud_updated: str | None) -> bool:
    """Сравнить updated в облаке с локальным состоянием.

    Возвращает True, если облачная версия новее локальной (нужен pull).
    Если локального состояния нет — считаем облако новее (первая синхронизация).
    """
    if not cloud_updated:
        return False
    local_ts = _read_local_state()
    if not local_ts:
        return True  # первой синхронизации нет — тянем из облака
    return cloud_updated > local_ts


def _local_is_newer(cloud_updated: str | None) -> bool:
    """Сравнить локальный файл с updated в облаке.

    Возвращает True, если локальная БД новее облачной (нужен push).
    Учитывает:
    - timestamp последней синхронизации (.broker_sync_state)
    - время модификации самого файла broker.db (mtime)

    Если облака нет — считаем локальное новее.
    Если локального состояния нет — проверяем по mtime файла.
    """
    if not os.path.exists(DB_PATH):
        return False

    # Время изменения файла БД (когда в последний раз меняли данные)
    file_mtime = datetime.fromtimestamp(
        os.path.getmtime(DB_PATH), tz=timezone.utc
    ).isoformat()

    local_ts = _read_local_state()

    if not local_ts:
        # Нет истории синхронизации — ориентируемся на mtime файла
        if not cloud_updated:
            return True  # облака нет — пушим
        return file_mtime > cloud_updated  # файл новее облака?

    if not cloud_updated:
        return True  # облака нет — пушим локальное

    # Если файл менялся ПОСЛЕ последнего push'а — локально новее
    if file_mtime > local_ts:
        return True

    return local_ts > cloud_updated


def push():
    """Загрузить broker.db в Firebase Realtime Database (base64).

    Пушит, только если локальная БД новее облачной версии или облака нет.
    """
    if not firebase_config.is_enabled():
        return False

    if not os.path.exists(DB_PATH):
        logger.warning('Push skipped: broker.db not found')
        return False

    try:
        _init_firebase()

        # Проверим, есть ли в облаке более свежая версия
        cloud = _rtdb_ref.get()
        cloud_updated = cloud.get('updated') if cloud else None

        if cloud_updated and not _local_is_newer(cloud_updated):
            logger.info(f'Push skipped: local version is older than cloud ({cloud_updated})')
            return False

        _checkpoint_db()

        with open(DB_PATH, 'rb') as f:
            encoded = base64.b64encode(f.read()).decode('utf-8')

        now = datetime.now(timezone.utc).isoformat()

        _rtdb_ref.set({
            'data': encoded,
            'size': os.path.getsize(DB_PATH),
            'updated': now,
        })

        # Сохраняем timestamp удачного push'а
        _write_local_state(now)

        logger.info(f'Pushed broker.db to Firebase RTDB ({os.path.getsize(DB_PATH)} bytes)')
        return True
    except Exception as e:
        logger.error(f'Push to Firebase RTDB failed: {e}')
        return False


def pull():
    """Скачать broker.db из Firebase Realtime Database (если существует).

    Тянет, только если облачная версия новее локальной.
    """
    if not firebase_config.is_enabled():
        return False

    try:
        _init_firebase()

        snapshot = _rtdb_ref.get()

        if snapshot is None or 'data' not in snapshot:
            logger.info('No database in Firebase RTDB yet, starting fresh')
            return True

        cloud_updated = snapshot.get('updated')
        if not _cloud_is_newer(cloud_updated):
            logger.info(f'Pull skipped: local version is fresher than cloud ({cloud_updated})')
            return True

        decoded = base64.b64decode(snapshot['data'])

        # Атомарная замена через временный файл
        tmp_path = DB_PATH + '.tmp'
        with open(tmp_path, 'wb') as f:
            f.write(decoded)

        if os.path.exists(DB_PATH):
            os.remove(DB_PATH)
        os.rename(tmp_path, DB_PATH)

        # Сохраняем timestamp удачного pull'а
        if cloud_updated:
            _write_local_state(cloud_updated)

        logger.info(f'Pulled broker.db from Firebase RTDB ({len(decoded)} bytes)')
        return True
    except Exception as e:
        logger.error(f'Pull from Firebase RTDB failed: {e}')
        return False


def sync():
    """Двунаправленная синхронизация: pull затем push."""
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
    """Инициализировать репликацию: pull при запуске + фоновый sync.

    Определяет направление синхронизации при старте:
    - Если облако новее → pull (скачиваем на этот хост)
    - Если локально новее → push (загружаем в облако)
    - Если одинаковое или нет данных → первая запись в облако

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

    try:
        _init_firebase()
    except Exception as e:
        logger.warning(f'Firebase init failed, replication unavailable: {e}')
        return False

    # Определяем направление синхронизации при старте
    cloud = _rtdb_ref.get()
    cloud_updated = cloud.get('updated') if cloud else None
    local_ts = _read_local_state()

    if not cloud_updated and not local_ts:
        # Ничего нет нигде — просто пушим
        logger.info('First start: pushing local DB to cloud')
        push()
    elif cloud_updated and not local_ts:
        # В облаке есть, локально нет — тянем
        logger.info(f'First sync on this host: pulling from cloud ({cloud_updated})')
        pull()
    elif _cloud_is_newer(cloud_updated):
        logger.info(f'Cloud is newer ({cloud_updated}) → pulling')
        pull()
    elif _local_is_newer(cloud_updated):
        logger.info(f'Local is newer → pushing')
        push()
    else:
        logger.info('Local and cloud are in sync')
        # На всякий случай — перепроверим, что облачные данные корректны
        push()

    # Фоновая синхронизация каждые 5 минут
    start_background_sync(interval=300)

    return True
