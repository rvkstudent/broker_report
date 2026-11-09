#!/bin/sh
# Выгружает корневые сертификаты для invest-public-api.tinkoff.ru в PEM.
#
# Зачем: API подписан УЦ Минцифры (Russian Trusted Sub CA). В python:slim и в
# большинстве Linux-образов этого корня нет, поэтому контейнер не проверит TLS.
# Проверку сертификата мы не отключаем — вместо этого даём клиенту PEM.
#
# macOS  — берём системную связку ключей (там сертификат уже доверенный);
# Linux  — берём цепочку с живого сервера (TOFU: доверяем тому, что отдала
#          сеть при первом запуске; для аудита лучше скачать официальный
#          сертификат с https://www.gosuslugi.ru/crt).
#
# Использование:  sh deploy/export-tinkoff-ca.sh [путь]
# Затем в .env:  TINKOFF_CA_BUNDLE=/app/secrets/tinkoff-ca.pem
set -eu

out=${1:-secrets/tinkoff-ca.pem}
mkdir -p "$(dirname "$out")"

if [ "$(uname -s)" = "Darwin" ]; then
    security find-certificate -a -p /Library/Keychains/System.keychain > "$out"
    security find-certificate -a -p "$HOME/Library/Keychains/login.keychain-db" >> "$out" 2>/dev/null || true
else
    openssl s_client -showcerts -servername invest-public-api.tinkoff.ru \
        -connect invest-public-api.tinkoff.ru:443 </dev/null 2>/dev/null \
        | awk '/BEGIN CERTIFICATE/{f=1} f{print} /END CERTIFICATE/{f=0}' > "$out"
fi

count=$(grep -c 'BEGIN CERTIFICATE' "$out" || true)
if [ "$count" -lt 1 ]; then
    echo "Не удалось получить сертификаты — файл пуст: $out" >&2
    exit 1
fi
echo "Сертификаты записаны: $out ($count шт.)"
