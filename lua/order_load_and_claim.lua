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

-- Claim idempotency key: acquire if absent or previously failed;
-- leave alone if a saga is actively processing (avoid stealing in-flight work)
-- or if the order already succeeded (success is permanent).
local existing = redis.call('GET', idempotency_key)
local acquired
if existing then
    local ok, parsed = pcall(cjson.decode, existing)
    if ok and type(parsed) == 'table' and parsed.status == 'failed' then
        -- Previous attempt failed — allow re-acquisition so retries can proceed
        redis.call('SET', idempotency_key, claim_value, 'EX', ttl)
        acquired = 1
    else
        -- 'processing' (in-flight) or 'success' (permanent) — do not acquire
        acquired = 0
    end
else
    redis.call('SET', idempotency_key, claim_value, 'EX', ttl)
    acquired = 1
end

return {1, cjson.encode(entry), cjson.encode(items), acquired}
