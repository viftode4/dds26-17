-- stock_add_direct: Atomic increment for /add API endpoint
-- KEYS[1] = item:{item_id}
-- ARGV[1] = amount to add
local amount = tonumber(ARGV[1])
local exists = redis.call('EXISTS', KEYS[1])
if exists == 0 then
    return redis.error_reply('ITEM_NOT_FOUND')
end
return redis.call('HINCRBY', KEYS[1], 'available_stock', amount)
