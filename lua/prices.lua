-- filepath: lua/prices.lua
-- Скрипт для QUIK: отправляет твои сделки (OnTrade) и цены в BrokerReport API
-- Запуск в QUIK: указать полный путь к файлу через меню "Сервис → Lua скрипты"

-- Автоопределение пути к lua_modules относительно папки скрипта
local script_dir = debug.getinfo(1, "S").source
if script_dir:sub(1, 1) == "@" then
    script_dir = script_dir:sub(2)
end
script_dir = script_dir:gsub("\\[^\\]+$", "")  -- убираем имя файла, оставляем папку
-- Поднимаемся на уровень выше (из lua/ в корень проекта)
local project_root = script_dir:gsub("\\[^\\]+$", "")
local modules_path = project_root .. "\\lua_modules"
local lib_path  = modules_path .. "\\lib\\lua\\5.3"
local share_path = modules_path .. "\\share\\lua\\5.3"

package.path  = share_path .. "\\?.lua;" .. share_path .. "\\?\\init.lua;" .. package.path
package.cpath = lib_path .. "\\?.dll;" .. lib_path .. "\\?\\core.dll;" .. package.cpath

local http = require("socket.http")
local ltn12 = require("ltn12")
local json = require("cjson")
local socket = require("socket")

-- ==================== НАСТРОЙКИ ====================
local API_BASE = "http://127.0.0.1:5000"
local API_URL = API_BASE .. "/api/price"
local API_TRADES = API_BASE .. "/api/trade"
local API_INSTRUMENTS = API_BASE .. "/api/instruments"
local SEND_INTERVAL = 1        -- секунд между отправками цен (пачками)
local SEND_TRADES_INTERVAL = 3 -- секунд между отправками сделок
local REFRESH_INSTRUMENTS = 300 -- секунд между обновлением списка инструментов (5 мин)
local MAX_TRADE_BATCH = 100    -- макс сделок за одну отправку
local BROKER_NAME = "sber"    -- название брокера для тега сделок: "sber" или "vtb"
local LOG_FILE = os.getenv("TEMP") and (os.getenv("TEMP") .. "\\brokerreport_prices.log") or nil

-- Список инструментов для отслеживания (sec_code, class_code).
-- Загружается с API /api/instruments при старте и периодически обновляется.
-- Если API недоступен, можно задать вручную:
local INSTRUMENTS = {}

-- Фильтр по class_code, если INSTRUMENTS пуст после загрузки
-- (например, только "TQBR" для акций, "TQOB" для облигаций)
local FILTER_CLASS_CODES = {"TQBR", "TQOB", "TQTD", "TQBS"}
-- ===================================================

local price_cache = {}   -- sec_code => { price, qty, value, class_code, time }
local trade_cache = {}   -- array of trade objects for batch send
local last_send_time = 0
local last_send_trades = 0
local all_trades_count = 0

-- Логирование
local function log_msg(level, msg)
    local line = string.format("%s [%s] %s", os.date("%Y-%m-%d %H:%M:%S"), level, tostring(msg))
    print(line)
    if LOG_FILE then
        pcall(function()
            local f, err = io.open(LOG_FILE, "a")
            if f then
                f:write(line .. "\n")
                f:close()
            end
        end)
    end
end

local function log_info(msg)  log_msg("INFO", msg)  end
local function log_error(msg) log_msg("ERROR", msg) end

