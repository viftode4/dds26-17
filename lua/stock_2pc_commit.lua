-- stock_2pc_commit: Finalize (deduction already happened in prepare)
-- KEYS[1..N]    = item:{item_id} hashes
-- KEYS[N+1..2N] = lock:2pc:{saga_id}:{item_id} keys
-- KEYS[2N+1]    = saga:{saga_id}:stock:status
-- ARGV: saga_id, n_items, (item_id, amount)...
local saga_id = ARGV[1]
local n_items = tonumber(ARGV[2])
local status_key = KEYS[2 * n_items + 1]

-- Idempotency
local status = redis.call('HGET', status_key, 'status')
if status == 'committed' then return 1 end

-- Safety: if prepare data was lost (Redis Sentinel failover during 2PC),
-- re-apply the stock deduction.
if status ~= 'prepared' then
    for i = 1, n_items do
        local item_key = KEYS[i]
        local item_id = ARGV[1 + i * 2]
        local amount = tonumber(ARGV[2 + i * 2])
        redis.call('HINCRBY', item_key, 'available_stock', -amount)
        redis.call('HSET', status_key, 'amount:' .. item_id, tostring(amount))
    end
end

-- Delete all lock keys
for i = 1, n_items do
    redis.call('DEL', KEYS[n_items + i])
end

redis.call('HSET', status_key, 'status', 'committed')
redis.call('EXPIRE', status_key, 86400)
return 1
