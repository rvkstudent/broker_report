# BrokerReport

Анализ брокерских отчётов (HTML/XLSX) с веб-интерфейсом, SQLite и Docker.

## Быстрый старт (локальный)

```powershell
# Установка зависимостей
pip install flask beautifulsoup4 lxml firebase-admin pandas openpyxl

# Запуск (без авторизации устройств)
python run.py
```

Откройте http://127.0.0.1:5000

## Быстрый старт (Docker)

```bash
# Сборка и запуск
docker compose up -d

# Просмотр логов
docker compose logs -f
```

Откройте http://localhost:5000

> **При первом запуске:** авторизация устройств включена автоматически.
> Используйте админ-панель `/admin` с токеном `admin123` (или смените его в `docker-compose.yml`).

## Как это работает

1. HTML-отчёты брокера кладутся в папку `reports/`
2. При запуске скрипт автоматически импортирует все `.html`/`.HTML`/`.xlsx` файлы в SQLite
3. Фоновый дозор раз в минуту проверяет новые файлы
4. На дашборде — прибыль/убыток по трейд-сделкам (LIFO), открытые позиции, расходы на РЕПО
5. Фильтр по датам позволяет выбирать период для анализа
6. **Авторизация устройств** — администратор управляет доступом (см. ниже)
7. **Репликация БД в Firebase Realtime Database** (опционально)

## Структура

```
├── Dockerfile                # Образ для Docker
├── docker-compose.yml        # Docker-сервис
├── run.py                    # Точка входа
├── requirements.txt          # Зависимости Python
├── app/                      # исходный код
│   ├── app.py                # веб-интерфейс Flask + авторизация
│   ├── db.py                 # модели SQLite + аналитика
│   ├── parser.py             # парсинг HTML (Сбер)
│   ├── parser_vtb.py         # парсинг XLSX (ВТБ)
│   ├── replication.py        # репликация в Firebase RTDB
│   ├── firebase_config.py    # настройки Firebase
│   ├── allowed_devices.py    # модуль авторизации устройств
│   ├── allowed_devices.json  # список разрешённых устройств (runtime)
│   ├── broker.db             # SQLite (создаётся автоматически)
│   └── templates/            # Bootstrap 5 шаблоны
│       ├── base.html         #   базовый шаблон
│       ├── dashboard.html    #   дашборд
│       ├── report.html       #   просмотр отчёта
│       ├── admin.html        #   админ-панель устройств
│       ├── device_auth.html  #   страница авторизации устройства
│       └── device_status.html#   статус устройства
├── reports/                  # HTML-отчёты брокера
└── logs/                     # логи (Docker volume)
```

## Авторизация устройств

Система позволяет администратору управлять доступом к данным (страницы и API).

### Концепция

- **Device ID** — уникальный идентификатор устройства (генерируется браузером или задаётся вручную)
- **Админ-токен** — мастер-пароль для панели управления (задаётся через `ADMIN_TOKEN` в `docker-compose.yml`)
- **Список устройств** — хранится в `app/allowed_devices.json` (runtime, не в git)

### Управление доступом

1. Откройте `/admin` и введите админ-токен (по умолчанию `admin123`)
2. В форме "Добавить устройство" введите Device ID пользователя и название
3. Устройства можно включать/отключать индивидуально
4. При попытке доступа с неавторизованного устройства — страница-заглушка

### Отключение авторизации (локальная разработка)

```powershell
# Авторизация отключена по умолчанию при локальном запуске
$env:DISABLE_DEVICE_AUTH="true"
python run.py
```

### API регистрации устройств

```bash
curl -X POST http://localhost:5000/api/device-register \
  -H "Content-Type: application/json" \
  -d '{"device_id": "dev_abc123", "label": "Сервер QUIK", "admin_token": "admin123"}'
```

## Docker

### Сборка и запуск

```bash
# Стандартный запуск
docker compose up -d

# Пересборка образа
docker compose build --no-cache
docker compose up -d
```

### Переменные окружения

| Переменная | По умолчанию | Описание |
|------------|-------------|----------|
| `FLASK_HOST` | `0.0.0.0` | Хост для Flask |
| `FLASK_PORT` | `5000` | Порт |
| `FLASK_DEBUG` | `false` | Режим отладки |
| `ADMIN_TOKEN` | `admin123` | Токен администратора |
| `DISABLE_DEVICE_AUTH` | `true` (локально) / не задан (Docker) | Отключить авторизацию |

### Volumes

- `./reports:/app/reports` — HTML-отчёты на хосте
- `db_data:/app/app` — SQLite БД сохраняется между перезапусками
- `./logs:/app/logs` — логи (опционально)

## Репликация в Firebase Realtime Database

Проект поддерживает автоматическую репликацию `broker.db` в Firebase RTDB.

### Настройка

1. Создайте проект в [Firebase Console](https://console.firebase.google.com/)
2. **Project settings → Service accounts → Generate new private key**
3. Включите **Realtime Database** (Start in test mode)
4. Сохраните JSON-ключ как **`app/firebase-key.json`**
5. Добавьте volume для ключа в Docker: `./firebase-key.json:/app/app/firebase-key.json`

### Параметры

Настраиваются в `app/firebase_config.py`:

| Параметр | По умолчанию | Описание |
|----------|-------------|----------|
| `SERVICE_ACCOUNT_PATH` | `app/firebase-key.json` | Путь к JSON-ключу |
| `DB_PATH_IN_RTDB` | `broker_db` | Путь в RTDB |
| `SYNC_INTERVAL` | `300` (5 мин) | Интервал фоновой синхронизации |
