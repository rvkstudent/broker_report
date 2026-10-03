# Cloudflare: туннели, DNS и адреса

Актуально на 2026-10-03. Аккаунт Cloudflare: `a1427d4f0a1f7204bd15b77641729636`,
зона `roman-it.dev` (`b72f22e9509246ac877a563bf32c2ecb`).
Сервер: `78.17.114.178` (Amsterdam, NL).

## Что теперь работает

| Адрес | Куда ведёт | Как |
|---|---|---|
| `https://broker.roman-it.dev` | веб-интерфейс BrokerReport | Cloudflare Tunnel `broker-report` |
| `https://vless.roman-it.dev/<TUNNEL_PATH>` | VLESS + WebSocket (8447) | Cloudflare Tunnel `proxy-vless-remote` |
| `direct.roman-it.dev:8446` | VLESS + WS + TLS (self-signed) | напрямую по IP, DNS only |
| `hysteria.roman-it.dev:9445/udp` | Hysteria2 | напрямую по IP, DNS only |
| `78.17.114.178:9444/tcp` | VLESS + REALITY | напрямую по IP (SNI любой) |

Оба туннеля подключены по 4 соединения в дата-центрах Амстердама.

## Туннели

| Имя | ID | Режим | Ingress |
|---|---|---|---|
| `broker-report` | `8f70e77d-a6ce-4fc3-b573-53a4bf18627c` | управляется из панели | `broker.roman-it.dev → http://broker-report:5000` |
| `proxy-vless-remote` | `11af7f43-19b4-4780-8b8c-cfaa5000f6cd` | управляется из панели | `vless.roman-it.dev → http://proxy-xray:8447` |

Токены лежат в `.env` (chmod 600), в git не попадают:

- `/home/roman/broker-report/.env` → `BROKER_TUNNEL_TOKEN`
- `/home/roman/server/.env` → `PROXY_TUNNEL_TOKEN`

Токен — секрет уровня root: он даёт доступ к туннелю. Утечка = чужой трафик через
ваш сервер. При компрометации пересоздайте туннель в панели (старый токен умрёт).

### Запуск / остановка

```bash
# приложение (+ туннель)
cd /home/roman/broker-report && docker compose -f docker-compose.server.yml --profile tunnel up -d

# прокси (+ туннель)
cd /home/roman/server && docker compose --profile tunnel up -d

# остановить только туннель
docker stop broker-cloudflared      # или proxy-cloudflared
```

## DNS-записи зоны `roman-it.dev`

| Тип | Имя | Значение | Проксирование |
|---|---|---|---|
| A | `direct` | `78.17.114.178` | **DNS only** (серый) |
| A | `hysteria` | `78.17.114.178` | **DNS only** (серый) |
| CNAME | `broker` | `8f70e77d….cfargotunnel.com` | proxied |
| CNAME | `vless` | `11af7f43….cfargotunnel.com` | proxied |
| A | `roman-it.dev`, `www` | `82.38.40.176` | proxied (старый сервер/сайт) |
| CNAME | `tunnel` | `4bcd11bf….cfargotunnel.com` | proxied — **мёртвый туннель**, можно удалить |

Записи `direct` и `hysteria` обязаны быть **DNS only**. Если включить оранжевое
облако, клиент попадёт в Cloudflare, который терминейтит TLS своим сертификатом
и не умеет пробрасывать произвольные порты 8446/9445 — подключение сломается.

## Остатки Nextcloud

Туннель `nextcloud` (`edcf8add-ce1b-4ac8-bb9d-d2338a3d946b`) в панели ещё
числится, статус `down`, DNS-запись удалена, на сервере от него ничего не
осталось. Удалять туннель из панели необязательно — он ничего не делает.
Мёртвые `proxy-vless-tunnel` и `vless-proxy` остаются как история; их
DNS-записи (`tunnel.roman-it.dev`) можно смело удалить.

## Проверка

```bash
# веб-интерфейс
curl -s -o /dev/null -w '%{http_code}\n' https://broker.roman-it.dev/login   # 200

# защита без входа
curl -s -o /dev/null -w '%{http_code}\n' https://broker.roman-it.dev/        # 302

# API с токеном QUIK
curl -s -o /dev/null -w '%{http_code}\n' -H "X-API-Token: <API_TOKEN>" \
     https://broker.roman-it.dev/api/quik-connected                          # 200

# прокси через туннель (400 = xray ответил на не-WebSocket запрос — это норма)
curl -s -o /dev/null -w '%{http_code}\n' https://vless.roman-it.dev/<TUNNEL_PATH>

# состояние туннелей
docker logs --tail 20 broker-cloudflared | grep Registered
docker logs --tail 20 proxy-cloudflared  | grep Registered
```

## Если туннель не поднимается

1. `docker logs broker-cloudflared` — ищем `ERR` и `Failed to dial to edge`.
2. Токен пустой или с лишними символами: `grep -c '^BROKER_TUNNEL_TOKEN=.' .env`
   должен вернуть `1`.
3. `$` в токене — Docker Compose подставит переменную. Токены Cloudflare
   содержат только base64 (`A-Za-z0-9+/=`), знака `$` там быть не должно.
4. Туннель в панели удалён, а токен остался — контейнер будет рестартовать
   вечно. Пересоздайте туннель и обновите `.env`.
