#!/bin/sh
# ┌────────────────────────────────────────────────────────────────────┐
# │  Генерация секретов BrokerReport                                   │
# └────────────────────────────────────────────────────────────────────┘
# Что делает:
#   FLASK_SECRET — создаёт, если не задан (иначе сессии сбрасываются
#                  при каждом перезапуске приложения);
#   ADMIN_TOKEN  — заменяет известный всем admin123 на случайный;
#   API_TOKEN    — создаёт, если не задан, и печатает его один раз,
#                  чтобы вписать в lua/config.local.lua на машине с QUIK.
#
# Запуск на сервере:  sh deploy/init-secrets.sh
# Повторный запуск ничего не ломает: существующие значения сохраняются.

set -e
cd "$(dirname "$0")/.."
ENV_FILE=.env

if [ ! -f "$ENV_FILE" ]; then
    cp deploy/env.example "$ENV_FILE"
    echo "Создан $ENV_FILE из deploy/env.example"
fi

# Прочитать значение переменной из .env
get_env_var() {
    grep -E "^$1=" "$ENV_FILE" | head -1 | cut -d= -f2-
}

# Записать/заменить переменную в .env (значение передаётся аргументом,
# поэтому $ и кавычки внутри него безопасны)
set_env_var() {
    python3 - "$1" "$2" "$ENV_FILE" <<'PY'
import pathlib, sys
key, value, path = sys.argv[1], sys.argv[2], pathlib.Path(sys.argv[3])
lines = path.read_text(encoding='utf-8').splitlines() if path.exists() else []
out, replaced = [], False
for line in lines:
    if line.startswith(key + '='):
        out.append(f'{key}={value}')
        replaced = True
    else:
        out.append(line)
if not replaced:
    out.append(f'{key}={value}')
path.write_text('\n'.join(out) + '\n', encoding='utf-8')
PY
}

gen() { openssl rand -hex 32; }

echo '── Секреты BrokerReport ──────────────────────────────────'

if [ -z "$(get_env_var FLASK_SECRET)" ]; then
    set_env_var FLASK_SECRET "$(gen)"
    echo '  FLASK_SECRET : сгенерирован'
else
    echo '  FLASK_SECRET : уже задан, оставлен без изменений'
fi

case "$(get_env_var ADMIN_TOKEN)" in
    ''|admin123)
        set_env_var ADMIN_TOKEN "$(gen)"
        echo '  ADMIN_TOKEN  : заменён на случайный'
        ;;
    *)
        echo '  ADMIN_TOKEN  : уже задан, оставлен без изменений'
        ;;
esac

TOKEN="$(get_env_var API_TOKEN)"
if [ -z "$TOKEN" ]; then
    TOKEN="$(gen)"
    set_env_var API_TOKEN "$TOKEN"
    echo '  API_TOKEN    : сгенерирован (см. ниже)'
else
    echo '  API_TOKEN    : уже задан (см. ниже)'
fi

chmod 600 "$ENV_FILE"

echo
echo '── Токен для QUIK ───────────────────────────────────────'
echo 'Впишите эту строку в lua/config.local.lua на машине с QUIK:'
echo
echo "    api_token = \"$TOKEN\","
echo
echo "(файл config.local.lua добавлен в .gitignore — в git он не попадёт)"
