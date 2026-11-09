"""Загрузка .env без внешних зависимостей.

В Docker переменные приходят из ``env_file`` в docker-compose, а при локальном
запуске (``python run.py``) их взять неоткуда. Чтобы ``TINKOFF_TOKEN=...`` в
``.env`` работал одинаково в обоих режимах, читаем файл сами: python-dotenv
тянуть ради 20 строк не хочется.

Правила простые: ``KEY=VALUE``, ``#`` — комментарий, кавычки по краям
снимаются. Уже заданные переменные окружения не перетираются, поэтому
``export TINKOFF_TOKEN=...`` всегда главнее файла.
"""

import os


def load_env_file(path: str | None = None) -> int:
    """Прочитать .env в os.environ. Возвращает число применённых переменных."""
    if path is None:
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        path = os.path.join(root, '.env')
    if not os.path.isfile(path):
        return 0

    applied = 0
    try:
        with open(path, encoding='utf-8') as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith('#') or '=' not in line:
                    continue
                if line.startswith('export '):
                    line = line[len('export '):].lstrip()
                key, value = line.split('=', 1)
                key = key.strip()
                if not key or not key.replace('_', '').isalnum():
                    continue
                value = value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in '"\'':
                    value = value[1:-1]
                if key in os.environ:
                    continue
                os.environ[key] = value
                applied += 1
    except OSError:
        return 0
    return applied
