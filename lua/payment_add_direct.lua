-- payment_add_direct: Atomic increment for /add_funds API endpoint
-- KEYS[1] = user:{user_id}
-- ARGV[1] = amount to add
local amount = tonumber(ARGV[1])
local exists = redis.call('EXISTS', KEYS[1])
if exists == 0 then
    return redis.error_reply('USER_NOT_FOUND')
end
return redis.call('HINCRBY', KEYS[1], 'available_credit', amount)
