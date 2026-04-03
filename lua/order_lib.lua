#!lua name=order_lib

-- ============================================================================
-- Order Service Lua Function Library
-- ============================================================================

-- Atomically add an item to an order and update total_cost
-- KEYS[1] = order:{order_id}:items (List)
-- KEYS[2] = order:{order_id} (Hash)
-- ARGV[1] = item entry string (item_id:quantity)
-- ARGV[2] = cost_increment (quantity * price)
local function add_item_atomic(KEYS, ARGV)
    -- Verify order exists
    local exists = redis.call('EXISTS', KEYS[2])
    if exists == 0 then
        return redis.error_reply('ORDER_NOT_FOUND')
    end

    redis.call('RPUSH', KEYS[1], ARGV[1])
    redis.call('HINCRBY', KEYS[2], 'total_cost', tonumber(ARGV[2]))
    return 1
end

-- Load order + items + claim idempotency key in a single FCALL (saves 1 RTT)
-- KEYS[1] = order:{order_id} (Hash)
-- KEYS[2] = order:{order_id}:items (List)
-- KEYS[3] = idempotency:checkout:{order_id}
-- ARGV[1] = claim_value (JSON string)
-- ARGV[2] = ttl (seconds)
-- Returns: {found, entry_flat, items_json, acquired}
local function order_load_and_claim(KEYS, ARGV)
    local order_key = KEYS[1]
    local items_key = KEYS[2]
    local idempotency_key = KEYS[3]
    local claim_value = ARGV[1]
    local ttl = tonumber(ARGV[2])

    -- Load order hash
    local entry = redis.call('HGETALL', order_key)
    if #entry == 0 then
        return {0, '', '[]', 0}  -- not found
    end

    -- Load items list
    local items = redis.call('LRANGE', items_key, 0, -1)

    -- Attempt SET NX (idempotency claim)
    local acquired = redis.call('SET', idempotency_key, claim_value, 'NX', 'EX', ttl)

    return {1, cjson.encode(entry), cjson.encode(items), acquired and 1 or 0}
end

-- Finalize checkout: idempotency success + mark order paid (atomic)
-- KEYS[1] = order:{id}, KEYS[2] = idempotency:checkout:{id}
-- ARGV[1] = saga_id, ARGV[2] = success JSON for idempotency key
-- Returns {ok, reason} — ok 1 = done or already success, ok 0 = skip/mismatch
local function order_complete_checkout(KEYS, ARGV)
    local order_key = KEYS[1]
    local idem_key = KEYS[2]
    local saga_id = ARGV[1]
    local success_json = ARGV[2]

    local raw = redis.call('GET', idem_key)
    if not raw then
        return {0, 'no_key'}
    end
    local data = cjson.decode(raw)
    if data.status == 'success' then
        return {1, 'already'}
    end
    if data.status ~= 'processing' or data.saga_id ~= saga_id then
        return {0, 'mismatch'}
    end
    redis.call('SET', idem_key, success_json, 'EX', 86400)
    redis.call('HSET', order_key, 'paid', 'true')
    return {1, 'done'}
end

-- Release processing claim (failed saga / retry)
-- KEYS[1] = idempotency:checkout:{id}, ARGV[1] = saga_id
-- Returns {ok, reason}
local function order_release_checkout(KEYS, ARGV)
    local idem_key = KEYS[1]
    local saga_id = ARGV[1]

    local raw = redis.call('GET', idem_key)
    if not raw then
        return {1, 'noop'}
    end
    local data = cjson.decode(raw)
    if data.status == 'processing' and data.saga_id == saga_id then
        redis.call('DEL', idem_key)
        return {1, 'released'}
    end
    return {0, 'skip'}
end

redis.register_function('order_add_item', add_item_atomic)
redis.register_function('order_load_and_claim', order_load_and_claim)
redis.register_function('order_complete_checkout', order_complete_checkout)
redis.register_function('order_release_checkout', order_release_checkout)
