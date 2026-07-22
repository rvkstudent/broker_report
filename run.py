"""Startup script: parse HTML files and launch web interface."""
import os
import sys

from app.db import init_db
from app.app import flask_app, start_watcher, _auto_import


if __name__ == '__main__':
    print('═' * 50)
    print('  BrokerReport — Анализ брокерских отчётов')
    print('═' * 50)
    print()

    init_db()

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

    # Start background watcher (checks for new files every 60s)
    start_watcher(interval=60)
    print('  Фоновый дозор: проверка новых отчётов каждые 60с')

    print()
    print('  Запуск веб-интерфейса: http://127.0.0.1:5000')
    print()

    from app import app
    flask_app.run(host='127.0.0.1', port=5000, debug=False)
