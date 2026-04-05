-- order_load_and_claim: Load order + items + claim idempotency key in a single call
-- KEYS[1] = order:{order_id} (Hash)
-- KEYS[2] = order:{order_id}:items (List)
-- KEYS[3] = idempotency:checkout:{order_id}
-- ARGV[1] = claim_value (JSON string)
-- ARGV[2] = ttl (seconds)
-- Returns: {found, entry_flat_json, items_json, acquired}
local order_key = KEYS[1]
local items_key = KEYS[2]
local idempotency_key = KEYS[3]
local claim_value = ARGV[1]
local ttl = tonumber(ARGV[2])

-- Load order hash
local entry = redis.call('HGETALL', order_key)
if #entry == 0 then
    return {0, '', '[]', 0}  -- not found
end

-- Load items list
local items = redis.call('LRANGE', items_key, 0, -1)

-- Attempt SET NX (idempotency claim)
local acquired = redis.call('SET', idempotency_key, claim_value, 'NX', 'EX', ttl)

return {1, cjson.encode(entry), cjson.encode(items), acquired and 1 or 0}
