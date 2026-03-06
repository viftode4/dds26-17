#!lua name=payment_lib

-- ============================================================================
-- Payment Service Lua Function Library
-- ============================================================================
-- 2PC operations: payment_2pc_prepare, payment_2pc_commit, payment_2pc_abort
-- Saga operations: payment_saga_execute, payment_saga_compensate
-- Direct API operations: payment_subtract_direct, payment_add_direct
-- ============================================================================

-- 2PC Prepare: Validate + deduct credit atomically
--
-- KEYS[1] = user:{user_id}
-- KEYS[2] = lock:2pc:{saga_id}:{user_id}
-- KEYS[3] = saga:{saga_id}:payment:status
-- KEYS[4] = payment-outbox
-- ARGV: amount, saga_id, user_id, lock_ttl, skip_outbox
local function payment_2pc_prepare(KEYS, ARGV)
    local amount = tonumber(ARGV[1])
    local saga_id = ARGV[2]
    local lock_ttl = tonumber(ARGV[4])
    local skip_outbox = ARGV[5]

    -- Idempotency
    local status = redis.call('GET', KEYS[3])
    if status == 'prepared' then return 1 end

    -- Validate
    local available = tonumber(redis.call('HGET', KEYS[1], 'available_credit'))
    if not available or available < amount then
        return 0
    end

    -- Deduct credit and store amount in lock key (for abort recovery)
    redis.call('HINCRBY', KEYS[1], 'available_credit', -amount)
    redis.call('SETEX', KEYS[2], lock_ttl, tostring(amount))

    redis.call('SETEX', KEYS[3], 86400, 'prepared')
    if skip_outbox ~= "1" then
        redis.call('XADD', KEYS[4], 'MAXLEN', '~', '10000', '*',
            'saga_id', saga_id, 'event', 'prepared')
    end
    return 1
end

-- 2PC Commit: Finalize (deduction already happened in prepare)
--
-- KEYS[1] = user:{user_id}
-- KEYS[2] = lock:2pc:{saga_id}:{user_id}
-- KEYS[3] = saga:{saga_id}:payment:status
-- KEYS[4] = payment-outbox
-- ARGV: saga_id, skip_outbox
local function payment_2pc_commit(KEYS, ARGV)
    local saga_id = ARGV[1]
    local skip_outbox = ARGV[2]

    -- Idempotency
    local status = redis.call('GET', KEYS[3])
    if status == 'committed' then return 1 end

    -- Delete lock key (amount already deducted in prepare)
    redis.call('DEL', KEYS[2])

    redis.call('SETEX', KEYS[3], 86400, 'committed')
    if skip_outbox ~= "1" then
        redis.call('XADD', KEYS[4], 'MAXLEN', '~', '10000', '*',
            'saga_id', saga_id, 'event', 'committed')
    end
    return 1
end

-- 2PC Abort: Restore credit from lock key (undo prepare deduction)
--
-- KEYS[1] = user:{user_id}
-- KEYS[2] = lock:2pc:{saga_id}:{user_id}
-- KEYS[3] = saga:{saga_id}:payment:status
-- KEYS[4] = payment-outbox
-- ARGV: saga_id, skip_outbox
local function payment_2pc_abort(KEYS, ARGV)
    local saga_id = ARGV[1]
    local skip_outbox = ARGV[2]

    -- Idempotency
    local status = redis.call('GET', KEYS[3])
    if status == 'aborted' then return 1 end

    -- Restore credit from lock key (reverse the prepare deduction)
    local amount = redis.call('GET', KEYS[2])
    if amount then
        redis.call('HINCRBY', KEYS[1], 'available_credit', tonumber(amount))
    end
    redis.call('DEL', KEYS[2])

    redis.call('SETEX', KEYS[3], 86400, 'aborted')
    if skip_outbox ~= "1" then
        redis.call('XADD', KEYS[4], 'MAXLEN', '~', '10000', '*',
            'saga_id', saga_id, 'event', 'aborted')
    end
    return 1
end

-- Saga Execute: Direct deduction (no locks, no held_credit)
--
-- KEYS[1] = user:{user_id}
-- KEYS[2] = payment-outbox
-- KEYS[3] = saga:{saga_id}:payment:status
-- KEYS[4] = saga:{saga_id}:payment:amounts
-- ARGV: amount, saga_id, user_id, skip_outbox
local function payment_saga_execute(KEYS, ARGV)
    local amount = tonumber(ARGV[1])
    local saga_id = ARGV[2]
    local user_id = ARGV[3]
    local skip_outbox = ARGV[4]

    -- Idempotency
    local status = redis.call('GET', KEYS[3])
    if status == 'executed' then return 1 end

    -- Validate
    local available = tonumber(redis.call('HGET', KEYS[1], 'available_credit'))
    if not available or available < amount then
        return 0
    end

    redis.call('HINCRBY', KEYS[1], 'available_credit', -amount)
    redis.call('HSET', KEYS[4], user_id, tostring(amount))
    redis.call('EXPIRE', KEYS[4], 86400)

    redis.call('SETEX', KEYS[3], 86400, 'executed')
    if skip_outbox ~= "1" then
        redis.call('XADD', KEYS[2], 'MAXLEN', '~', '10000', '*',
            'saga_id', saga_id, 'event', 'executed')
    end
    return 1
end

-- Saga Compensate: Restore credit from stored amounts
--
-- KEYS[1] = user:{user_id}
-- KEYS[2] = payment-outbox
-- KEYS[3] = saga:{saga_id}:payment:status
-- KEYS[4] = saga:{saga_id}:payment:amounts
-- ARGV: saga_id, skip_outbox
local function payment_saga_compensate(KEYS, ARGV)
    local saga_id = ARGV[1]
    local skip_outbox = ARGV[2]

    -- Idempotency
    local status = redis.call('GET', KEYS[3])
    if status == 'compensated' then return 1 end

    -- Restore credit from stored amounts
    local all_amounts = redis.call('HGETALL', KEYS[4])
    for j = 1, #all_amounts, 2 do
        local amount = tonumber(all_amounts[j + 1])
        if amount then
            redis.call('HINCRBY', KEYS[1], 'available_credit', amount)
        end
    end

    redis.call('DEL', KEYS[4])
    redis.call('SETEX', KEYS[3], 86400, 'compensated')
    if skip_outbox ~= "1" then
        redis.call('XADD', KEYS[2], 'MAXLEN', '~', '10000', '*',
            'saga_id', saga_id, 'event', 'compensated')
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

redis.register_function('payment_2pc_prepare', payment_2pc_prepare)
redis.register_function('payment_2pc_commit', payment_2pc_commit)
redis.register_function('payment_2pc_abort', payment_2pc_abort)
redis.register_function('payment_saga_execute', payment_saga_execute)
redis.register_function('payment_saga_compensate', payment_saga_compensate)
redis.register_function('payment_subtract_direct', subtract_direct)
redis.register_function('payment_add_direct', add_direct)
