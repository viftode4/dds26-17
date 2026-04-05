-- stock_saga_execute: Direct deduction (no locks, no reserved_stock)
-- KEYS[1..N]   = item:{item_id} hashes
-- KEYS[N+1]    = saga:{saga_id}:stock:status
-- KEYS[N+2]    = saga:{saga_id}:stock:amounts
-- ARGV: saga_id, (item_id, amount)...
local saga_id = ARGV[1]
local n_items = #KEYS - 2
local status_key = KEYS[n_items + 1]
local amounts_key = KEYS[n_items + 2]

-- Idempotency + poison pill: refuse if already compensated
local status = redis.call('GET', status_key)
if status == 'executed' then return 1 end
if status == 'compensated' then return 0 end

-- Validate ALL items
for i = 1, n_items do
    local item_key = KEYS[i]
    local amount = tonumber(ARGV[1 + i * 2])
    local available = tonumber(redis.call('HGET', item_key, 'available_stock'))
    if not available or available < amount then
        return 0
    end
end

-- Apply deductions and store amounts for compensation
for i = 1, n_items do
    local item_key = KEYS[i]
    local item_id = ARGV[i * 2]
    local amount = tonumber(ARGV[1 + i * 2])
    redis.call('HINCRBY', item_key, 'available_stock', -amount)
    redis.call('HSET', amounts_key, item_id, tostring(amount))
end

redis.call('SETEX', status_key, 86400, 'executed')
redis.call('EXPIRE', amounts_key, 86400)
return 1
