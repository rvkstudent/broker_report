"""
Модуль авторизации устройств для BrokerReport.

Позволяет администратору управлять доступом:
- Только устройства с разрешённым Device ID могут открывать страницы и API
- Устройства автоматически появляются в списке ожидания при попытке доступа
- Администратор в панели /admin нажимает «Разрешить» или «Запретить»

Файл с устройствами: app/allowed_devices.json

Структура JSON:
{
  "approved": [
    {"device_id": "...", "label": "...", "enabled": true, ...}
  ],
  "pending": [
    {"device_id": "...", "label": "...", "first_seen": "...", "last_seen": "..."}
  ]
}
"""

import os
import json
import uuid
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

# Путь к файлу с устройствами
ALLOWED_DEVICES_PATH = os.path.join(os.path.dirname(__file__), 'allowed_devices.json')

# По умолчанию авторизация ВЫКЛЮЧЕНА (для локальной разработки)
# В Docker-режиме включается автоматически через переменную окружения
DISABLE_AUTH = os.environ.get('DISABLE_DEVICE_AUTH', 'true').lower() == 'true'


def _load_store() -> dict:
    """Загрузить весь сторадж устройств (approved + pending)."""
    if not os.path.exists(ALLOWED_DEVICES_PATH):
        return {'approved': [], 'pending': []}
    try:
        with open(ALLOWED_DEVICES_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
        # Поддержка старого формата (список approved)
        if isinstance(data, list):
            return {'approved': data, 'pending': []}
        return {
            'approved': data.get('approved', []),
            'pending': data.get('pending', []),
        }
    except Exception as e:
        logger.error(f'Failed to load devices: {e}')
        return {'approved': [], 'pending': []}


def _save_store(store: dict):
    """Сохранить сторадж устройств."""
    with open(ALLOWED_DEVICES_PATH, 'w', encoding='utf-8') as f:
        json.dump({
            'approved': store.get('approved', []),
            'pending': store.get('pending', []),
        }, f, ensure_ascii=False, indent=2)


def get_admin_token() -> str:
    """Получить мастер-токен администратора из окружения."""
    return os.environ.get('ADMIN_TOKEN', 'admin123')


def generate_device_id() -> str:
    """Сгенерировать уникальный идентификатор устройства."""
    return uuid.uuid4().hex[:16]


def is_device_allowed(device_id: str | None) -> bool:
    """Проверить, разрешён ли доступ устройству."""
    if DISABLE_AUTH:
        return True
    if not device_id:
        return False
    store = _load_store()
    return any(
        d.get('device_id') == device_id and d.get('enabled', True)
        for d in store.get('approved', [])
    )


def add_pending_device(device_id: str) -> dict:
    """Добавить устройство в список ожидания (без токена).

    Вызывается middleware при попытке доступа с неавторизованного устройства.
    Если устройство уже в approved или pending — обновляем last_seen.
    """
    if not device_id:
        return {'success': False, 'error': 'Device ID не указан'}

    store = _load_store()
    now = datetime.now().isoformat()

    # Уже в approved — ничего не делаем
    for d in store.get('approved', []):
        if d.get('device_id') == device_id:
            d['last_seen'] = now
            _save_store(store)
            return {'success': True, 'status': 'approved'}

    # Уже в pending — обновляем last_seen
    for d in store.get('pending', []):
        if d.get('device_id') == device_id:
            d['last_seen'] = now
            _save_store(store)
            return {'success': True, 'status': 'pending'}

    # Новое устройство — добавляем в pending
    store.setdefault('pending', []).append({
        'device_id': device_id,
        'label': f'Устройство {device_id[:8]}',
        'first_seen': now,
        'last_seen': now,
    })
    _save_store(store)
    return {'success': True, 'status': 'pending'}


def approve_device(device_id: str, admin_token: str = '') -> dict:
    """Разрешить устройству доступ (pending → approved)."""
    expected_token = get_admin_token()
    if admin_token != expected_token:
        return {'success': False, 'error': 'Неверный токен администратора'}

    store = _load_store()
    now = datetime.now().isoformat()

    # Ищем в pending
    pending_devices = store.get('pending', [])
    device = None
    for i, d in enumerate(pending_devices):
        if d.get('device_id') == device_id:
            device = pending_devices.pop(i)
            break

    if not device:
        # Может уже в approved — просто включаем
        for d in store.get('approved', []):
            if d.get('device_id') == device_id:
                d['enabled'] = True
                d['updated_at'] = now
                _save_store(store)
                return {'success': True, 'device_id': device_id, 'action': 'enabled'}
        return {'success': False, 'error': 'Устройство не найдено'}

    # Переносим в approved
    store['pending'] = pending_devices
    store.setdefault('approved', []).append({
        'device_id': device['device_id'],
        'label': device.get('label', f'Устройство {device_id[:8]}'),
        'enabled': True,
        'first_seen': device.get('first_seen', now),
        'approved_at': now,
        'updated_at': now,
    })
    _save_store(store)
    return {'success': True, 'device_id': device_id, 'action': 'approved'}


def reject_device(device_id: str, admin_token: str = '') -> dict:
    """Запретить устройству доступ (удалить из pending)."""
    expected_token = get_admin_token()
    if admin_token != expected_token:
        return {'success': False, 'error': 'Неверный токен администратора'}

    store = _load_store()

    # Удаляем из pending
    store['pending'] = [
        d for d in store.get('pending', [])
        if d.get('device_id') != device_id
    ]

    # Можно также удалить из approved или отключить
    for d in store.get('approved', []):
        if d.get('device_id') == device_id:
            d['enabled'] = False
            d['updated_at'] = datetime.now().isoformat()

    _save_store(store)
    return {'success': True, 'device_id': device_id}


def remove_device(device_id: str, admin_token: str = '') -> dict:
    """Полностью удалить устройство из обоих списков."""
    expected_token = get_admin_token()
    if admin_token != expected_token:
        return {'success': False, 'error': 'Неверный токен администратора'}

    store = _load_store()
    store['approved'] = [d for d in store.get('approved', []) if d.get('device_id') != device_id]
    store['pending'] = [d for d in store.get('pending', []) if d.get('device_id') != device_id]
    _save_store(store)
    return {'success': True, 'device_id': device_id}


def get_all_devices(admin_token: str = '') -> dict:
    """Получить approved и pending устройства."""
    expected_token = get_admin_token()
    if admin_token != expected_token:
        return {'approved': [], 'pending': []}
    return _load_store()


def get_pending_count() -> int:
    """Сколько устройств ожидают подтверждения (для бейджа в меню)."""
    store = _load_store()
    return len(store.get('pending', []))


def toggle_device(device_id: str, enabled: bool, admin_token: str = '') -> dict:
    """Включить/отключить approved-устройство."""
    expected_token = get_admin_token()
    if admin_token != expected_token:
        return {'success': False, 'error': 'Неверный токен администратора'}

    store = _load_store()
    for d in store.get('approved', []):
        if d.get('device_id') == device_id:
            d['enabled'] = enabled
            d['updated_at'] = datetime.now().isoformat()
            _save_store(store)
            return {'success': True, 'device_id': device_id, 'enabled': enabled}
    return {'success': False, 'error': 'Устройство не найдено'}
