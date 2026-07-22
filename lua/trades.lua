-- filepath: lua/trades.lua
-- Указываем пути для библиотек Lua (локальные модули из lua_modules/)
local script_dir = debug.getinfo(1, "S").source
if script_dir:sub(1, 1) == "@" then
    script_dir = script_dir:sub(2)
end
script_dir = script_dir:gsub("\\[^\\]+$", "")
local project_root = script_dir:gsub("\\[^\\]+$", "")
local modules_path = project_root .. "\\lua_modules"
local lib_path  = modules_path .. "\\lib\\lua\\5.3"
local share_path = modules_path .. "\\share\\lua\\5.3"

package.path  = share_path .. "\\?.lua;" .. share_path .. "\\?\\init.lua;" .. package.path
package.cpath = lib_path .. "\\?.dll;" .. lib_path .. "\\?\\core.dll;" .. package.cpath

-- Подключаем необходимые модули
local http = require("socket.http")
local ltn12 = require("ltn12")
local json = require("cjson") -- Используется для кодирования данных в JSON
local amqp = require "amqp"
local mime = require("mime") -- для base64 кодирования
local socket = require("socket")

-- Безопасная обёртка для setdefaulttimeout: если функция недоступна, возвращаем nil
local function safe_settimeout(t)
    if not socket then return nil end
    local fn = socket.setdefaulttimeout
    if type(fn) ~= 'function' then return nil end
    local ok, prev = pcall(fn, t)
    if ok then return prev end
    return nil
end

-- Простое логирование ошибок в файл (определено рано, чтобы можно было логировать во время инициализации)
local log_file = "C:\\Users\\Roman\\YandexDisk\\test\\trades_errors.log"
local function log_write(level, msg)
    pcall(function()
        local f, ferr = io.open(log_file, "a")
        if not f then return end
        f:write(string.format("%s [%s] %s\n", os.date("%Y-%m-%d %H:%M:%S"), level, tostring(msg)))
        f:close()
    end)
end
local function log_error(msg) log_write("ERROR", msg) end
local function log_info(msg) log_write("INFO", msg) end

local protoc = require "protoc"
local pb = require "pb"

assert(protoc:load [[
   syntax = "proto3";

message DateTime {
    int32 year = 1;
    int32 month = 2;
    int32 day = 3;
    int32 hour = 4;
    int32 min = 5;
    int32 sec = 6;
    string raw = 7;
}

message TradeData {
    int64 trade_num = 1;
    int32 flags = 2;
    double price = 3;
    int32 qty = 4;
    double value = 5;
    double accruedint = 6;
    double yield = 7;
    string settlecode = 8;
    double reporate = 9;
    double repovalue = 10;
    double repo2value = 11;
    int32 repoterm = 12;
    string sec_code = 13;
    string class_code = 14;
    DateTime datetime = 15;
    int32 period = 16;
} ]])

local proto_text = [[
syntax = "proto3";

package trades;

message DateTime {
  int32 year = 1;
  int32 month = 2;
  int32 day = 3;
  int32 hour = 4;
  int32 min = 5;
  int32 sec = 6;
  string raw = 7;
}

message TradeData {
  int64 trade_num = 1;
  int32 flags = 2;
  double price = 3;
  int32 qty = 4;
  double value = 5;
  double accruedint = 6;
  double yield_val = 7;    
  string settlecode = 8;
  double repo_rate = 9;    
  double repovalue = 10;
  double repo2value = 11;
  int32 repoterm = 12;
  string sec_code = 13;
  string class_code = 14;
  DateTime datetime = 15;
  int32 period = 16;
}
]]

-- ==================== Configuration (set values here) ====================
-- Enable or disable self-test at script startup
local SELF_TEST_RUN_ON_START = true

-- Schema Registry URL
-- Schema Registry is running on the remote Kafka host; use that address
local schema_registry_url = "http://192.168.31.165:8081"

-- Kafka REST Proxy (if used)
-- Kafka REST Proxy lives on the remote Kafka host (if present)
local kafka_rest_host = "192.168.31.165"
local kafka_rest_port = 8082

-- (JSON mode removed — this deployment uses protobuf binary only)

-- Binary queue and bridge settings
local queue_max_per_tick = 50
local bridge_available = false
local BRIDGE_HEALTH_INTERVAL = 5
local BRIDGE_RETRY_SECS = 5

-- Metricing: sample queue lengths every main loop iteration and send avg every METRIC_INTERVAL secs
local METRIC_INTERVAL = 10
local metric_last_time = os.time()
local metric_sample_count = 0
local metric_amqp_acc = 0
local metric_kafka_acc = 0
local metric_trades_count = 0
local metric_amqp_min = nil
local metric_amqp_max = nil
local metric_kafka_min = nil
local metric_kafka_max = nil

