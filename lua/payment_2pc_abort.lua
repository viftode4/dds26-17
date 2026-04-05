-- payment_2pc_abort: Restore credit from status hash (undo prepare deduction)
-- KEYS[1] = user:{user_id}
-- KEYS[2] = lock:2pc:{saga_id}:{user_id}
-- KEYS[3] = saga:{saga_id}:payment:status
-- ARGV: saga_id
local saga_id = ARGV[1]

-- Idempotency
local status = redis.call('HGET', KEYS[3], 'status')
if status == 'aborted' then return 1 end

-- Restore credit from status hash (survives lock TTL expiry)
local amount = redis.call('HGET', KEYS[3], 'amount')
if amount then
    redis.call('HINCRBY', KEYS[1], 'available_credit', tonumber(amount))
end
redis.call('DEL', KEYS[2])

redis.call('HSET', KEYS[3], 'status', 'aborted')
redis.call('EXPIRE', KEYS[3], 86400)
return 1
