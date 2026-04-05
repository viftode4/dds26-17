#!lua name=stock_lib

-- ============================================================================
-- Stock Service Lua Function Library (Redis Cluster edition)
-- ============================================================================
-- All operations are single-item. Key naming uses {item_{item_id}} hash tag so
-- that data, lock, and status keys for the same item land on the same shard.
--
-- Key layout per item:
--   {item_{id}}:data              — item HASH (available_stock, reserved_stock, price)
--   {item_{id}}:lock:{saga}       — 2PC lock STRING (amount, short TTL)
--   {item_{id}}:2pc-status:{saga} — 2PC status HASH (status, amount)
--   {item_{id}}:saga-status:{saga}— saga status STRING (executed / compensated)
--   {item_{id}}:amounts:{saga}    — saga amounts HASH (item_id -> amount, for compensate)
--
-- 2PC operations:   stock_2pc_prepare_one, stock_2pc_commit_one, stock_2pc_abort_one
-- Saga operations:  stock_saga_execute_one, stock_saga_compensate_one
-- Direct API ops:   stock_subtract_direct, stock_add_direct
-- ============================================================================

-- 2PC Prepare (single item): Validate + deduct stock atomically
--
-- KEYS[1] = {item_{id}}:data
-- KEYS[2] = {item_{id}}:lock:{saga_id}
-- KEYS[3] = {item_{id}}:2pc-status:{saga_id}
-- ARGV[1] = saga_id
-- ARGV[2] = item_id
-- ARGV[3] = amount
-- ARGV[4] = lock_ttl
-- Returns: 1=success, 0=insufficient/already aborted or committed
local function stock_2pc_prepare_one(KEYS, ARGV)
    local amount   = tonumber(ARGV[3])
    local lock_ttl = tonumber(ARGV[4])

    -- Idempotency + poison pill: refuse if already aborted or committed.
    -- Prevents a late prepare (connection retry) from re-deducting after the
    -- orchestrator already decided to abort.
    local status = redis.call('HGET', KEYS[3], 'status')
    if status == 'prepared'   then return 1 end
    if status == 'aborted' or status == 'committed' then return 0 end

    -- Validate sufficient available_stock
    local available = tonumber(redis.call('HGET', KEYS[1], 'available_stock'))
    if not available or available < amount then
        return 0
    end

    -- Deduct and store amount durably in both lock key and status hash
    redis.call('HINCRBY', KEYS[1], 'available_stock', -amount)
    redis.call('SETEX', KEYS[2], lock_ttl, tostring(amount))
    redis.call('HSET',  KEYS[3], 'amount', tostring(amount))
    redis.call('HSET',  KEYS[3], 'status', 'prepared')
    redis.call('EXPIRE', KEYS[3], 86400)
    return 1
end

-- 2PC Commit (single item): Finalize — deduction already happened in prepare
--
-- KEYS[1] = {item_{id}}:data
-- KEYS[2] = {item_{id}}:lock:{saga_id}
-- KEYS[3] = {item_{id}}:2pc-status:{saga_id}
-- ARGV[1] = saga_id
-- ARGV[2] = item_id
-- ARGV[3] = amount  (re-apply if prepare data lost during failover)
local function stock_2pc_commit_one(KEYS, ARGV)
    local amount = tonumber(ARGV[3])

    -- Idempotency
    local status = redis.call('HGET', KEYS[3], 'status')
    if status == 'committed' then return 1 end

    -- Safety: if prepare data was lost during a Redis master failover,
    -- re-apply the deduction. In 2PC the commit decision is irrevocable
    -- so we MUST deduct to maintain conservation invariant.
    if status ~= 'prepared' then
        redis.call('HINCRBY', KEYS[1], 'available_stock', -amount)
        redis.call('HSET',  KEYS[3], 'amount', tostring(amount))
    end

    redis.call('DEL',  KEYS[2])
    redis.call('HSET', KEYS[3], 'status', 'committed')
    redis.call('EXPIRE', KEYS[3], 86400)
    return 1
end

