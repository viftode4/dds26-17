-- stock_2pc_abort: Restore stock from status hash (undo prepare deduction)
-- KEYS[1..N]    = item:{item_id} hashes
-- KEYS[N+1..2N] = lock:2pc:{saga_id}:{item_id} keys
-- KEYS[2N+1]    = saga:{saga_id}:stock:status
-- ARGV: saga_id, n_items, item_id_1, item_id_2, ...
local saga_id = ARGV[1]
local n_items = tonumber(ARGV[2])
local status_key = KEYS[2 * n_items + 1]

-- Idempotency
local status = redis.call('HGET', status_key, 'status')
if status == 'aborted' then return 1 end

-- Restore stock from status hash (survives lock TTL expiry)
for i = 1, n_items do
    local item_key = KEYS[i]
    local lock_key = KEYS[n_items + i]
    local item_id = ARGV[2 + i]
    local amount = redis.call('HGET', status_key, 'amount:' .. item_id)
    if amount then
        redis.call('HINCRBY', item_key, 'available_stock', tonumber(amount))
    end
    redis.call('DEL', lock_key)
end

redis.call('HSET', status_key, 'status', 'aborted')
redis.call('EXPIRE', status_key, 86400)
return 1
