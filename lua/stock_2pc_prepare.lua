-- stock_2pc_prepare: Validate + deduct stock atomically
-- KEYS[1..N]    = item:{item_id} hashes
-- KEYS[N+1..2N] = lock:2pc:{saga_id}:{item_id} keys
-- KEYS[2N+1]    = saga:{saga_id}:stock:status
-- ARGV: saga_id, lock_ttl, (item_id, amount)...
local saga_id = ARGV[1]
local lock_ttl = tonumber(ARGV[2])
local n_items = (#KEYS - 1) / 2
local status_key = KEYS[2 * n_items + 1]

-- Idempotency + poison pill: if abort already ran, refuse to prepare.
local status = redis.call('HGET', status_key, 'status')
if status == 'prepared' then return 1 end
if status == 'aborted' or status == 'committed' then return 0 end

-- Validate ALL items have sufficient available_stock
for i = 1, n_items do
    local item_key = KEYS[i]
    local amount = tonumber(ARGV[2 + i * 2])
    local available = tonumber(redis.call('HGET', item_key, 'available_stock'))
    if not available or available < amount then
        return 0
    end
end

-- Deduct stock and store amounts durably
for i = 1, n_items do
    local item_key = KEYS[i]
    local lock_key = KEYS[n_items + i]
    local item_id = ARGV[1 + i * 2]
    local amount = tonumber(ARGV[2 + i * 2])
    redis.call('HINCRBY', item_key, 'available_stock', -amount)
    redis.call('SETEX', lock_key, lock_ttl, tostring(amount))
    redis.call('HSET', status_key, 'amount:' .. item_id, tostring(amount))
end

redis.call('HSET', status_key, 'status', 'prepared')
redis.call('EXPIRE', status_key, 86400)
return 1
