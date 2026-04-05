-- payment_saga_compensate: Restore credit from stored amounts
-- KEYS[1] = user:{user_id}
-- KEYS[2] = saga:{saga_id}:payment:status
-- KEYS[3] = saga:{saga_id}:payment:amounts
-- ARGV: saga_id
local saga_id = ARGV[1]

-- Idempotency
local status = redis.call('GET', KEYS[2])
if status == 'compensated' then return 1 end

-- Restore credit from stored amounts
local all_amounts = redis.call('HGETALL', KEYS[3])
for j = 1, #all_amounts, 2 do
    local amount = tonumber(all_amounts[j + 1])
    if amount then
        redis.call('HINCRBY', KEYS[1], 'available_credit', amount)
    end
end

redis.call('DEL', KEYS[3])
redis.call('SETEX', KEYS[2], 86400, 'compensated')
return 1