-- Загрузка списка моих инструментов с API
local function fetch_instruments()
    local resp = {}
    local ok, res, code = pcall(function()
        return http.request{
            url = API_INSTRUMENTS,
            method = "GET",
            headers = { ["Accept"] = "application/json" },
            sink = ltn12.sink.table(resp)
        }
    end)

    if not ok then
        log_error("fetch_instruments HTTP error: " .. tostring(res))
        return false
    end

    local ncode = tonumber(code)
    if not ncode or ncode >= 400 then
        log_error("fetch_instruments returned code " .. tostring(code))
        return false
    end

    local body = table.concat(resp)
    local okj, data = pcall(function() return json.decode(body) end)
    if not okj or type(data) ~= "table" then
        log_error("fetch_instruments: invalid JSON response")
        return false
    end

    -- Очищаем и заполняем список инструментов
    INSTRUMENTS = {}
    for _, item in ipairs(data) do
        local sec_code = item.sec_code
        if sec_code and #sec_code > 0 then
            table.insert(INSTRUMENTS, sec_code)
        end
    end

    log_info(string.format("Loaded %d instruments from API: %s",
        #INSTRUMENTS, table.concat(INSTRUMENTS, ", ")))
    return true
end

-- Отправка параметров инструментов (lotsize, min_step) из QUIK в БД
local function send_instrument_params()
    if #INSTRUMENTS == 0 then
        log_info("send_instrument_params: no instruments to process")
        return
    end

    local batch = {}
    for _, sec_code in ipairs(INSTRUMENTS) do
        local found = false
        for _, cc in ipairs(FILTER_CLASS_CODES) do
            local ok_si, si = pcall(getSecurityInfo, cc, sec_code)
            if ok_si and si and si.lot_size then
                local lotsize = math.max(tonumber(si.lot_size) or 1, 1)
                local min_step = tonumber(si.min_price_step) or 0.01
                if min_step <= 0 then min_step = 0.01 end
                table.insert(batch, {
                    sec_code = sec_code,
                    class_code = cc,
                    lotsize = lotsize,
                    min_step = min_step,
                })
                found = true
                break
            end
        end
    end

    if #batch == 0 then
        log_info("send_instrument_params: no params fetched")
        return
    end

    local body = json.encode({ instruments = batch })
    local resp = {}
    local ok, res, code = pcall(function()
        return http.request{
            url = API_INSTRUMENTS,
            method = "POST",
            headers = {
                ["Content-Type"] = "application/json",
                ["Content-Length"] = tostring(#body)
            },
            source = ltn12.source.string(body),
            sink = ltn12.sink.table(resp)
        }
    end)
    if not ok then
        log_error("send_instrument_params HTTP error: " .. tostring(res))
    else
        local resp_body = table.concat(resp)
        log_info(string.format("Sent %d instrument params, response: %s", #batch, resp_body))
    end
end

-- Отправка пачки цен на API
local function send_prices()
    if not next(price_cache) then
        -- Нет данных для отправки — не логируем каждый раз, только при пустом кеше
        return
    end

    log_info(string.format("send_prices: preparing %d items", tonumber(#price_cache) or 0))

    local prices = {}
    for sec_code, info in pairs(price_cache) do
        table.insert(prices, {
            sec_code = sec_code,
            class_code = info.class_code or "",
            price = info.price,
            qty = info.qty or 0,
            value = info.value or 0
        })
    end

    local body = json.encode({ prices = prices })
    local resp = {}

    local ok, res, code = pcall(function()
        return http.request{
            url = API_URL,
            method = "POST",
            headers = {
                ["Content-Type"] = "application/json",
                ["Content-Length"] = tostring(#body)
            },
            source = ltn12.source.string(body),
            sink = ltn12.sink.table(resp)
        }
    end)

    if not ok then
        log_error("HTTP error: " .. tostring(res))
        return
    end

    local ncode = tonumber(code)
    if ncode and ncode >= 400 then
        log_error("API returned code " .. tostring(code) .. ": " .. table.concat(resp))
    else
        log_info(string.format("Sent %d prices, response: %s", #prices, table.concat(resp)))
        -- Очищаем кеш после успешной отправки (оставляем только неподтверждённые)
        price_cache = {}
    end
end

-- Отправка пачки сделок на API
local function send_trades()
    local n = #trade_cache
    if n == 0 then
        return
    end

    -- Берём максимум MAX_TRADE_BATCH сделок за раз (срезом, без table.remove)
    local batch_size = math.min(MAX_TRADE_BATCH, n)
    local batch = {}
    for i = 1, batch_size do
        batch[i] = trade_cache[i]
    end
    -- Сдвигаем оставшиеся элементы (создаём новый массив вместо удаления по одному)
    if n > batch_size then
        local rest = {}
        for i = batch_size + 1, n do
            table.insert(rest, trade_cache[i])
        end
        trade_cache = rest
    else
        trade_cache = {}
    end

    local body = json.encode({ trades = batch })
    local resp = {}

    local ok, res, code = pcall(function()
        return http.request{
            url = API_TRADES,
            method = "POST",
            headers = {
                ["Content-Type"] = "application/json",
                ["Content-Length"] = tostring(#body)
            },
            source = ltn12.source.string(body),
            sink = ltn12.sink.table(resp)
        }
    end)

    if not ok then
        log_error("send_trades HTTP error: " .. tostring(res))
        -- при ошибке сети возвращаем только в начало, но не больше 2000 всего
        if #trade_cache + batch_size <= 2000 then
            for _, t in ipairs(batch) do
                table.insert(trade_cache, 1, t)
            end
        else
            log_info("send_trades: dropping batch, queue too large")
        end
        -- Обнуляем batch для GC
        batch = nil
        return
    end

    local ncode = tonumber(code)
    if ncode and ncode >= 400 then
        log_error("send_trades API returned code " .. tostring(code) .. ": " .. table.concat(resp))
        -- возврат в очередь при ошибке, но не больше 2000
        if #trade_cache + batch_size <= 2000 then
            for _, t in ipairs(batch) do
                table.insert(trade_cache, 1, t)
            end
        else
            log_info("send_trades: dropping batch on HTTP error, queue too large")
        end
    else
        log_info(string.format("Sent %d trades, queue left: %d", batch_size, #trade_cache))
    end
    batch = nil
end

-- Проверка, нужно ли отслеживать инструмент
local should_track_count = 0
local should_track_skip = 0
local function should_track(sec_code, class_code)
    if #INSTRUMENTS > 0 then
        -- Отслеживаем только инструменты из списка (по sec_code)
        for _, sc in ipairs(INSTRUMENTS) do
            if sc == sec_code then
                return true
            end
        end
        should_track_skip = should_track_skip + 1
        if should_track_skip <= 5 then
            log_info(string.format("should_track: SKIP %s/%s (not in my instruments)", sec_code, class_code))
        end
        return false
    end

    -- Если список пуст — фильтруем по class_code
    if #FILTER_CLASS_CODES > 0 then
        for _, cc in ipairs(FILTER_CLASS_CODES) do
            if cc == class_code then
                return true
            end
        end
        should_track_skip = should_track_skip + 1
        if should_track_skip <= 5 then
            log_info(string.format("should_track: SKIP %s/%s (class filter)", sec_code, class_code))
        end
        return false
    end

    return true
end

-- Нормализация datetime из QUIK
local function normalize_trade_datetime(dt)
    if not dt then return nil, nil end
    if type(dt) == 'table' then
        if dt.date and dt.time then
            local d = {}
            for num in string.gmatch(dt.date, "%d+") do table.insert(d, tonumber(num)) end
            local t = {}
            for num in string.gmatch(dt.time, "%d+") do table.insert(t, tonumber(num)) end
            local res = { year = 0, month = 0, day = 0, hour = 0, min = 0, sec = 0 }
            if #d == 3 then res.day = d[1] or 0; res.month = d[2] or 0; res.year = d[3] or 0 end
            if #t >= 2 then res.hour = t[1] or 0; res.min = t[2] or 0; res.sec = t[3] or 0 end
            return res
        end
        return {
            year = tonumber(dt.year) or 0,
            month = tonumber(dt.month) or 0,
            day = tonumber(dt.day) or 0,
            hour = tonumber(dt.hour) or 0,
            min = tonumber(dt.min) or 0,
            sec = tonumber(dt.sec) or 0
        }
    end
    local s = tostring(dt)
    local d_str, t_str = string.match(s, "^(%d%d%.%d%d%.%d%d%d%d)%s*(%d%d:%d%d:%d%d)$")
    if d_str then
        local dd, mm, yy = string.match(d_str, "(%d%d)%.(%d%d)%.(%d%d%d%d)")
        local hh, mi, ss = string.match(t_str, "(%d%d):(%d%d):(%d%d)")
        return {
            year = tonumber(yy) or 0, month = tonumber(mm) or 0, day = tonumber(dd) or 0,
            hour = tonumber(hh) or 0, min = tonumber(mi) or 0, sec = tonumber(ss) or 0
        }
    end
    return nil, nil
end

-- Callback QUIK: обезличенные сделки (для отслеживания цен моих инструментов)
function OnAllTrade(alltrade)
    local sec_code = alltrade.sec_code
    local class_code = alltrade.class_code or ""

    -- Фильтруем только мои инструменты
    if #INSTRUMENTS > 0 and not should_track(sec_code, class_code) then
        return
    end

    -- Обновляем цену (только последнюю, в trade_cache НЕ добавляем)
    price_cache[sec_code] = {
        price = tonumber(alltrade.price) or 0,
        qty = tonumber(alltrade.qty) or 0,
        value = tonumber(alltrade.value) or 0,
        class_code = class_code,
        time = os.time()
    }
end

-- Callback QUIK: твои собственные сделки (покупка/продажа)
function OnTrade(trade)
    all_trades_count = all_trades_count + 1

    -- OnTrade использует seccode (не sec_code как в OnAllTrade)
    local sec_code = trade.seccode or trade.sec_code or ""
    local class_code = trade.class_code or ""

    should_track_count = should_track_count + 1

    -- Сохраняем цену (только последнюю)
    local price = tonumber(trade.price) or 0
    local qty = tonumber(trade.qty or trade.quantity) or 0
    local value = tonumber(trade.value) or 0
    price_cache[sec_code] = {
        price = price,
        qty = qty,
        value = value,
        class_code = class_code,
        time = os.time()
    }

    -- Определяем сторону сделки:
    -- Приоритет 1: trade.operation ("B"/"S") — надёжный признак из QUIK.
    -- Приоритет 2: flags — бит 0 (0x01) установлен = покупка (в Lua 5.1 нет &
    --              используем % 2: нечётное = бит 0 установлен).
    -- ВАЖНО: QUIK вызывает OnTrade для одной сделки несколько раз, и в первый
    -- раз флаги могут не содержать бит стороны (flags=32/64) → неверная сторона.
    -- На сервере при конфликте по trade_num сторона перезаписывается последней,
    -- поэтому здесь важно передавать trade.operation, если он доступен.
    local raw_flags = tonumber(trade.flags) or 0
    local trade_operation = tostring(trade.operation or "")
    local side
    if trade_operation == "B" then
        side = "buy"
    elseif trade_operation == "S" then
        side = "sell"
    else
        side = "sell"
        if raw_flags > 0 and raw_flags % 2 == 1 then
            side = "buy"
        end
    end

    -- Диагностика: если flags=0, дамп всех полей для анализа
    if raw_flags == 0 then
        local fields = {}
        for k, v in pairs(trade) do
            table.insert(fields, string.format("%s=%s", tostring(k), tostring(v)))
        end
        log_info("FLAGS=0 TRADE: " .. table.concat(fields, ", "))
    end

    -- Логируем каждую сделку (для отладки), можно закомментировать при высокой частоте
    local side_label = (side == "buy") and "BUY" or "SELL"
    if should_track_count <= 10 or should_track_count % 10 == 0 then
        log_info(string.format("MY TRADE #%d (%d) %s: %s/%s price=%.4f qty=%d value=%.2f flags=%d account=[%s] broker=%s",
            tonumber(trade.trade_num) or 0, should_track_count, side_label,
            sec_code, class_code,
            price, qty, value, tonumber(trade.flags) or -1,
            tostring(trade.account or ""), BROKER_NAME))
    end

    -- Размер лота из QUIK
    local lotsize = 1
    local lp = getParamEx(class_code, sec_code, "LOTSIZE")
    if lp and lp.param_value then
        lotsize = tonumber(lp.param_value) or 1
    end

    -- Сохраняем сделку для отправки в БД
    local dt = normalize_trade_datetime(trade.datetime)
    -- Определяем счёт (account): в QUIK OnTrade это поле trade.account
    local account = tostring(trade.account or "")
    local settlecode = tostring(trade.settlecode or "")
    local raw_flags = tonumber(trade.flags) or 0
    local raw_op_type = tonumber(trade.operation_type) or -1
    -- brokerref — поле "Комментарий" в терминале QUIK, содержит код клиента
    local brokerref = tostring(trade.brokerref or "")
    local client_code = tostring(trade.client_code or "")
    table.insert(trade_cache, {
        trade_num = tonumber(trade.trade_num) or 0,
        sec_code = sec_code,
        class_code = class_code,
        price = price,
        qty = qty,
        value = value,
        accruedint = tonumber(trade.accruedint) or 0,
        yield = tonumber(trade.yield) or 0,
        settlecode = settlecode,
        account = account,        -- номер счёта для определения брокера
        reporate = tonumber(trade.reporate) or 0,
        repovalue = tonumber(trade.repovalue) or 0,
        repo2value = tonumber(trade.repo2value) or 0,
        repoterm = tonumber(trade.repoterm) or 0,
        period = tonumber(trade.period) or 0,
        datetime = dt,
        side = side,
        lotsize = lotsize,
        broker = BROKER_NAME,      -- fallback: если CLIENT_CODE_MAP не заполнен
        flags = raw_flags,         -- оригинальные флаги для перепроверки на сервере
        operation = trade_operation, -- "B"/"S" из QUIK (если доступно)
        operation_type = raw_op_type,
        brokerref = brokerref,     -- "Комментарий" в терминале QUIK (код клиента)
        client_code = client_code, -- код клиента (если есть)
    })

    -- Если очередь сделок слишком большая — сбрасываем старые (срезом, а не циклом)
    if #trade_cache > 2000 then
        local keep = 1000
        local new = {}
        for i = #trade_cache - keep + 1, #trade_cache do
            table.insert(new, trade_cache[i])
        end
        trade_cache = new
        log_info("Trade cache trimmed to " .. keep)
    end
end

-- Загрузка истории всех моих сделок из QUIK при старте
local function load_existing_trades()
    -- Проверяем, доступен ли QUIK API (getNumberOf/getItem)
    local ok, n = pcall(function()
        return getNumberOf("trades")
    end)
    if not ok or type(n) ~= "number" or n <= 0 then
        log_info("load_existing_trades: QUIK trades table empty or unavailable (" .. tostring(n) .. ")")
        return false
    end

    log_info("load_existing_trades: found " .. n .. " trades in QUIK, loading...")
    local loaded = 0
    local debug_done = false
    for i = 0, n - 1 do
        local ok2, trade = pcall(function() return getItem("trades", i) end)
        if ok2 and trade and type(trade) == "table" then
            -- Отладка: выводим ВСЕ поля первой сделки
            if not debug_done then
                local fields = {}
                for k, v in pairs(trade) do
                    table.insert(fields, string.format("%s=%s", tostring(k), tostring(v)))
                end
                log_info("TRADE FIELDS: " .. table.concat(fields, ", "))
                debug_done = true
            end

            local sec_code = trade.seccode or trade.sec_code or ""
            local class_code = trade.class_code or ""

            -- Пропускаем не наши инструменты если список загружен
            if #INSTRUMENTS > 0 and not should_track(sec_code, class_code) then
                -- не логируем каждый пропуск, только счётчик
            else
                -- Определяем сторону сделки:
                -- Приоритет: trade.operation ("B"/"S"), затем flags (бит 0 = покупка)
                local trade_raw_flags = tonumber(trade.flags) or 0
                local trade_operation = tostring(trade.operation or "")
                local trade_side
                if trade_operation == "B" then
                    trade_side = "buy"
                elseif trade_operation == "S" then
                    trade_side = "sell"
                else
                    trade_side = "sell"
                    if trade_raw_flags > 0 and trade_raw_flags % 2 == 1 then
                        trade_side = "buy"
                    end
                end

                -- Размер лота из QUIK
                local trade_lotsize = 1
                local lp = getParamEx(class_code, sec_code, "LOTSIZE")
                if lp and lp.param_value then
                    trade_lotsize = tonumber(lp.param_value) or 1
                end

                local dt = normalize_trade_datetime(trade.datetime)
                local account = tostring(trade.account or "")
                local raw_flags = trade_raw_flags
                -- brokerref — поле "Комментарий" в терминале QUIK, содержит код клиента
                local brokerref = tostring(trade.brokerref or "")
                local client_code = tostring(trade.client_code or "")
                table.insert(trade_cache, {
                    trade_num = tonumber(trade.trade_num) or 0,
                    sec_code = sec_code,
                    class_code = class_code,
                    price = tonumber(trade.price) or 0,
                    qty = tonumber(trade.qty) or 0,
                    value = tonumber(trade.value) or 0,
                    accruedint = tonumber(trade.accruedint) or 0,
                    yield = tonumber(trade.yield) or 0,
                    settlecode = tostring(trade.settlecode or ""),
                    account = account,
                    reporate = tonumber(trade.reporate) or 0,
                    repovalue = tonumber(trade.repovalue) or 0,
                    repo2value = tonumber(trade.repo2value) or 0,
                    repoterm = tonumber(trade.repoterm) or 0,
                    period = tonumber(trade.period) or 0,
                    datetime = dt,
                    side = trade_side,
                    lotsize = trade_lotsize,
                    broker = BROKER_NAME,
                    flags = raw_flags,
                    operation = trade_operation,
                    brokerref = brokerref,
                    client_code = client_code,
                })
                loaded = loaded + 1

                -- Отправляем пачками по ходу загрузки, чтобы не забивать память
                if #trade_cache >= MAX_TRADE_BATCH then
                    pcall(send_trades)
                end
            end
        end
    end

    log_info(string.format("load_existing_trades: loaded %d trades into queue (%d total in QUIK)",
        loaded, n))
    return true
end

-- Главный цикл
function main()
    log_info("BrokerReport Price Sender started")
    log_info("API: " .. API_URL)
    log_info("Instruments API: " .. API_INSTRUMENTS)
    log_info("Send interval: " .. SEND_INTERVAL .. "s")
    log_info("Send trades interval: " .. SEND_TRADES_INTERVAL .. "s")
    log_info("Refresh instruments: every " .. REFRESH_INSTRUMENTS .. "s")
    log_info("Log file: " .. tostring(LOG_FILE))
    log_info("Lua version: " .. tostring(_VERSION))

    -- Загружаем список инструментов с API при старте
    pcall(fetch_instruments)
    pcall(send_instrument_params)
    if #INSTRUMENTS == 0 then
        log_info("No instruments from API, fallback to class filter: "
            .. table.concat(FILTER_CLASS_CODES, ", "))
    else
        log_info("Tracking " .. #INSTRUMENTS .. " instruments from API")
    end

    -- Загружаем историю сделок из QUIK
    pcall(load_existing_trades)

    local last_log_time = 0
    local last_heartbeat = 0
    local last_refresh = os.time()

    log_info("main() loop started")

    while true do
        local now = os.time()

        -- Периодическое обновление списка инструментов
        if now - last_refresh >= REFRESH_INSTRUMENTS then
            pcall(fetch_instruments)
            pcall(send_instrument_params)
            last_refresh = now
            if #INSTRUMENTS > 0 then
                log_info("Instruments refreshed: " .. #INSTRUMENTS .. " items")
            end
        end

        -- Отправка пачки цен по таймеру
        if now - last_send_time >= SEND_INTERVAL then
            pcall(send_prices)
            last_send_time = now
        end

        -- Отправка пачки сделок по таймеру (реже, чем цены)
        if now - last_send_trades >= SEND_TRADES_INTERVAL then
            pcall(send_trades)
            last_send_trades = now
        end

        -- Очистка устаревших цен (старше 1 часа)
        if now % 60 == 0 then
            local stale_cutoff = now - 3600
            for k, v in pairs(price_cache) do
                if v.time and v.time < stale_cutoff then
                    price_cache[k] = nil
                end
            end
        end

        -- Heartbeat раз в 10 секунд (показывает что main() жив)
        if now - last_heartbeat >= 10 then
            local mem_kb = collectgarbage("count")
            local pc_count = 0; for _ in pairs(price_cache) do pc_count = pc_count + 1 end
    log_info(string.format("HEARTBEAT: all_trades=%d tracked=%d price_cache=%d trade_queue=%d instruments=%d mem=%.0fKB",
                all_trades_count, should_track_count, pc_count, #trade_cache, #INSTRUMENTS, mem_kb))
            last_heartbeat = now
        end

        -- Лог статистики раз в 60 секунд + принудительный GC
        if now - last_log_time >= 60 then
            collectgarbage("collect")
            local mem_kb = collectgarbage("count")
            local pc_count = 0; for _ in pairs(price_cache) do pc_count = pc_count + 1 end
    log_info(string.format("Stats: all_trades=%d, cached_prices=%d, trade_queue=%d, tracked=%d, mem=%.0fKB",
                all_trades_count, pc_count, #trade_cache, #INSTRUMENTS, mem_kb))
            last_log_time = now
        end

        socket.sleep(0.2)
    end
end

log_info("Script loaded. Waiting for QUIK callbacks (OnAllTrade + OnTrade)...")
