#!/bin/sh
# Настройка чистого сервера Ubuntu 24.04 под BrokerReport.
#
# Запускать от root на НОВОМ сервере, один раз:
#   scp deploy/bootstrap-server.sh root@<ip>:/root/ && ssh root@<ip> sh /root/bootstrap-server.sh
#
# Что делает (идемпотентно — повторный запуск безопасен):
#   1) обновляет пакеты, ставит таймзону Europe/Moscow;
#   2) создаёт swap 2 ГБ (на VPS с 2 ГБ RAM сборка образа без swap падает);
#   3) ставит Docker CE + compose plugin из официального репозитория;
#   4) создаёт пользователя roman, копирует ему SSH-ключи root и даёт sudo;
#   5) включает ufw: снаружи открыт только 22 (веб-доступ идёт через Cloudflare Tunnel);
#   6) создаёт каталог проекта /home/roman/broker-report;
#   7) включает unattended-upgrades для security-обновлений.

set -eu

APP_USER="${APP_USER:-roman}"
APP_DIR="/home/${APP_USER}/broker-report"

log() { printf '\n=== %s\n' "$1"; }

[ "$(id -u)" -eq 0 ] || { echo "запускать от root" >&2; exit 1; }

log "1/7 пакеты и таймзона"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get -y -qq upgrade
apt-get -y -qq install ca-certificates curl gnupg rsync ufw htop jq tmux unattended-upgrades
timedatectl set-timezone Europe/Moscow

log "2/7 swap 2G"
if ! swapon --show | grep -q '/swapfile'; then
    fallocate -l 2G /swapfile
    chmod 600 /swapfile
    mkswap /swapfile >/dev/null
    swapon /swapfile
    grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
    sysctl -w vm.swappiness=10 >/dev/null
    grep -q '^vm.swappiness' /etc/sysctl.conf || echo 'vm.swappiness=10' >> /etc/sysctl.conf
fi

log "3/7 Docker CE + compose"
if ! command -v docker >/dev/null 2>&1; then
    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
    chmod a+r /etc/apt/keyrings/docker.asc
    . /etc/os-release
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
https://download.docker.com/linux/ubuntu ${VERSION_CODENAME} stable" \
        > /etc/apt/sources.list.d/docker.list
    apt-get update -qq
    apt-get -y -qq install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
fi
systemctl enable --now docker
docker --version
docker compose version

log "4/7 пользователь ${APP_USER}"
if ! id "$APP_USER" >/dev/null 2>&1; then
    adduser --disabled-password --gecos "" "$APP_USER"
fi
usermod -aG sudo,docker "$APP_USER"
if [ -s /root/.ssh/authorized_keys ]; then
    install -d -m 700 -o "$APP_USER" -g "$APP_USER" "/home/${APP_USER}/.ssh"
    cp /root/.ssh/authorized_keys "/home/${APP_USER}/.ssh/authorized_keys"
    chown "$APP_USER:$APP_USER" "/home/${APP_USER}/.ssh/authorized_keys"
    chmod 600 "/home/${APP_USER}/.ssh/authorized_keys"
fi
echo "${APP_USER} ALL=(ALL) NOPASSWD:ALL" > "/etc/sudoers.d/${APP_USER}"
chmod 440 "/etc/sudoers.d/${APP_USER}"

log "5/7 firewall (только SSH снаружи)"
ufw --force reset >/dev/null
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp comment 'ssh'
ufw --force enable
ufw status verbose | head -8

log "6/7 каталог проекта"
install -d -o "$APP_USER" -g "$APP_USER" "$APP_DIR"

log "7/7 автообновления безопасности"
dpkg-reconfigure -f noninteractive unattended-upgrades >/dev/null 2>&1 || true

log "готово"
echo "Docker: $(docker --version)"
echo "Пользователь: ${APP_USER}, каталог: ${APP_DIR}"
echo "Дальше: rsync проекта и docker compose -f docker-compose.server.yml up -d --build"
