#!lua name=payment_lib

-- ============================================================================
-- Payment Service Lua Function Library
-- ============================================================================
-- Provides atomic operations for credit management:
--   Transaction operations: try_reserve, confirm, cancel
--   Direct API operations: subtract_direct, add_direct
-- ============================================================================

-- Reserve credit for a transaction
-- KEYS[1] = user:{user_id}, KEYS[2] = reservation:{saga_id}:{user_id}
-- KEYS[3] = saga:{saga_id}:payment:status, KEYS[4] = payment-outbox
-- KEYS[5] = reservation_amount:{saga_id}:{user_id} (24h fallback)
-- ARGV[1] = amount, ARGV[2] = saga_id, ARGV[3] = user_id, ARGV[4] = ttl
local function payment_try_reserve(KEYS, ARGV)
    local skip_outbox = ARGV[5]  -- optional, "1" to skip
    local status = redis.call('GET', KEYS[3])
    if status == 'reserved' then
        if skip_outbox ~= "1" then
            redis.call('XADD', KEYS[4], 'MAXLEN', '~', '10000', '*',
                'saga_id', ARGV[2], 'event', 'reserved')
        end
        return 1
    end

    local available = tonumber(redis.call('HGET', KEYS[1], 'available_credit'))
    if not available then
        if skip_outbox ~= "1" then
            redis.call('XADD', KEYS[4], 'MAXLEN', '~', '10000', '*',
                'saga_id', ARGV[2], 'event', 'failed', 'reason', 'user_not_found')
        end
        return 0
    end

    local amount = tonumber(ARGV[1])
    if available < amount then
        if skip_outbox ~= "1" then
            redis.call('XADD', KEYS[4], 'MAXLEN', '~', '10000', '*',
                'saga_id', ARGV[2], 'event', 'failed', 'reason', 'insufficient_credit')
        end
        return 0
    end

    redis.call('HINCRBY', KEYS[1], 'available_credit', -amount)
    redis.call('HINCRBY', KEYS[1], 'held_credit', amount)
    redis.call('SET', KEYS[2], tostring(amount))
    redis.call('EXPIRE', KEYS[2], tonumber(ARGV[4]))
    -- Fallback amount key survives short reservation TTL (24h)
    redis.call('SETEX', KEYS[5], 86400, tostring(amount))
    redis.call('SETEX', KEYS[3], 86400, 'reserved')
    if skip_outbox ~= "1" then
        redis.call('XADD', KEYS[4], 'MAXLEN', '~', '10000', '*',
            'saga_id', ARGV[2], 'event', 'reserved', 'amount', ARGV[1])
    end
    return 1
end

-- Confirm reserved credit (finalize payment)
-- KEYS[1] = user:{user_id}, KEYS[2] = reservation:{saga_id}:{user_id}
-- KEYS[3] = saga:{saga_id}:payment:status, KEYS[4] = payment-outbox
-- KEYS[5] = reservation_amount:{saga_id}:{user_id} (24h fallback)
-- ARGV[1] = saga_id
local function payment_confirm(KEYS, ARGV)
    local skip_outbox = ARGV[2]  -- optional, "1" to skip
    local status = redis.call('GET', KEYS[3])
    if status == 'confirmed' then
        if skip_outbox ~= "1" then
            redis.call('XADD', KEYS[4], 'MAXLEN', '~', '10000', '*',
                'saga_id', ARGV[1], 'event', 'confirmed')
        end
        return 1
    end
    local amount = redis.call('GET', KEYS[2])
    if not amount then
        if skip_outbox ~= "1" then
            redis.call('XADD', KEYS[4], 'MAXLEN', '~', '10000', '*',
                'saga_id', ARGV[1], 'event', 'confirm_failed', 'reason', 'reservation_expired')
        end
        return -1
    end
    redis.call('HINCRBY', KEYS[1], 'held_credit', -tonumber(amount))
    redis.call('DEL', KEYS[2])
    redis.call('DEL', KEYS[5])
    redis.call('SETEX', KEYS[3], 86400, 'confirmed')
    if skip_outbox ~= "1" then
        redis.call('XADD', KEYS[4], 'MAXLEN', '~', '10000', '*',
            'saga_id', ARGV[1], 'event', 'confirmed')
    end
    return 1
end

-- Cancel reserved credit (restore to available)
-- KEYS[1] = user:{user_id}, KEYS[2] = reservation:{saga_id}:{user_id}
-- KEYS[3] = saga:{saga_id}:payment:status, KEYS[4] = payment-outbox
-- KEYS[5] = reservation_amount:{saga_id}:{user_id} (24h fallback)
-- ARGV[1] = saga_id
local function payment_cancel(KEYS, ARGV)
    local skip_outbox = ARGV[2]  -- optional, "1" to skip
    local status = redis.call('GET', KEYS[3])
    if status == 'cancelled' then return 1 end
    local amount = redis.call('GET', KEYS[2])
    if amount then
        redis.call('HINCRBY', KEYS[1], 'available_credit', tonumber(amount))
        redis.call('HINCRBY', KEYS[1], 'held_credit', -tonumber(amount))
        redis.call('DEL', KEYS[2])
    else
        -- Reservation key expired (TTL) — try fallback amount key
        amount = redis.call('GET', KEYS[5])
        if amount then
            redis.call('HINCRBY', KEYS[1], 'available_credit', tonumber(amount))
            redis.call('HINCRBY', KEYS[1], 'held_credit', -tonumber(amount))
        end
    end
    redis.call('DEL', KEYS[5])
    redis.call('SETEX', KEYS[3], 86400, 'cancelled')
    if skip_outbox ~= "1" then
        redis.call('XADD', KEYS[4], 'MAXLEN', '~', '10000', '*',
            'saga_id', ARGV[1], 'event', 'cancelled')
    end
    return 1
end

-- Atomic check-and-decrement for /pay API endpoint
-- KEYS[1] = user:{user_id}
-- ARGV[1] = amount to subtract
local function subtract_direct(KEYS, ARGV)
    local amount = tonumber(ARGV[1])
    local available = tonumber(redis.call('HGET', KEYS[1], 'available_credit'))
    if not available then
        return redis.error_reply('USER_NOT_FOUND')
    end
    if available < amount then
        return redis.error_reply('INSUFFICIENT_CREDIT')
    end
    redis.call('HINCRBY', KEYS[1], 'available_credit', -amount)
    return available - amount
end

-- Atomic increment for /add_funds API endpoint
-- KEYS[1] = user:{user_id}
-- ARGV[1] = amount to add
local function add_direct(KEYS, ARGV)
    local amount = tonumber(ARGV[1])
    local exists = redis.call('EXISTS', KEYS[1])
    if exists == 0 then
        return redis.error_reply('USER_NOT_FOUND')
    end
    return redis.call('HINCRBY', KEYS[1], 'available_credit', amount)
end

redis.register_function('payment_try_reserve', payment_try_reserve)
redis.register_function('payment_confirm', payment_confirm)
redis.register_function('payment_cancel', payment_cancel)
redis.register_function('payment_subtract_direct', subtract_direct)
redis.register_function('payment_add_direct', add_direct)
