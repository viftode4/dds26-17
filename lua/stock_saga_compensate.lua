-- stock_saga_compensate: Restore stock from stored amounts
-- KEYS[1..N]   = item:{item_id} hashes
-- KEYS[N+1]    = saga:{saga_id}:stock:status
-- KEYS[N+2]    = saga:{saga_id}:stock:amounts
-- ARGV: saga_id, n_items
local saga_id = ARGV[1]
local n_items = tonumber(ARGV[2])
local status_key = KEYS[n_items + 1]
local amounts_key = KEYS[n_items + 2]

-- Idempotency
local status = redis.call('GET', status_key)
if status == 'compensated' then return 1 end

-- Restore stock from stored amounts
local all_amounts = redis.call('HGETALL', amounts_key)
for j = 1, #all_amounts, 2 do
    local item_id = all_amounts[j]
    local amount = tonumber(all_amounts[j + 1])
    if amount then
        redis.call('HINCRBY', 'item:' .. item_id, 'available_stock', amount)
    end
end

redis.call('DEL', amounts_key)
redis.call('SETEX', status_key, 86400, 'compensated')
return 1
