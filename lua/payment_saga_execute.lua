-- payment_saga_execute: Direct deduction (no locks, no held_credit)
-- KEYS[1] = user:{user_id}
-- KEYS[2] = saga:{saga_id}:payment:status
-- KEYS[3] = saga:{saga_id}:payment:amounts
-- ARGV: amount, saga_id, user_id
local amount = tonumber(ARGV[1])
local saga_id = ARGV[2]
local user_id = ARGV[3]

-- Idempotency + poison pill: refuse if already compensated
local status = redis.call('GET', KEYS[2])
if status == 'executed' then return 1 end
if status == 'compensated' then return 0 end

-- Validate
local available = tonumber(redis.call('HGET', KEYS[1], 'available_credit'))
if not available or available < amount then
    return 0
end

redis.call('HINCRBY', KEYS[1], 'available_credit', -amount)
redis.call('HSET', KEYS[3], user_id, tostring(amount))
redis.call('EXPIRE', KEYS[3], 86400)

redis.call('SETEX', KEYS[2], 86400, 'executed')
return 1
