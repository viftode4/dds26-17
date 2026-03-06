#!lua name=stock_lib

-- ============================================================================
-- Stock Service Lua Function Library
-- ============================================================================
-- Provides atomic operations for stock management:
--   Transaction operations: try_reserve_batch, confirm_batch, cancel_batch
--   Direct API operations: subtract_direct, add_direct
-- ============================================================================

-- Reserve ALL items atomically (all-or-nothing)
-- Used by both 2PC (prepare) and Saga (try_reserve)
--
-- Layout (for N items):
--   KEYS[1..N]       = item:{item_id} hashes
--   KEYS[N+1..2N]    = reservation:{saga_id}:{item_id} keys (with TTL)
--   KEYS[2N+1..3N]   = reservation_amount:{saga_id}:{item_id} keys (24h fallback)
--   KEYS[3N+1]       = stock-outbox stream
--   KEYS[3N+2]       = saga:{saga_id}:stock:status
--   ARGV[1]          = saga_id
--   ARGV[2]          = reservation_ttl
--   ARGV[3..3+2N]    = pairs of (item_id, amount)
--   ARGV[3+2N]        = optional skip_outbox ("1" to skip XADD)
local function try_reserve_batch(KEYS, ARGV)
    local saga_id = ARGV[1]
    local ttl = tonumber(ARGV[2])
    local n_items = (#KEYS - 2) / 3  -- derive from KEYS layout (3N+2), stable regardless of skip_outbox
    local skip_outbox = ARGV[3 + 2 * n_items]  -- optional, "1" to skip
    local outbox_key = KEYS[3 * n_items + 1]
    local status_key = KEYS[3 * n_items + 2]

    -- Idempotency check
    local status = redis.call('GET', status_key)
    if status == 'reserved' then
        if skip_outbox ~= "1" then
            redis.call('XADD', outbox_key, 'MAXLEN', '~', '10000', '*',
                'saga_id', saga_id, 'event', 'reserved')
        end
        return 1
    end

    -- Phase 1: Validate ALL items have sufficient stock
    for i = 1, n_items do
        local item_key = KEYS[i]
        local amount = tonumber(ARGV[2 + i * 2])
        local available = tonumber(redis.call('HGET', item_key, 'available_stock'))
        if not available then
            if skip_outbox ~= "1" then
                redis.call('XADD', outbox_key, 'MAXLEN', '~', '10000', '*',
                    'saga_id', saga_id, 'event', 'failed',
                    'reason', 'item_not_found', 'item_id', ARGV[1 + i * 2])
            end
            return 0
        end
        if available < amount then
            if skip_outbox ~= "1" then
                redis.call('XADD', outbox_key, 'MAXLEN', '~', '10000', '*',
                    'saga_id', saga_id, 'event', 'failed',
                    'reason', 'insufficient_stock', 'item_id', ARGV[1 + i * 2])
            end
            return 0
        end
    end

    -- Phase 2: All checks passed — apply ALL reservations atomically
    for i = 1, n_items do
        local item_key = KEYS[i]
        local reservation_key = KEYS[n_items + i]
        local fallback_key = KEYS[2 * n_items + i]
        local item_id = ARGV[1 + i * 2]
        local amount = tonumber(ARGV[2 + i * 2])

        redis.call('HINCRBY', item_key, 'available_stock', -amount)
        redis.call('HINCRBY', item_key, 'reserved_stock', amount)
        redis.call('SET', reservation_key, tostring(amount))
        redis.call('EXPIRE', reservation_key, ttl)
        -- Fallback amount key survives short reservation TTL (24h)
        redis.call('SETEX', fallback_key, 86400, tostring(amount))
    end

    -- Idempotency marker + outbox event (atomic with state change)
    redis.call('SETEX', status_key, 86400, 'reserved')
    if skip_outbox ~= "1" then
        redis.call('XADD', outbox_key, 'MAXLEN', '~', '10000', '*',
            'saga_id', saga_id, 'event', 'reserved')
    end
    return 1
end

-- Confirm ALL reserved items (finalize the sale)
-- Used by both 2PC (commit) and Saga (confirm)
local function confirm_batch(KEYS, ARGV)
    local saga_id = ARGV[1]
    local n_items = (tonumber(ARGV[2]))
    local skip_outbox = ARGV[3]  -- optional, "1" to skip
    local outbox_key = KEYS[3 * n_items + 1]
    local status_key = KEYS[3 * n_items + 2]

    -- Idempotency
    local status = redis.call('GET', status_key)
    if status == 'confirmed' then
        if skip_outbox ~= "1" then
            redis.call('XADD', outbox_key, 'MAXLEN', '~', '10000', '*',
                'saga_id', saga_id, 'event', 'confirmed')
        end
        return 1
    end

    for i = 1, n_items do
        local item_key = KEYS[i]
        local reservation_key = KEYS[n_items + i]
        local fallback_key = KEYS[2 * n_items + i]
        local amount = redis.call('GET', reservation_key)
        if not amount then
            -- Reservation key expired (TTL) — try fallback amount key (24h TTL)
            amount = redis.call('GET', fallback_key)
            if not amount then
                -- Both keys gone — truly unrecoverable
                if skip_outbox ~= "1" then
                    redis.call('XADD', outbox_key, 'MAXLEN', '~', '10000', '*',
                        'saga_id', saga_id, 'event', 'confirm_failed',
                        'reason', 'reservation_expired')
                end
                return -1
            end
        end
        redis.call('HINCRBY', item_key, 'reserved_stock', -tonumber(amount))
        redis.call('DEL', reservation_key)
        redis.call('DEL', fallback_key)
    end

    redis.call('SETEX', status_key, 86400, 'confirmed')
    if skip_outbox ~= "1" then
        redis.call('XADD', outbox_key, 'MAXLEN', '~', '10000', '*',
            'saga_id', saga_id, 'event', 'confirmed')
    end
    return 1
end

-- Cancel ALL reservations (restore stock)
-- Used by both 2PC (abort) and Saga (cancel)
local function cancel_batch(KEYS, ARGV)
    local saga_id = ARGV[1]
    local n_items = tonumber(ARGV[2])
    local skip_outbox = ARGV[3]  -- optional, "1" to skip
    local outbox_key = KEYS[3 * n_items + 1]
    local status_key = KEYS[3 * n_items + 2]

    -- Idempotency
    local status = redis.call('GET', status_key)
    if status == 'cancelled' then return 1 end

    for i = 1, n_items do
        local item_key = KEYS[i]
        local reservation_key = KEYS[n_items + i]
        local fallback_key = KEYS[2 * n_items + i]
        local amount = redis.call('GET', reservation_key)
        if amount then
            redis.call('HINCRBY', item_key, 'available_stock', tonumber(amount))
            redis.call('HINCRBY', item_key, 'reserved_stock', -tonumber(amount))
            redis.call('DEL', reservation_key)
        else
            -- Reservation key expired (TTL) — try fallback amount key
            amount = redis.call('GET', fallback_key)
            if amount then
                redis.call('HINCRBY', item_key, 'available_stock', tonumber(amount))
                redis.call('HINCRBY', item_key, 'reserved_stock', -tonumber(amount))
            end
        end
        redis.call('DEL', fallback_key)
    end

    redis.call('SETEX', status_key, 86400, 'cancelled')
    if skip_outbox ~= "1" then
        redis.call('XADD', outbox_key, 'MAXLEN', '~', '10000', '*',
            'saga_id', saga_id, 'event', 'cancelled')
    end
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

redis.register_function('stock_try_reserve_batch', try_reserve_batch)
redis.register_function('stock_confirm_batch', confirm_batch)
redis.register_function('stock_cancel_batch', cancel_batch)
redis.register_function('stock_subtract_direct', subtract_direct)
redis.register_function('stock_add_direct', add_direct)
