-- payment_2pc_commit: Finalize (deduction already happened in prepare)
-- KEYS[1] = user:{user_id}
-- KEYS[2] = lock:2pc:{saga_id}:{user_id}
-- KEYS[3] = saga:{saga_id}:payment:status
-- ARGV: saga_id, amount, user_id
local saga_id = ARGV[1]

-- Idempotency
local status = redis.call('HGET', KEYS[3], 'status')
if status == 'committed' then return 1 end

-- Safety: if prepare data was lost (Redis Sentinel failover during 2PC),
-- re-apply the credit deduction.
if status ~= 'prepared' then
    local amount = tonumber(ARGV[2])
    if amount and amount > 0 then
        redis.call('HINCRBY', KEYS[1], 'available_credit', -amount)
        redis.call('HSET', KEYS[3], 'amount', tostring(amount))
    end
end

-- Delete lock key
redis.call('DEL', KEYS[2])

redis.call('HSET', KEYS[3], 'status', 'committed')
redis.call('EXPIRE', KEYS[3], 86400)
return 1
