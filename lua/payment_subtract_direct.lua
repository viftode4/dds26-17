-- payment_subtract_direct: Atomic check-and-decrement for /pay API endpoint
-- KEYS[1] = user:{user_id}
-- ARGV[1] = amount to subtract
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