-- 2PC Abort (single item): Restore stock from status hash
--
-- KEYS[1] = {item_{id}}:data
-- KEYS[2] = {item_{id}}:lock:{saga_id}
-- KEYS[3] = {item_{id}}:2pc-status:{saga_id}
-- ARGV[1] = saga_id
-- ARGV[2] = item_id
local function stock_2pc_abort_one(KEYS, ARGV)
    -- Idempotency
    local status = redis.call('HGET', KEYS[3], 'status')
    if status == 'aborted' then return 1 end

    -- Restore stock from status hash (survives lock TTL expiry)
    local amount = redis.call('HGET', KEYS[3], 'amount')
    if amount then
        redis.call('HINCRBY', KEYS[1], 'available_stock', tonumber(amount))
    end
    redis.call('DEL', KEYS[2])

    redis.call('HSET', KEYS[3], 'status', 'aborted')
    redis.call('EXPIRE', KEYS[3], 86400)
    return 1
end

-- Saga Execute (single item): Direct deduction, no locks
--
-- KEYS[1] = {item_{id}}:data
-- KEYS[2] = {item_{id}}:saga-status:{saga_id}
-- KEYS[3] = {item_{id}}:amounts:{saga_id}
-- ARGV[1] = saga_id
-- ARGV[2] = item_id
-- ARGV[3] = amount
-- Returns: 1=success, 0=insufficient / already compensated
local function stock_saga_execute_one(KEYS, ARGV)
    local item_id = ARGV[2]
    local amount  = tonumber(ARGV[3])

    -- Idempotency + poison pill: refuse if already compensated
    local status = redis.call('GET', KEYS[2])
    if status == 'executed'    then return 1 end
    if status == 'compensated' then return 0 end

    -- Validate
    local available = tonumber(redis.call('HGET', KEYS[1], 'available_stock'))
    if not available or available < amount then
        return 0
    end

    redis.call('HINCRBY', KEYS[1], 'available_stock', -amount)
    -- Store amount in the per-item amounts hash so compensate can recover it
    redis.call('HSET',   KEYS[3], item_id, tostring(amount))
    redis.call('EXPIRE', KEYS[3], 86400)
    redis.call('SETEX',  KEYS[2], 86400, 'executed')
    return 1
end

-- Saga Compensate (single item): Restore stock from stored amounts
--
-- KEYS[1] = {item_{id}}:data
-- KEYS[2] = {item_{id}}:saga-status:{saga_id}
-- KEYS[3] = {item_{id}}:amounts:{saga_id}
-- ARGV[1] = saga_id
-- ARGV[2] = item_id
local function stock_saga_compensate_one(KEYS, ARGV)
    local item_id = ARGV[2]

    -- Idempotency
    local status = redis.call('GET', KEYS[2])
    if status == 'compensated' then return 1 end

    -- Restore stock from amounts hash
    local amount = redis.call('HGET', KEYS[3], item_id)
    if amount then
        redis.call('HINCRBY', KEYS[1], 'available_stock', tonumber(amount))
    end

    redis.call('DEL',   KEYS[3])
    redis.call('SETEX', KEYS[2], 86400, 'compensated')
    return 1
end

-- Atomic check-and-decrement for /subtract API endpoint
-- KEYS[1] = {item_{id}}:data
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
-- KEYS[1] = {item_{id}}:data
-- ARGV[1] = amount to add
local function add_direct(KEYS, ARGV)
    local amount = tonumber(ARGV[1])
    local exists = redis.call('EXISTS', KEYS[1])
    if exists == 0 then
        return redis.error_reply('ITEM_NOT_FOUND')
    end
    return redis.call('HINCRBY', KEYS[1], 'available_stock', amount)
end

redis.register_function('stock_2pc_prepare_one',   stock_2pc_prepare_one)
redis.register_function('stock_2pc_commit_one',    stock_2pc_commit_one)
redis.register_function('stock_2pc_abort_one',     stock_2pc_abort_one)
redis.register_function('stock_saga_execute_one',  stock_saga_execute_one)
redis.register_function('stock_saga_compensate_one', stock_saga_compensate_one)
redis.register_function('stock_subtract_direct',   subtract_direct)
redis.register_function('stock_add_direct',        add_direct)