-- Toggle verbose logging inside process_queues (set false to silence preview/schema_id logs)
local LOG_PROCESS_QUEUES = false

-- Queue limits
local MAX_AMQP_QUEUE = 2000
local MAX_KAFKA_BIN_QUEUE = 20000
local MAX_KAFKA_BATCH = 20000

-- Bridge host/port and control/outbox locations
local bridge_host = "127.0.0.1"
local bridge_port = 18080
local tcp_bridge_port = 19090
local tcp_sock = nil
local tcp_connected = false
local function tcp_connect()
    if tcp_connected and tcp_sock then return true end
    local ok, sock = pcall(function()
    local s = socket.tcp()
    -- short blocking timeout for connect, then we'll use select for sends
    s:settimeout(1)
    s:connect(bridge_host, tcp_bridge_port)
    -- leave socket in non-blocking mode; send_all will use select to wait when needed
    s:settimeout(0)
    return s
    end)
    if ok and sock then
        tcp_sock = sock
        tcp_connected = true
        log_info("tcp_connect: connected to " .. bridge_host .. ":" .. tostring(tcp_bridge_port))
        return true
    end
    tcp_sock = nil
    tcp_connected = false
    return false
end

local function tcp_send_frame(topic, payload_bytes)
    if not tcp_connect() then return false end
    local topic_b = tostring(topic)
    local tlen = #topic_b
    local payload = payload_bytes or ""
    local N = 1 + tlen + #payload
    -- 4-byte BE length
    local function pack_u32(n)
        local b1 = math.floor(n / 16777216) % 256
        local b2 = math.floor(n / 65536) % 256
        local b3 = math.floor(n / 256) % 256
        local b4 = n % 256
        return string.char(b1, b2, b3, b4)
    end
    local frame = pack_u32(N) .. string.char(tlen) .. topic_b .. payload

    -- Helper: send full buffer, handling partial writes and timeouts using select
    local function send_all(sock, data)
        local total = #data
        local sent_pos = 1
        while sent_pos <= total do
            local sent, err, last = sock:send(data, sent_pos)
            if sent and sent > 0 then
                sent_pos = sent_pos + sent
            else
                -- sent is nil on timeout or error; err contains reason
                if err == 'timeout' then
                    -- wait until socket is writable (up to short timeout)
                    local _, writable = socket.select(nil, {sock}, 0.2)
                    if not writable or #writable == 0 then
                        -- still not writable, retry a few times then fail
                        -- continue loop to attempt send again
                    end
                else
                    return false, err
                end
            end
        end
        return true
    end

    local ok_send, send_err = send_all(tcp_sock, frame)
    if not ok_send then
        log_error("tcp_send_frame send failed: " .. tostring(send_err))
        tcp_connected = false
        pcall(function() tcp_sock:close() end)
        tcp_sock = nil
        return false
    end
    return true
end
local BRIDGE_CONTROL_FILE = "C:\\Users\\Roman\\YandexDisk\\test\\bridge_on"
local OUTBOX_DIR = "C:\\Users\\Roman\\YandexDisk\\test\\outbox"
local OUTBOX_READY = false
-- ============================================================================

