"""WSGI-точка входа для gunicorn (продакшн).

Отличие от run.py: не поднимает встроенный сервер Flask, а готовит состояние
приложения (база, репликация Firebase, фоновый дозор файлов) и отдаёт объект
``application`` серверу gunicorn.

Запускать с ОДНИМ воркером: фоновые потоки (дозор отчётов и синхронизация с
Firebase) должны существовать в единственном экземпляре, а SQLite плохо
переносит одновременную запись из нескольких процессов. Параллелизм
достигается потоками: ``--workers 1 --threads 8``.
"""

from app.app import flask_app, start_watcher, _auto_import
from app.db import init_db
from app.replication import init_replication
from app.replication import push_async as push_to_cloud


def _bootstrap():
    """Подготовить приложение перед первым запросом."""
    init_db()
    print('  [ok] База данных инициализирована', flush=True)

    try:
        init_replication()  # pull из Firebase при старте + фоновый sync
    except Exception as e:
        print(f'  [warn] Firebase недоступен: {e}', flush=True)

    try:
        cnt = _auto_import()
        print(f'  Отчётов загружено: {cnt}', flush=True)
        if cnt:
            push_to_cloud()
    except Exception as e:
        print(f'  [warn] Автоимпорт отчётов: {e}', flush=True)

    start_watcher(interval=60)
    print('  Фоновый дозор: проверка новых отчётов каждые 60с', flush=True)


_bootstrap()

# gunicorn ищет переменную application
application = flask_app
