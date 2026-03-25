#!lua name=stock_lib

-- ============================================================================
-- Stock Service Lua Function Library
-- ============================================================================
-- 2PC operations: stock_2pc_prepare, stock_2pc_commit, stock_2pc_abort
-- Saga operations: stock_saga_execute, stock_saga_compensate
-- Direct API operations: stock_subtract_direct, stock_add_direct
-- ============================================================================

-- 2PC Prepare: Validate + deduct stock atomically
--
-- KEYS layout (for N items):
--   KEYS[1..N]       = item:{item_id} hashes
--   KEYS[N+1..2N]    = lock:2pc:{saga_id}:{item_id} keys (store amounts for abort)
--   KEYS[2N+1]       = saga:{saga_id}:stock:status
-- ARGV: saga_id, lock_ttl, (item_id, amount)...
local function stock_2pc_prepare(KEYS, ARGV)
    local saga_id = ARGV[1]
    local lock_ttl = tonumber(ARGV[2])
    local n_items = (#KEYS - 1) / 2
    local status_key = KEYS[2 * n_items + 1]

    -- Idempotency + poison pill: if abort already ran, refuse to prepare.
    -- This prevents a late prepare (redis-py retry after Sentinel failover)
    -- from deducting stock after the orchestrator already decided to abort.
    local status = redis.call('HGET', status_key, 'status')
    if status == 'prepared' then return 1 end
    if status == 'aborted' or status == 'committed' then return 0 end

    -- Validate ALL items have sufficient available_stock
    for i = 1, n_items do
        local item_key = KEYS[i]
        local amount = tonumber(ARGV[2 + i * 2])
        local available = tonumber(redis.call('HGET', item_key, 'available_stock'))
        if not available or available < amount then
            return 0
        end
    end

    -- Deduct stock and store amounts durably
    for i = 1, n_items do
        local item_key = KEYS[i]
        local lock_key = KEYS[n_items + i]
        local item_id = ARGV[1 + i * 2]
        local amount = tonumber(ARGV[2 + i * 2])
        redis.call('HINCRBY', item_key, 'available_stock', -amount)
        -- Lock key guards against duplicate prepares (short TTL is fine)
        redis.call('SETEX', lock_key, lock_ttl, tostring(amount))
        -- Store amount in status hash (survives lock TTL expiry, 24h TTL)
        redis.call('HSET', status_key, 'amount:' .. item_id, tostring(amount))
    end

    redis.call('HSET', status_key, 'status', 'prepared')
    redis.call('EXPIRE', status_key, 86400)
    return 1
end

-- 2PC Commit: Finalize (deduction already happened in prepare)
--
-- Same KEYS layout as prepare
-- ARGV: saga_id, n_items, (item_id, amount)...
--   item_id/amount pairs are used to re-apply the deduction if prepare
--   data was lost during a Redis Sentinel failover.
local function stock_2pc_commit(KEYS, ARGV)
    local saga_id = ARGV[1]
    local n_items = tonumber(ARGV[2])
    local status_key = KEYS[2 * n_items + 1]

    -- Idempotency
    local status = redis.call('HGET', status_key, 'status')
    if status == 'committed' then return 1 end

    -- Safety: if prepare data was lost (Redis Sentinel failover during 2PC),
    -- re-apply the stock deduction.  In 2PC the commit decision is irrevocable
    -- so we MUST ensure stock is deducted to maintain conservation invariant:
    --   total_credit_spent == total_stock_sold * price
    if status ~= 'prepared' then
        for i = 1, n_items do
            local item_key = KEYS[i]
            local item_id = ARGV[1 + i * 2]
            local amount = tonumber(ARGV[2 + i * 2])
            redis.call('HINCRBY', item_key, 'available_stock', -amount)
            redis.call('HSET', status_key, 'amount:' .. item_id, tostring(amount))
        end
    end

    -- Delete all lock keys (amounts already deducted in prepare or above)
    for i = 1, n_items do
        redis.call('DEL', KEYS[n_items + i])
    end

    redis.call('HSET', status_key, 'status', 'committed')
    redis.call('EXPIRE', status_key, 86400)
    return 1
end

-- 2PC Abort: Restore stock from status hash (undo prepare deduction)
--
-- Same KEYS layout as prepare
-- ARGV: saga_id, n_items, item_id_1, item_id_2, ...
local function stock_2pc_abort(KEYS, ARGV)
    local saga_id = ARGV[1]
    local n_items = tonumber(ARGV[2])
    local status_key = KEYS[2 * n_items + 1]

    -- Idempotency
    local status = redis.call('HGET', status_key, 'status')
    if status == 'aborted' then return 1 end

    -- Restore stock from status hash (survives lock TTL expiry)
    for i = 1, n_items do
        local item_key = KEYS[i]
        local lock_key = KEYS[n_items + i]
        local item_id = ARGV[2 + i]
        local amount = redis.call('HGET', status_key, 'amount:' .. item_id)
        if amount then
            redis.call('HINCRBY', item_key, 'available_stock', tonumber(amount))
        end
        redis.call('DEL', lock_key)
    end

    redis.call('HSET', status_key, 'status', 'aborted')
    redis.call('EXPIRE', status_key, 86400)
    return 1
end

-- Saga Execute: Direct deduction (no locks, no reserved_stock)
--
-- KEYS layout (for N items):
--   KEYS[1..N]   = item:{item_id} hashes
--   KEYS[N+1]    = saga:{saga_id}:stock:status
--   KEYS[N+2]    = saga:{saga_id}:stock:amounts
-- ARGV: saga_id, (item_id, amount)...
local function stock_saga_execute(KEYS, ARGV)
    local saga_id = ARGV[1]
    local n_items = #KEYS - 2
    local status_key = KEYS[n_items + 1]
    local amounts_key = KEYS[n_items + 2]

    -- Idempotency + poison pill: refuse if already compensated
    local status = redis.call('GET', status_key)
    if status == 'executed' then return 1 end
    if status == 'compensated' then return 0 end

    -- Validate ALL items
    for i = 1, n_items do
        local item_key = KEYS[i]
        local amount = tonumber(ARGV[1 + i * 2])
        local available = tonumber(redis.call('HGET', item_key, 'available_stock'))
        if not available or available < amount then
            return 0
        end
    end

    -- Apply deductions and store amounts for compensation
    for i = 1, n_items do
        local item_key = KEYS[i]
        local item_id = ARGV[i * 2]
        local amount = tonumber(ARGV[1 + i * 2])
        redis.call('HINCRBY', item_key, 'available_stock', -amount)
        redis.call('HSET', amounts_key, item_id, tostring(amount))
    end

    redis.call('SETEX', status_key, 86400, 'executed')
    redis.call('EXPIRE', amounts_key, 86400)
    return 1
end

-- Saga Compensate: Restore stock from stored amounts
--
-- Same KEYS layout as execute
-- ARGV: saga_id, n_items
local function stock_saga_compensate(KEYS, ARGV)
    local saga_id = ARGV[1]
    local n_items = tonumber(ARGV[2])
    local status_key = KEYS[n_items + 1]
    local amounts_key = KEYS[n_items + 2]

    -- Idempotency
    local status = redis.call('GET', status_key)
    if status == 'compensated' then return 1 end

    -- Restore stock from stored amounts
    local all_amounts = redis.call('HGETALL', amounts_key)
    for j = 1, #all_amounts, 2 do
        local item_id = all_amounts[j]
        local amount = tonumber(all_amounts[j + 1])
        if amount then
            redis.call('HINCRBY', 'item:' .. item_id, 'available_stock', amount)
        end
    end

    redis.call('DEL', amounts_key)
    redis.call('SETEX', status_key, 86400, 'compensated')
    return 1
end

-- Atomic check-and-decrement for /subtract API endpoint
-- KEYS[1] = item:{item_id}
-- ARGV[1] = amount to subtract
local function subtract_direct(KEYS, ARGV)
    local amount = tonumber(ARGV[1])
    local available = tonumber(redis.call('HGET', KEYS[1], 'available_stock'))
    if not available then
        return redis.error_reply('ITEM_NOT_FOUND')
    end
    if available < amount then
        return redis.error_reply('INSUFFICIENT_STOCK')
    end
    redis.call('HINCRBY', KEYS[1], 'available_stock', -amount)
    return available - amount
end

-- Atomic increment for /add API endpoint
-- KEYS[1] = item:{item_id}
-- ARGV[1] = amount to add
local function add_direct(KEYS, ARGV)
    local amount = tonumber(ARGV[1])
    local exists = redis.call('EXISTS', KEYS[1])
    if exists == 0 then
        return redis.error_reply('ITEM_NOT_FOUND')
    end
    return redis.call('HINCRBY', KEYS[1], 'available_stock', amount)
end

redis.register_function('stock_2pc_prepare', stock_2pc_prepare)
redis.register_function('stock_2pc_commit', stock_2pc_commit)
redis.register_function('stock_2pc_abort', stock_2pc_abort)
redis.register_function('stock_saga_execute', stock_saga_execute)
redis.register_function('stock_saga_compensate', stock_saga_compensate)
redis.register_function('stock_subtract_direct', subtract_direct)
redis.register_function('stock_add_direct', add_direct)
