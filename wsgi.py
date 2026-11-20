"""WSGI-точка входа для gunicorn (продакшн).

Отличие от run.py: не поднимает встроенный сервер Flask, а готовит состояние
приложения (база, репликация Firebase, фоновый дозор файлов) и отдаёт объект
``application`` серверу gunicorn.

Запускать с ОДНИМ воркером: фоновые потоки (дозор отчётов и синхронизация с
Firebase) должны существовать в единственном экземпляре, а SQLite плохо
переносит одновременную запись из нескольких процессов. Параллелизм
достигается потоками: ``--workers 1 --threads 8``.
"""

import os

from app.envfile import load_env_file

# В Docker переменные приходят из env_file; локальный запуск gunicorn без
# compose должен вести себя так же. Вызов — до импорта app.db, потому что
# путь к базе фиксируется при импорте.
load_env_file()

from app.app import flask_app, start_watcher, _auto_import
from app.db import init_db
from app.replication import init_replication
from app.replication import push_async as push_to_cloud
from app.screener import start_collector as start_screener_collector


def _bootstrap():
    """Подготовить приложение перед первым запросом."""
    init_db()
    print('  [ok] База данных инициализирована', flush=True)

    try:
        if os.environ.get('BROKER_DISABLE_REPLICATION') == '1':
            print('  Репликация Firebase отключена (BROKER_DISABLE_REPLICATION=1)', flush=True)
        else:
            init_replication()  # pull из Firebase при старте + фоновый sync
    except Exception as e:
        print(f'  [warn] Firebase недоступен: {e}', flush=True)

    # Автоимпорт отчётов включается только явно (BROKER_AUTO_IMPORT=1).
    # Молчаливый импорт всех файлов из reports/ — источник сюрпризов: отчёты за
    # годы до корпоративных действий (сплит ВТБ 1:5000) ломают позиции.
    if os.environ.get('BROKER_AUTO_IMPORT') == '1':
        try:
            cnt = _auto_import()
            print(f'  Отчётов загружено: {cnt}', flush=True)
            if cnt:
                push_to_cloud()
        except Exception as e:
            print(f'  [warn] Автоимпорт отчётов: {e}', flush=True)

        start_watcher(interval=60)
        print('  Фоновый дозор: проверка новых отчётов каждые 60с', flush=True)
    else:
        print('  Автоимпорт отчётов выключен (BROKER_AUTO_IMPORT!=1)', flush=True)

    if start_screener_collector():
        print('  Скринер: сбор сделок T-Invest запущен', flush=True)
    else:
        print('  Скринер: выключен (нет TINKOFF_TOKEN)', flush=True)

    # Сигналы стратегии считаем постоянно в фоне, а не при открытии страницы.
    from app.signals_cache import start_background_refresh
    start_background_refresh()
    print('  Сигналы: фоновый пересчёт запущен', flush=True)

    # Индивидуальные параметры на инструмент + донастройка после сделок.
    from app import adaptive
    adaptive.start_background_adaptation()
    adaptive.seed_universe()
    print('  Адаптивная стратегия: донастройка параметров запущена', flush=True)

    if start_screener_collector():
        print('  Скринер: сбор сделок T-Invest запущен', flush=True)
    else:
        print('  Скринер: выключен (нет TINKOFF_TOKEN)', flush=True)


_bootstrap()

# gunicorn ищет переменную application
application = flask_app
