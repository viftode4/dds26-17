-- stock_subtract_direct: Atomic check-and-decrement for /subtract API endpoint
-- KEYS[1] = item:{item_id}
-- ARGV[1] = amount to subtract
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
