-- order_add_item: Atomically add an item to an order and update total_cost
-- KEYS[1] = order:{order_id}:items (List)
-- KEYS[2] = order:{order_id} (Hash)
-- ARGV[1] = item entry string (item_id:quantity)
-- ARGV[2] = cost_increment (quantity * price)
local exists = redis.call('EXISTS', KEYS[2])
if exists == 0 then
    return redis.error_reply('ORDER_NOT_FOUND')
end

redis.call('RPUSH', KEYS[1], ARGV[1])
redis.call('HINCRBY', KEYS[2], 'total_cost', tonumber(ARGV[2]))
return 1
