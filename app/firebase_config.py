"""
Настройки Firebase Replication для BrokerReport.

КАК НАСТРОИТЬ:
─────────────────────────────────────────────────────
1. Скачайте JSON-ключ сервисного аккаунта Firebase:
   Firebase Console → Project settings → Service accounts
   → Generate new private key

2. Сохраните файл как  app/firebase-key.json  (рядом с этим файлом)

3. В Firebase Console включите Realtime Database:
   Build → Realtime Database → Create Database
   (Start in test mode для начала)

4. Готово! Репликация включится автоматически при запуске.

Если ключ не найден — репликация просто не запускается,
приложение работает как обычно.
─────────────────────────────────────────────────────
"""

import os
import json

# ─── Путь к файлу сервисного аккаунта Firebase ───
SERVICE_ACCOUNT_PATH = os.path.join(
    os.path.dirname(__file__),
    'firebase-key.json'
)

# ─── URL базы данных Realtime Database ────────────
# По умолчанию: https://{project_id}-default-rtdb.{region}.firebasedatabase.app
# Если None — формируется автоматически из project_id
DATABASE_URL = None  # например 'https://broker-6e6e6-default-rtdb.europe-west1.firebasedatabase.app'

# ─── Путь в базе данных для хранения БД ───────────
DB_PATH_IN_RTDB = 'broker_db'

# ─── Интервал фоновой синхронизации (секунд) ──────
SYNC_INTERVAL = 300  # 5 минут


def is_enabled() -> bool:
    """Проверяет, доступен ли файл сервисного аккаунта."""
    return os.path.exists(SERVICE_ACCOUNT_PATH)


def get_database_url() -> str:
    """Определяет URL Realtime Database."""
    if DATABASE_URL:
        return DATABASE_URL
    # Автоопределение из service account JSON
    if os.path.exists(SERVICE_ACCOUNT_PATH):
        with open(SERVICE_ACCOUNT_PATH, encoding='utf-8') as f:
            data = json.load(f)
        project_id = data.get('project_id', '')
        if project_id:
            return f'https://{project_id}-default-rtdb.europe-west1.firebasedatabase.app'
    return 'https://broker-6e6e6-default-rtdb.europe-west1.firebasedatabase.app'
