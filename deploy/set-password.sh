#!/bin/sh
# ┌────────────────────────────────────────────────────────────────────┐
# │  Пароль для входа в веб-интерфейс BrokerReport                     │
# └────────────────────────────────────────────────────────────────────┘
# Пароль не отображается при вводе, в открытом виде нигде не хранится
# и не выводится на экран.
#
# Хеш (PBKDF2-SHA256, формат werkzeug) пишется в secrets/password_hash.txt,
# а НЕ в .env: в хеше есть знак $, а Docker Compose в env_file разворачивает
# его как подстановку переменной и хеш портится.
#
# Запуск на сервере:  sh deploy/set-password.sh
# Дополнительные пакеты не нужны — используется только python3 стандартной
# поставки (hashlib + secrets), результат совместим с werkzeug внутри образа.

set -e
cd "$(dirname "$0")/.."
ENV_FILE=.env
HASH_FILE=secrets/password_hash.txt

if [ ! -f "$ENV_FILE" ]; then
    cp deploy/env.example "$ENV_FILE"
    echo "Создан $ENV_FILE из deploy/env.example"
fi

printf 'Логин (логин входа, Enter — roman): '
read -r LOGIN
if [ -z "$LOGIN" ]; then
    LOGIN=roman
fi

# Приглашение с вводом без отображения символов. Работает и когда ввод
# идёт из конвейера (например, при автоматической установке): тогда
# отключить эхо не требуется.
read_secret() {
    printf '%s' "$1"
    if [ -t 0 ]; then
        stty -echo
        read -r REPLY
        stty echo
        printf '\n'
    else
        read -r REPLY
    fi
}

read_secret 'Пароль: '
PASS1="$REPLY"
read_secret 'Повторите пароль: '
PASS2="$REPLY"

if [ "$PASS1" != "$PASS2" ]; then
    echo 'Пароли не совпадают.' >&2
    exit 1
fi

if [ "${#PASS1}" -lt 10 ]; then
    echo 'Пароль короче 10 символов — придумайте длиннее.' >&2
    exit 1
fi

HASH=$(python3 - "$PASS1" <<'PY'
import hashlib, secrets, sys
password = sys.argv[1].encode('utf-8')
salt = secrets.token_hex(8)
digest = hashlib.pbkdf2_hmac('sha256', password, salt.encode('ascii'), 600000)
print(f'pbkdf2:sha256:600000${salt}${digest.hex()}')
PY
)
unset PASS1 PASS2

mkdir -p secrets
umask 077
printf '%s\n' "$HASH" > "$HASH_FILE"
chmod 600 "$HASH_FILE"
unset HASH

python3 - "$LOGIN" "$ENV_FILE" <<'PY'
import pathlib, sys
login, path = sys.argv[1], pathlib.Path(sys.argv[2])
lines = path.read_text(encoding='utf-8').splitlines() if path.exists() else []
out = []
seen = False
for line in lines:
    key = line.split('=', 1)[0] if '=' in line else None
    if key == 'APP_USER':
        out.append(f'APP_USER={login}')
        seen = True
    elif key == 'APP_PASSWORD_HASH':
        # Старое место хранения: значение отсюда больше не используется,
        # но и вредить не должно — убираем, чтобы не путало
        continue
    else:
        out.append(line)
if not seen:
    out.append(f'APP_USER={login}')
path.write_text('\n'.join(out) + '\n', encoding='utf-8')
PY

chmod 600 "$ENV_FILE"

echo "Готово: логин «$LOGIN», хеш пароля записан в $HASH_FILE"
echo 'Применится после:  docker compose -f docker-compose.server.yml up -d --build'
echo 'Куки, выданные до смены пароля, остаются действительными.'
echo 'Отозвать все входы:  docker exec broker-report rm -f /app/data/session_epoch'