-- { changed code }
local function register_protobuf_schema(subject)
    local payload = { schemaType = "PROTOBUF", schema = proto_text }
    local body = json.encode(payload)
    local url = string.format("%s/subjects/%s/versions", schema_registry_url, subject)

    local resp_tbl = {}
    local prev_to = safe_settimeout(5)
    local ok, http_res, code = pcall(function()
        return http.request{
            url = url,
            method = "POST",
            headers = {
                ["Content-Type"] = "application/json",
                ["Content-Length"] = tostring(#body)
            },
            source = ltn12.source.string(body),
            sink = ltn12.sink.table(resp_tbl)
        }
    end)
    if prev_to ~= nil then safe_settimeout(prev_to) end

    if not ok then
        log_error("SR register http error: " .. tostring(http_res))
        return nil, tostring(http_res)
    end

    local resp_body = table.concat(resp_tbl)
    local ncode = tonumber(code)
    if ncode and ncode >= 400 then
        log_info("SR register returned code " .. tostring(code) .. " body=" .. tostring(resp_body))
        -- try GET latest
        local got, body2 = pcall(function()
            local r = {}
            http.request{ url = string.format("%s/subjects/%s/versions/latest", schema_registry_url, subject), method = "GET", sink = ltn12.sink.table(r) }
            return table.concat(r)
        end)
        if got and body2 and #body2 > 0 then
            local okj, j = pcall(function() return json.decode(body2) end)
            if okj and j and j.id then return j.id, nil end
        end
        return nil, "register_failed"
    end

    local okj, j = pcall(function() return json.decode(resp_body) end)
    if okj and j and j.id then return j.id, nil end
    return nil, "parse_failed"
end

local function get_or_register_schema_id(topic, record_name)
    local subject = topic .. "-" .. record_name
    local response = {}
    local url = string.format("%s/subjects/%s/versions/latest", schema_registry_url, subject)
    local prev_to = safe_settimeout(3)
    local ok, res, code = pcall(function()
        return http.request{ url = url, method = "GET", sink = ltn12.sink.table(response) }
    end)
    if prev_to ~= nil then safe_settimeout(prev_to) end
    if ok and response[1] then
        local j = json.decode(table.concat(response))
        if j and j.id then return j.id end
    end
    local id, err = register_protobuf_schema(subject)
    return id, err
end

local function wrap_with_sr(payload_bytes, schema_id)
    local id = tonumber(schema_id)
    if not id then return nil, "invalid schema id" end
    local function int32_be(n)
        local b1 = math.floor(n / 16777216) % 256
        local b2 = math.floor(n / 65536) % 256
        local b3 = math.floor(n / 256) % 256
        local b4 = n % 256
        return string.char(b1, b2, b3, b4)
    end
    return string.char(0) .. int32_be(id) .. payload_bytes
end

-- Use TopicNameStrategy subject: <topic>-value so Connect with TopicNameStrategy can find it
local schema_id, schema_err = get_or_register_schema_id("trades", "value")
if not schema_id then
    log_error("Cannot obtain schema id for subject 'trades-value': "..tostring(schema_err))
end

-- Helper: produce short hex preview of first N bytes (used in logs)
local function bytes_preview_hex(s, n)
    if not s then return "" end
    n = n or 8
    local parts = {}
    for i = 1, math.min(n, #s) do
        parts[#parts+1] = string.format("%02X", string.byte(s, i))
    end
    return table.concat(parts, " ")
end

-- Initialize and cache schema id once to avoid repeated SR calls
local schema_cache_initialized = false
local function init_schema_cache()
    if schema_cache_initialized then return true end
    local ok, id_or_err = pcall(function()
        return get_or_register_schema_id("trades", "value")
    end)
    local id = nil
    if ok then id = id_or_err else id = nil end
    if id then
        schema_id = id
        schema_cache_initialized = true
        log_info("init_schema_cache: schema_id=" .. tostring(schema_id))
        return true
    else
        log_error("init_schema_cache: failed to obtain schema id: " .. tostring(id_or_err))
        return false, id_or_err
    end
end

-- Отложенная инициализация AMQP/producer, чтобы не блокировать загрузку скрипта
local ctx = nil
local amqp_ready = false

local function init_amqp()
    if amqp_ready or ctx then return end
    local ok, err
    ok, ctx = pcall(function()
        return amqp.new({role = "publisher", exchange = "amq.topic", ssl = false, user = "guest", password = "guest"})
    end)
    if not ok or not ctx then
        ctx = nil
        amqp_ready = false
    log_error("init_amqp failed: " .. tostring(err))
    return false, tostring(err)
    end

    ok, err = pcall(function() ctx:connect("192.168.31.165",5672) end)
    if not ok then
        -- не смогли подключиться сейчас; оставим ctx и попытаемся позже
        amqp_ready = false
    log_error("init_amqp connect failed: " .. tostring(err))
    return false, tostring(err)
    end

    ok, err = pcall(function() ctx:setup() end)
    if not ok then
        amqp_ready = false
    log_error("init_amqp setup failed: " .. tostring(err))
    return false, tostring(err)
    end

    amqp_ready = true
    return true
end


-- Очереди для фоновой отправки, чтобы не блокировать QUIK
local amqp_queue = {}
local kafka_bin_queue = {}
-- Increase per-iteration work so bridge can keep up with producer rates.
-- Set to 2000 and run main loop every 0.1s -> up to ~20000 items/sec (2000 * 10).
-- Reduced per-iteration work to avoid long blocking in QUIK main loop.
-- If you need higher throughput, increase this carefully and ensure bridge is healthy.
local queue_max_per_tick = 200
-- Bridge availability and health-check settings
bridge_available = false
local BRIDGE_HEALTH_INTERVAL = 5
local last_bridge_check = 0
local dropped_while_bridge_down = 0
local dropped_while_amqp_full = 0
-- Защита от утечек: ограничиваем размеры очередей и вводим cooldown при недоступности bridge
local MAX_AMQP_QUEUE = 2000
local MAX_KAFKA_BIN_QUEUE = 20000
local MAX_KAFKA_BATCH = 20000
local BRIDGE_RETRY_SECS = 5
local last_bridge_failure = 0

-- Утилита: показать/залогировать размеры очередей и состояние
function show_queues()
    local s = string.format("queues amqp=%d kafka_bin=%d amqp_ready=%s last_bridge_failure=%s dropped_amqp_full=%d",
        #amqp_queue, #kafka_bin_queue, tostring(amqp_ready), tostring(last_bridge_failure), tonumber(dropped_while_amqp_full) or 0)
    print(s)
    log_info(s)
    return s
end

-- Обратный вызов для таблицы all_trades
function OnAllTrade(alltrade)
    -- Non-blocking fast-path: build payload, encode protobuf and push to in-memory queues only.
    -- This function must not perform disk or network IO or call logging that writes to disk.
    local ok, err = pcall(function()
        local function normalize_datetime(dt)
            if not dt then return nil end
            local res = { year = 0, month = 0, day = 0, hour = 0, min = 0, sec = 0, raw = tostring(dt) }
            if type(dt) == 'table' then
                if dt.date and dt.time then
                    local d = {}
                    for num in string.gmatch(dt.date, "%d+") do table.insert(d, tonumber(num)) end
                    local t = {}
                    for num in string.gmatch(dt.time, "%d+") do table.insert(t, tonumber(num)) end
                    if #d == 3 then
                        res.day = d[1] or 0; res.month = d[2] or 0; res.year = d[3] or 0
                    end
                    if #t >= 2 then
                        res.hour = t[1] or 0; res.min = t[2] or 0; res.sec = t[3] or 0
                    end
                else
                    res.year = tonumber(dt.year) or res.year
                    res.month = tonumber(dt.month) or res.month
                    res.day = tonumber(dt.day) or res.day
                    res.hour = tonumber(dt.hour) or res.hour
                    res.min = tonumber(dt.min) or res.min
                    res.sec = tonumber(dt.sec) or res.sec
                end
                -- avoid calling json.encode (disk IO via log functions) here
                res.raw = tostring(dt)
            else
                local s = tostring(dt)
                local d, t = string.match(s, "^(%d%d%.%d%d%.%d%d%d%d)%s*(%d%d:%d%d:%d%d)$")
                if d then
                    local dd, mm, yy = string.match(d, "(%d%d)%.(%d%d)%.(%d%d%d%d)")
                    local hh, mi, ss = string.match(t, "(%d%d):(%d%d):(%d%d)")
                    res.day = tonumber(dd) or 0; res.month = tonumber(mm) or 0; res.year = tonumber(yy) or 0
                    res.hour = tonumber(hh) or 0; res.min = tonumber(mi) or 0; res.sec = tonumber(ss) or 0
                    res.raw = s
                end
            end
            return res
        end

        local trade_data = {
            trade_num = tonumber(alltrade.trade_num) or 0,
            flags = tonumber(alltrade.flags) or 0,
            price = tonumber(alltrade.price) or 0.0,
            qty = tonumber(alltrade.qty) or 0,
            value = tonumber(alltrade.value) or 0.0,
            accruedint = tonumber(alltrade.accruedint) or 0.0,
            yield = tonumber(alltrade.yield) or 0.0,
            settlecode = tostring(alltrade.settlecode or ""),
            reporate = tonumber(alltrade.reporate) or 0.0,
            repovalue = tonumber(alltrade.repovalue) or 0.0,
            repo2value = tonumber(alltrade.repo2value) or 0.0,
            repoterm = tonumber(alltrade.repoterm) or 0,
            sec_code = tostring(alltrade.sec_code or alltrade.ticker or ""),
            class_code = tostring(alltrade.class_code or ""),
            datetime = normalize_datetime(alltrade.datetime) or normalize_datetime({ date = alltrade.trade_datetime or "" }),
            period = tonumber(alltrade.period) or 0
        }

        -- encode protobuf (in-memory CPU) and enqueue; do not perform IO or network here
        local ok_encode, msg = pcall(function() return pb.encode("TradeData", trade_data) end)
        if not ok_encode or not msg then
            -- encoding failed; drop and continue (keep hot-path minimal)
            return
        end

        -- Enqueue directly to Kafka binary queue to avoid silent drops when AMQP is not ready.
        if #kafka_bin_queue < MAX_KAFKA_BIN_QUEUE then
            table.insert(kafka_bin_queue, { msg = msg, topic = "trades" })
        else
            -- Kafka queue full: drop oldest to keep memory bounded
            table.remove(kafka_bin_queue, 1)
            table.insert(kafka_bin_queue, { msg = msg, topic = "trades" })
            dropped_while_bridge_down = (dropped_while_bridge_down or 0) + 1
        end

        metric_trades_count = (metric_trades_count or 0) + 1
    end)
    if not ok then
        -- Keep this minimal: avoid calling log_error (disk IO). Print to console only.
        print("OnAllTrade internal error: " .. tostring(err))
    end
end

-- Главная функция
function main()
    local last_init = 0
    local last_dump = 0
    while true do
        local now = os.time()
        if now - last_init >= 5 then
            -- Пытаться инициализировать in background (не блокировать)
            pcall(init_amqp)
            last_init = now
        end
        -- Периодическая проверка здоровья bridge
        if now - last_bridge_check >= BRIDGE_HEALTH_INTERVAL then
            local ok = pcall(check_bridge_health)
            if ok then
                local healthy = check_bridge_health()
                if healthy then
                    if not bridge_available then
                        log_info("bridge recovered")
                        bridge_available = true
                        dropped_while_bridge_down = 0
                    end
                else
                    if bridge_available then
                        log_info("bridge appears down")
                    end
                    bridge_available = false
                end
            else
                bridge_available = false
            end
            last_bridge_check = now
        end
        -- Обработка очередей небольшими порциями, чтобы не блокировать QUIK
        local ok, err = pcall(process_queues)
        if not ok then
            -- логируем, но не прерываем основной цикл
            print("process_queues error: " .. tostring(err))
            log_error("process_queues error: " .. tostring(err))
        end
        -- Metric sampling: capture current queue sizes for averaging
    local cur_amqp = #amqp_queue
    local cur_kafka = #kafka_bin_queue
    metric_sample_count = metric_sample_count + 1
    metric_amqp_acc = metric_amqp_acc + cur_amqp
    metric_kafka_acc = metric_kafka_acc + cur_kafka
    -- update min/max
    if not metric_amqp_min or cur_amqp < metric_amqp_min then metric_amqp_min = cur_amqp end
    if not metric_amqp_max or cur_amqp > metric_amqp_max then metric_amqp_max = cur_amqp end
    if not metric_kafka_min or cur_kafka < metric_kafka_min then metric_kafka_min = cur_kafka end
    if not metric_kafka_max or cur_kafka > metric_kafka_max then metric_kafka_max = cur_kafka end

        if now - metric_last_time >= METRIC_INTERVAL then
            -- compute averages
            local avg_amqp = 0
            local avg_kafka = 0
            if metric_sample_count > 0 then
                avg_amqp = metric_amqp_acc / metric_sample_count
                avg_kafka = metric_kafka_acc / metric_sample_count
            end
            -- prepare metric payload
            local metric_payload = {
                topic = 'trades_metrics',
                records = {
                    {
                        value = json.encode({
                            count = metric_trades_count or 0,
                            duration = METRIC_INTERVAL,
                            avg_amqp_queue = avg_amqp,
                            avg_kafka_queue = avg_kafka,
                            amqp_queue_min = metric_amqp_min or 0,
                            amqp_queue_max = metric_amqp_max or 0,
                            kafka_queue_min = metric_kafka_min or 0,
                            kafka_queue_max = metric_kafka_max or 0
                        }),
                        binary = false
                    }
                }
            }
            -- print formatted metrics locally and send metrics to bridge via HTTP (non-blocking best-effort)
            pcall(function()
                -- Local formatted prints similar to bridge logs for quick visibility
                local avg_per_sec = 0.0
                if METRIC_INTERVAL > 0 then avg_per_sec = (metric_trades_count or 0) / METRIC_INTERVAL end
                log_info(string.format('Metrics received: count=%d duration=%d avg_per_sec=%.2f', metric_trades_count or 0, METRIC_INTERVAL, avg_per_sec))
                log_info(string.format('STATS %ds: amqp_avg=%.2f kafka_avg=%.2f amqp_min=%d amqp_max=%d kafka_min=%d kafka_max=%d', METRIC_INTERVAL, avg_amqp, avg_kafka, metric_amqp_min or 0, metric_amqp_max or 0, metric_kafka_min or 0, metric_kafka_max or 0))

                local okm, resm, codem = send_to_bridge(metric_payload)
                if not okm then
                    log_error('metrics send failed: ' .. tostring(resm) .. ' code=' .. tostring(codem))
                else
                    log_info(string.format('metrics sent: trades=%d avg_amqp=%.2f avg_kafka=%.2f', metric_trades_count or 0, avg_amqp, avg_kafka))
                end
            end)
            -- reset accumulators
            metric_last_time = now
            metric_sample_count = 0
            metric_amqp_acc = 0
            metric_kafka_acc = 0
            metric_trades_count = 0
            metric_amqp_min = nil
            metric_amqp_max = nil
            metric_kafka_min = nil
            metric_kafka_max = nil
        end

        if now - last_dump >= 30 then
            pcall(show_queues)
            last_dump = now
        end
    -- Sleep a short time to allow faster draining while still yielding to QUIK
    -- use socket.sleep for fractional seconds (QUIK's sleep expects integer)
    socket.sleep(0.1)
    end
end


-- Фоновая обработка очередей: отправляем ограниченное количество сообщений за итерацию
function process_queues()
    -- AMQP очередь
    for i = 1, queue_max_per_tick do
        if #amqp_queue == 0 then break end
        local item = table.remove(amqp_queue, 1)
        if item and ctx and amqp_ready then
            pcall(function() ctx:publish(item.msg, item.rk) end)
        end
    end

    -- Kafka бинарная очередь (через REST или native)
    -- process up to queue_max_per_tick items OR until time budget exhausted
    local start_t = socket.gettime()
    local time_budget = 0.03 -- seconds per main-loop for kafka work (tunable)
    for i = 1, queue_max_per_tick do
        if #kafka_bin_queue == 0 then break end
        local item = table.remove(kafka_bin_queue, 1)
        if not item then break end
        -- break if we've exceeded time budget to keep main loop responsive
        if socket.gettime() - start_t > time_budget then
            -- push item back to head so it will be retried soon
            table.insert(kafka_bin_queue, 1, item)
            break
        end
        -- Always prefer REST Proxy; fallback to bridge; persist to outbox if neither available.
        -- если мост недавно упал — не делаем агрессивных повторов для экономии CPU
        if last_bridge_failure ~= 0 and (os.time() - last_bridge_failure) < BRIDGE_RETRY_SECS then
            table.insert(kafka_bin_queue, 1, item)
            break
        end

    -- ensure schema id cached (one-time)
    init_schema_cache()
    if LOG_PROCESS_QUEUES then log_info('process_queues: schema_id=' .. tostring(schema_id)) end

        -- prepare bytes: if schema_id available, wrap into Confluent SR wire format
        local send_bytes = item.msg
        if schema_id then
            local wrapped, werr = wrap_with_sr(item.msg, schema_id)
            if wrapped then send_bytes = wrapped end
        end

    -- Prefer using local bridge for delivery (do not use REST Proxy)
    if LOG_PROCESS_QUEUES then log_info("process_queues: will send via bridge preview first8=" .. bytes_preview_hex(send_bytes, 8)) end
        local b64_out = mime.b64(send_bytes):gsub("\r\n", "")
        local payload = { topic = item.topic, records = { { value = b64_out, binary = true } } }
        -- Prefer fast TCP framed transport; fallback to HTTP bridge if TCP not available
        local sent_via_tcp = false
        if tcp_connect() then
            local ok_tcp = tcp_send_frame(item.topic or "trades", send_bytes)
            if ok_tcp then sent_via_tcp = true end
        end
        if sent_via_tcp then
            -- consider this sent (producer will enqueue on bridge)
            last_bridge_failure = 0
        elseif bridge_available then
            local ok, res, code = send_to_bridge(payload)
            local ncode = tonumber(code)
            if not ok or (ncode and ncode >= 400) then
                local msg = string.format("Bridge binary publish failed: ok=%s code=%s resp=%s", tostring(ok), tostring(code), tostring(res))
                print(msg)
                log_error(msg)
                last_bridge_failure = os.time()
                -- return item to queue to retry later
                table.insert(kafka_bin_queue, 1, item)
                break
            else
                last_bridge_failure = 0
            end
        else
            -- Bridge unavailable — do a quick live check to avoid writing files if the bridge just recovered
            local ok_hb, healthy = pcall(check_bridge_health)
            local sent_via_bridge = false
            if ok_hb and healthy then
                bridge_available = true
                log_info("process_queues: bridge appeared healthy on recheck; attempting send")
                local ok2, res2, code2 = send_to_bridge(payload)
                local ncode2 = tonumber(code2)
                if not ok2 or (ncode2 and ncode2 >= 400) then
                    log_error(string.format("process_queues: send_to_bridge failed after recheck: ok=%s code=%s resp=%s", tostring(ok2), tostring(code2), tostring(res2)))
                    -- fall through to persist below
                else
                    last_bridge_failure = 0
                    sent_via_bridge = true
                end
            end

            if not sent_via_bridge then
                -- Persist to outbox as bridge not available or send failed
                local send_bytes2 = item.msg
                if schema_id then
                    local wrapped2, werr2 = wrap_with_sr(item.msg, schema_id)
                    if wrapped2 then send_bytes2 = wrapped2 end
                end
                log_info("process_queues: outbox fallback preview first8=" .. bytes_preview_hex(send_bytes2, 8))
                local b64_out2 = mime.b64(send_bytes2):gsub("\r\n", "")
                local ok_file, ferr = write_outbox({ topic = item.topic, records = { { value = b64_out2, binary = true } } })
                if not ok_file then
                    log_error("outbox write bin failed: " .. tostring(ferr))
                    -- couldn't persist to disk; requeue and back off
                    table.insert(kafka_bin_queue, 1, item)
                    last_bridge_failure = os.time()
                    break
                else
                    last_bridge_failure = last_bridge_failure or os.time()
                end
            end
        end
    end

    -- JSON mode removed: nothing to flush here
    -- native librdkafka support removed; nothing to flush here
end

-- Простая проверка здоровья bridge: пробуем GET /health
function check_bridge_health()
    -- Prefer a fast TCP connect to the bridge port; if the port is listening, bridge is up
    local ok, err = pcall(function()
        local sock = socket.tcp()
        sock:settimeout(1)
        local res, rc = sock:connect(bridge_host, bridge_port)
        if res then
            pcall(function() sock:close() end)
            return true
        end
        return false
    end)
    if not ok then
        log_error("check_bridge_health tcp error: " .. tostring(err))
        return false
    end
    -- if pcall returned true, inner function returned true/false in ok; use that
    if ok then
        -- pcall succeeded; re-run a non-pcall check to get boolean result safely
        local sock = socket.tcp()
        sock:settimeout(1)
        local res = sock:connect(bridge_host, bridge_port)
        if res then
            pcall(function() sock:close() end)
            log_info("check_bridge_health: tcp connect successful")
            return true
        end
        log_info("check_bridge_health: tcp connect failed")
        return false
    end
    return false
end

-- (bridge/outbox and related configuration moved to the top configuration block)

function ensure_outbox()
    if OUTBOX_READY then return true end
    -- try using LuaFileSystem if available
    local ok, lfs = pcall(function() return require('lfs') end)
    if ok and lfs and lfs.mkdir then
        local succ, derr = pcall(function() lfs.mkdir(OUTBOX_DIR) end)
        OUTBOX_READY = true
        return true
    end
    -- fallback: run mkdir once (may spawn a cmd window) but avoid repeating it
    local suc, err = pcall(function() os.execute(string.format('mkdir "%s"', OUTBOX_DIR)) end)
    OUTBOX_READY = true
    return suc
end

function write_outbox(payload)
    local ok, body = pcall(function() return json.encode(payload) end)
    if not ok or not body then return false, "encode_failed" end
    -- ensure folder exists
    ensure_outbox()
    local basename = string.format("%d_%06d.json", os.time(), math.random(1,999999))
    local final = OUTBOX_DIR .. "\\" .. basename
    local tmp = final .. ".tmp"
    -- write to tmp then atomically replace so readers never see partial files
    local f, ferr = io.open(tmp, "wb")
    if not f then return false, ferr end
    local okw, werr = pcall(function()
        f:write(body)
        -- ensure data is flushed to disk before closing to avoid readers seeing partial/empty files
        if f.flush then pcall(function() f:flush() end) end
        f:close()
    end)
    if not okw then
        -- cleanup tmp if write failed
        pcall(function() os.remove(tmp) end)
        return false, werr
    end
    -- try atomic replace; on Windows os.rename will fail if final exists, so remove first
    pcall(function()
        if os.remove(final) then end
    end)
    local okr, rerr = pcall(function() return os.rename(tmp, final) end)
    if not okr then
        -- last resort: try os.execute move (platform-specific), then leave tmp for manual inspection
        pcall(function()
            local cmd = string.format('move /Y "%s" "%s"', tmp, final)
            os.execute(cmd)
        end)
    end
    log_info("wrote outbox file: " .. tostring(final))
    return true
end
function send_to_bridge(payload)
    local body = json.encode(payload)
    -- Quick sanity checks: ensure the JSON is valid and base64 fields decode where expected.
    local ok_dec, parsed = pcall(function() return json.decode(body) end)
    if not ok_dec or type(parsed) ~= 'table' then
        log_error("send_to_bridge: payload JSON invalid; persisting to outbox instead of sending")
        pcall(function() write_outbox(payload) end)
        return false, nil, "invalid_json"
    end
    if type(parsed.records) == 'table' then
        for i, rec in ipairs(parsed.records) do
            if rec and rec.binary == true then
                local v = rec.value
                if type(v) ~= 'string' or #v == 0 then
                    log_error("send_to_bridge: record."..tostring(i).." has invalid base64 value; persisting to outbox")
                    pcall(function() write_outbox(payload) end)
                    return false, nil, "invalid_base64"
                end
                -- If mime.unb64 exists use it, otherwise use a lightweight pattern+length check
                local decoder = mime and mime.unb64 or nil
                if type(decoder) == 'function' then
                    local okb = pcall(function() local _ = decoder(v) end)
                    if not okb then
                        log_error("send_to_bridge: base64 decode failed for record."..tostring(i).."; persisting to outbox")
                        pcall(function() write_outbox(payload) end)
                        return false, nil, "invalid_base64"
                    end
                else
                    -- crude sanity: allowed chars and length mod 4
                    if not v:match('^[A-Za-z0-9+/=]+$') or (#v % 4 ~= 0) then
                        log_error("send_to_bridge: base64 pattern check failed for record."..tostring(i).."; persisting to outbox")
                        pcall(function() write_outbox(payload) end)
                        return false, nil, "invalid_base64"
                    end
                end
            end
        end
    end
    local url = string.format("http://%s:%d/publish", bridge_host, bridge_port)
    local response = {}
    -- делаем короткий таймаут; если bridge недоступен — возвращаем ошибку быстро
    local prev_to = safe_settimeout(2)
    local ok, res, code = pcall(function()
        return http.request{
            url = url,
            method = "POST",
            headers = {
                ["Content-Type"] = "application/json",
                ["Content-Length"] = tostring(#body)
            },
            source = ltn12.source.string(body),
            sink = ltn12.sink.table(response)
        }
    end)
    if prev_to ~= nil then safe_settimeout(prev_to) end

    if not ok then
    log_error("send_to_bridge http error: " .. tostring(res))
    last_bridge_failure = os.time()
    bridge_available = false
    return false, nil, tostring(res)
    end
    -- ok == true, res == first return of http.request, code == status code (may be nil/string)
    local resp_body = table.concat(response)
    local ncode = nil
    if code ~= nil then ncode = tonumber(code) or nil end
    if ncode and ncode >= 400 then
    log_error(string.format("send_to_bridge returned code %s, body=%s", tostring(ncode), resp_body))
    last_bridge_failure = os.time()
    bridge_available = false
    else
    log_info(string.format("send_to_bridge success code=%s body=%s", tostring(ncode), resp_body))
    -- mark bridge healthy when we successfully publish
    last_bridge_failure = 0
    bridge_available = true
    end
    return true, res, ncode, resp_body
end

-- Self-test hook: encode sample TradeData, wrap with SR if available and log first bytes (only runs when trigger file exists)
-- Self-test configuration: controlled by the top-level constant defined in the configuration block
-- (do not redefine here)

local function bytes_to_hex(s, n)
    n = n or #s
    local parts = {}
    for i = 1, math.min(n, #s) do
        parts[#parts+1] = string.format("%02X", string.byte(s, i))
    end
    return table.concat(parts, " ")
end

local function test_wire_format()
    local sample = {
        trade_num = 123456789,
        flags = 0,
        price = 100.5,
        qty = 10,
        value = 1005.0,
        accruedint = 0.0,
        yield = 0.0,
        settlecode = "",
        reporate = 0.0,
        repovalue = 0.0,
        repo2value = 0.0,
        repoterm = 0,
        sec_code = "TEST",
        class_code = "TST",
        datetime = { year = 2025, month = 8, day = 20, hour = 12, min = 0, sec = 0 },
        period = 1
    }
    local ok_enc, pbbytes_or_err = pcall(function() return pb.encode("TradeData", sample) end)
    if not ok_enc or not pbbytes_or_err then
        log_error("self-test: pb.encode failed: " .. tostring(pbbytes_or_err))
        return false, tostring(pbbytes_or_err)
    end
    local raw = pbbytes_or_err
    local wrapped = raw
    if schema_id then
        local w, werr = wrap_with_sr(raw, schema_id)
        if w then wrapped = w else log_error("self-test: wrap_with_sr failed: "..tostring(werr)) end
    else
        log_info("self-test: schema_id not set; will show raw protobuf bytes")
    end

    -- log first 5 bytes
    local first_bytes = bytes_to_hex(wrapped, 5)
    local msg = string.format("self-test: wrapped len=%d first5=%s", #wrapped, first_bytes)
    print(msg)
    log_info(msg)
    -- enqueue raw protobuf bytes for sending (process_queues will wrap with SR and deliver)
    if pbbytes_or_err then
        -- ensure schema cache is initialized (will log schema_id)
        pcall(init_schema_cache)
        -- push raw protobuf (not SR-wrapped) so normal pipeline wraps it once
        table.insert(kafka_bin_queue, { msg = pbbytes_or_err, topic = "trades" })
        log_info("self-test: queued test trade into kafka_bin_queue preview first8=" .. bytes_preview_hex(pbbytes_or_err, 8))
        -- attempt immediate processing in background (non-blocking to caller)
        pcall(process_queues)
    end
    return true
end

-- Run self-test at startup if enabled via constant
pcall(function()
    if SELF_TEST_RUN_ON_START then
        test_wire_format()
    end
end)
