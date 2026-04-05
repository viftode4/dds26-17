-- payment_2pc_prepare: Validate + deduct credit atomically
-- KEYS[1] = user:{user_id}
-- KEYS[2] = lock:2pc:{saga_id}:{user_id}
-- KEYS[3] = saga:{saga_id}:payment:status
-- ARGV: amount, saga_id, user_id, lock_ttl
local amount = tonumber(ARGV[1])
local saga_id = ARGV[2]
local lock_ttl = tonumber(ARGV[4])

-- Idempotency + poison pill: refuse if already aborted/committed
local status = redis.call('HGET', KEYS[3], 'status')
if status == 'prepared' then return 1 end
if status == 'aborted' or status == 'committed' then return 0 end

-- Validate
local available = tonumber(redis.call('HGET', KEYS[1], 'available_credit'))
if not available or available < amount then
    return 0
end

-- Deduct credit and store amount durably
redis.call('HINCRBY', KEYS[1], 'available_credit', -amount)
redis.call('SETEX', KEYS[2], lock_ttl, tostring(amount))
redis.call('HSET', KEYS[3], 'amount', tostring(amount))

redis.call('HSET', KEYS[3], 'status', 'prepared')
redis.call('EXPIRE', KEYS[3], 86400)
return 1
