"""Startup script: parse HTML files and launch web interface."""
import os
import sys

from app.db import init_db
from app.app import flask_app, start_watcher, _auto_import
from app.replication import init_replication
from app.replication import push_async as push_to_cloud
from app.allowed_devices import DISABLE_AUTH


if __name__ == '__main__':
    print('═' * 50)
    print('  BrokerReport — Анализ брокерских отчётов')
    print('═' * 50)
    print()

    try:
        init_db()
        print('  [ok] База данных инициализирована')
    except Exception as e:
        print(f'  [ERROR] init_db: {e}')
        import traceback
        traceback.print_exc()
        sys.exit(1)

    # Инициализация репликации в Firebase (pull при старте + фоновый sync)
    init_replication()

    if len(sys.argv) > 1:
        # Import specific files
        from app.parser import parse_report
        for path in sys.argv[1:]:
            if os.path.exists(path):
                try:
                    rid = parse_report(path)
                    print(f'  ✓ {os.path.basename(path)} (id={rid})')
                except Exception as e:
                    print(f'  ✗ {os.path.basename(path)}: {e}')
            else:
                print(f'  ✗ Файл не найден: {path}')
    else:
        # Auto import all HTML files
        print('  Загрузка отчётов...')
        cnt = _auto_import()
        print(f'  Загружено: {cnt} отчётов')
        if cnt:
            push_to_cloud()

    # Start background watcher (checks for new files every 60s)
    start_watcher(interval=60)
    print('  Фоновый дозор: проверка новых отчётов каждые 60с')

    # Настройки хоста/порта из переменных окружения (для Docker)
    flask_host = os.environ.get('FLASK_HOST', '127.0.0.1')
    flask_port = int(os.environ.get('FLASK_PORT', 5000))
    flask_debug = os.environ.get('FLASK_DEBUG', 'false').lower() == 'true'

    auth_mode = 'ВКЛЮЧЕНА' if not DISABLE_AUTH else 'отключена (локальный режим)'
    print()
    print(f'  Админ-панель: /admin')
    print(f'  Авторизация устройств: {auth_mode}')
    print(f'  Запуск веб-интерфейса: http://{flask_host}:{flask_port}')
    print()

    from app import app
    flask_app.run(host=flask_host, port=flask_port, debug=flask_debug)
