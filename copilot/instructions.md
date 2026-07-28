# BrokerReport — инструкции для агента

## Архитектура проекта

- `run.py` — точка входа: init_db → init_replication → _auto_import → Flask
- `app.py` — Flask-приложение: роуты, API, фоновый дозор файлов
- `db.py` — SQLite: единая таблица `trade` для всех сделок + LIFO-матчинг
- `parser.py` — парсинг HTML-отчётов Сбера (BeautifulSoup + lxml)
- `parser_vtb.py` — парсинг XLSX-отчётов ВТБ (pandas + openpyxl)
- `replication.py` — синхронизация с Firebase RTDB (push_async — не блокирует)
- `lua/prices.lua` — Lua-скрипт для QUIK: цены + сделки

## Единая таблица `trade`

Все сделки — в одной таблице с полем `source`:
- `source='sber'` — из HTML-отчёта Сбера
- `source='vtb'` — из XLSX-отчёта ВТБ
- `source='quik'` — из QUIK OnTrade/OnAllTrade

**QUIK-трейды НЕ хранятся отдельно в `quik_trade` для расчётов.**  
`quik_trade` — только для обратной совместимости (дублируется при записи).

Новые колонки в `trade`: `broker`, `account`, `class_code`, `flags`, `operation`, `operation_type`

## Firebase = Source of Truth

- **Push**: `push_async()` — асинхронно (фоновый поток), не блокирует старт
- **Pull**: синхронно при старте, загружает данные с других хостов
- **Ключи Firebase для trade**: `{deal_number}` (из отчётов), `quik_{deal_number}` (из QUIK)
- После каждого `save_quik_trades()` — автоматический push в Firebase
- Фоновый sync: daemon-поток с периодическим pull+push

## LIFO-матчинг

`_match_trades_lifo()` читает ТОЛЬКО из `trade` (без `_fetch_quik_trades`).

Правила матчинга:
- `broker='sber'`: `source='sber'` + `source='quik' AND broker='sber'`
- `broker='vtb'`: `source='vtb'` + `source='quik' AND broker='vtb'`
- `broker='all'`: каждый источник + его QUIK — отдельно

Дедупликация: `seen_deals_all` по `(sec_code, deal_number)` — первый выигрывает.

## Расчёт прибыли

LIFO (Last In, First Out):
- Покупки → в конец очереди `buy_queue`
- Продажи → снимают с **конца** очереди (самые свежие покупки)
- Прибыль лота: `sell_amount - buy_amount`
- Комиссии: пропорционально `used_qty / total_qty`
- Итог: `gross_profit - total_fees = net_profit`
- РЕПО: отдельно `get_repo_total()` → вычитается из net_profit

## Фильтр по датам

Даты в формате `DD.MM.YYYY`. Для SQL: `substr(trade_date,7,4)||substr(trade_date,4,2)||substr(trade_date,1,2)`.

## Температуры

- `temp/` — временные отладочные скрипты. Не удалять без подтверждения.

## Типичные ошибки (избегать!)

1. **Не использовать `_fetch_quik_trades()`** — эта функция удалена, QUIK уже в `trade`
2. **Не добавлять начальные позиции из портфеля** — они не трейд-операции
3. **Не усреднять цены** — каждый лот индивидуально
4. **Дедуплицировать сделки** при агрегации по всем отчётам
5. **РЕПО** — отдельной строкой в итоговый результат
6. **При `broker='all'` Сбер+ВТБ должны равняться Оба** — иначе баг в матчинге
