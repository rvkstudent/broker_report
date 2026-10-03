# Развёртывание BrokerReport на сервере

Стек: **gunicorn + SQLite + Cloudflare Tunnel**. Наружу не открывается ни один
порт — доступ идёт через туннель, TLS обеспечивает Cloudflare. Отдельный
сертификат и nginx не нужны.

```
QUIK (prices.lua) ──HTTPS──┐
                           ├──> broker.roman-it.dev ──Cloudflare Tunnel──> broker-report:5000
браузер (логин+пароль) ────┘
```

> Текущее состояние Cloudflare (ID туннелей, DNS-записи, как проверить и как
> починить) — в отдельном файле [`CLOUDFLARE.md`](CLOUDFLARE.md).

---

## 1. Что нужно подготовить заранее

| Что | Где взять |
|---|---|
| Токен туннеля | Cloudflare → Zero Trust → Networks → Tunnels → создать туннель → скопировать токен |
| Public hostname | В том же туннеле: `broker.roman-it.dev` → `http://broker-report:5000` |
| `firebase-key.json` | Из локального проекта `app/firebase-key.json` (в git не хранится) |

---

## 2. Перенос файлов на сервер

С локальной машины (из корня проекта):

```bash
rsync -av --delete \
  --exclude '.git' --exclude 'temp' --exclude 'lua_modules' \
  --exclude '*.db' --exclude 'logs' --exclude '.venv' --exclude '__pycache__' \
  --exclude 'app/firebase-key.json' --exclude '.env' \
  ./ vps-root:/home/roman/broker-report/
```

Ключ Firebase — отдельно, только по защищённому каналу:

```bash
ssh vps-root 'mkdir -p /home/roman/broker-report/secrets && chmod 700 /home/roman/broker-report/secrets'
scp app/firebase-key.json vps-root:/home/roman/broker-report/secrets/firebase-key.json
ssh vps-root 'chmod 600 /home/roman/broker-report/secrets/firebase-key.json'
```

---

## 3. Настройка на сервере

```bash
ssh vps-root
cd /home/roman/broker-report

# 3.1. Пароль для входа в веб-интерфейс (ввод не отображается)
#      Пишет хеш в secrets/password_hash.txt, в .env — только логин.
sh deploy/set-password.sh

# 3.2. Ключ сессий, токен для QUIK, замена admin123
sh deploy/init-secrets.sh      # в конце покажет токен для config.local.lua

# 3.3. Токен туннеля
nano .env                      # вписать BROKER_TUNNEL_TOKEN=...
```

Проверить, что секреты на месте (значения не печатать):

```bash
# ожидается 4 — пароль в .env не хранится, он в secrets/password_hash.txt
grep -cE '^(FLASK_SECRET|API_TOKEN|ADMIN_TOKEN|BROKER_TUNNEL_TOKEN)=.+' .env

# хеш пароля: ровно 2 знака $ и длина больше 80 (иначе хеш повреждён)
awk '{print length($0), gsub(/\$/,"$")}' secrets/password_hash.txt
```

> **Почему пароль не в `.env`.** Формат хеша — `pbkdf2:sha256:600000$соль$хеш`,
> и знак `$` в нём обязателен. Docker Compose в значениях из `env_file`
> разворачивает `$` как подстановку переменной и заменяет `$соль` на пустую
> строку — хеш молча портится, и вход перестаёт работать без единой ошибки
> в логах. Поэтому хеш лежит в отдельном файле, который подключается в
> контейнер только для чтения. Если задаёте хеш через `.env`, удваивайте
> знак: `$$`.

---

## 4. Запуск

```bash
cd /home/roman/broker-report
docker compose -f docker-compose.server.yml up -d --build
docker compose -f docker-compose.server.yml --profile tunnel up -d
docker compose -f docker-compose.server.yml ps
```

Проверка изнутри сервера:

```bash
docker exec broker-report python -c \
  "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:5000/healthz').read().decode())"
```

Проверка снаружи:

```bash
curl -sS -o /dev/null -w '%{http_code}\n' https://broker.roman-it.dev/healthz   # 200
curl -sS -o /dev/null -w '%{http_code}\n' https://broker.roman-it.dev/          # 302 -> /login
```

---

## 5. Настройка QUIK

На машине с QUIK, рядом с `prices.lua`:

```bash
cp lua/config.local.lua.example lua/config.local.lua
```

В `config.local.lua` указать адрес и токен из шага 3.2:

```lua
return {
    api_base  = "https://broker.roman-it.dev",
    api_token = "<токен из init-secrets.sh>",
    timeout   = 15,
}
```

Файл `config.local.lua` в `.gitignore` — в публичный репозиторий не попадёт.
Если проверка TLS не пройдёт (старый корневой сертификат в QUIK), в этом же
файле можно поставить `verify_tls = false` — но это снижает защиту канала.

---

## 6. Обновление версии

```bash
cd /home/roman/broker-report
# (повторить rsync с локальной машины)
docker compose -f docker-compose.server.yml up -d --build
```

База лежит на volume `broker_data`, отчёты — в `./reports`, поэтому обновление
образа данные не затрагивает.

---

## 7. Диагностика

```bash
docker compose -f docker-compose.server.yml logs -f broker-report
docker compose -f docker-compose.server.yml logs -f cloudflared
```

| Симптом | Причина / что делать |
|---|---|
| `502` от Cloudflare | контейнер не поднялся: смотреть логи `broker-report` |
| `1033` от Cloudflare | туннель без активного коннектора: проверить `BROKER_TUNNEL_TOKEN` и `--profile tunnel` |
| Всегда просит войти заново | не задан `FLASK_SECRET` → `sh deploy/init-secrets.sh` |
| Вход с верным паролем даёт «Форма устарела» | в браузере нет куки сессии: откройте страницу входа заново, включите куки; либо в логах `Хеш пароля повреждён` → `sh deploy/set-password.sh` |
| В логах `Хеш пароля (...) повреждён` | знак `$` в хеше съеден подстановкой в `.env`: пароль надо задавать файлом — `sh deploy/set-password.sh` |
| QUIK получает `401` | токен в `config.local.lua` не совпадает с `API_TOKEN` в `.env` |
| В логах `AUTH-FAIL` пачками | кто-то подбирает пароль: сменить пароль, при желании закрыть по fail2ban |
| Пустой дашборд после старта | не подтянулись данные из Firebase: проверить `secrets/firebase-key.json` и логи |

---

## 8. Безопасность: что учтено

- Пароль хранится только хешем (PBKDF2-SHA256, 600 000 итераций), в открытом виде не пишется нигде.
- Хеш пароля лежит в `secrets/password_hash.txt`, а не в `.env`: знак `$` внутри хеша
  ломается подстановкой переменных Docker Compose.
- Сессионная кука: `HttpOnly`, `SameSite=Lax`, `Secure` (только HTTPS).
- Все изменяющие запросы требуют CSRF-токен.
- Смена пароля не отзывает уже выданные куки. Чтобы отозвать все входы
  (например, если кука могла утечь): `docker exec broker-report rm -f /app/data/session_epoch`.
  После выхода из веб-интерфейса это делается автоматически.
- Подбор пароля ограничен: 10 попыток за 5 минут с одного IP, затем 429.
- API QUIK защищён отдельным токеном (`X-API-Token`), не паролем.
- Whitelist устройств отключён (`DISABLE_DEVICE_AUTH=true`) — он мешал бы работе с телефона.
- Наружу открыт только исходящий туннель; входящие порты закрыты.
- `.env`, `secrets/`, `*.db`, `config.local.lua` исключены из git и из образа.
