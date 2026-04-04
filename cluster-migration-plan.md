# Plan: Migrate from Redis Sentinel to Redis Cluster

## Context

The current architecture uses **Redis Sentinel** for high availability: one writeable master per service (order-db, stock-db, payment-db) with 2 read-only replicas and 3 Sentinel watchdogs. Sentinel provides HA (automatic failover) but **all writes funnel through a single master per service** — a throughput ceiling under heavy load.

**Redis Cluster** distributes data across multiple writeable master shards via keyslot routing (16,384 hash slots divided among masters). Each checkout's stock deductions for different items can hit different shards in parallel, and user credit deductions are naturally spread. This is the correct solution for improving write throughput in a distributed manner.

**Goal**: Replace Sentinel with per-service Redis Clusters (3 masters + 3 replicas each), update client code and Lua scripts to be cluster-compatible, and preserve all existing safety properties (atomic Lua, 2PC/Saga correctness, WAL durability).

---

## Theoretical Analysis: Sentinel vs Cluster

### Current bottleneck (Sentinel)
Every stock deduction, payment deduction, WAL write, idempotency SET, and leader lock SET goes to a **single master** per service. With 9 order instances × 3 Granian workers = 27 concurrent checkout orchestrators, all write pressure concentrates on one Redis process per service. Redis command execution (including Lua scripts) is single-threaded, so this is a hard serialization bottleneck.

### What Cluster provides
- **N writeable masters per service**: writes hash-routed across 3 shards. Each item's stock and each user's credit lives on exactly one shard. Different items in the same order land on different shards and their Lua executions proceed **in parallel**.
- **Write scaling math**: with 3 stock-cluster masters and uniform item distribution, each shard handles ~27/3 = 9 concurrent write processes on average instead of 27. Lua throughput scales with shard count.
- **Built-in HA**: cluster nodes elect a new master from their replicas when a primary fails — no Sentinel processes needed.

### Trade-offs and constraints

| Feature | Sentinel | Cluster | Notes |
|---------|----------|---------|-------|
| Write scalability | 1 master/service | N masters/service | **Primary motivation** |
| HA failover | Sentinel process | Built-in gossip election | Cluster nodes detect failure via `cluster-node-timeout` |
| Multi-key Lua (`fcall`) | Free | Keys must share hash slot | Requires hash tag `{tag}` redesign |
| `MULTI/EXEC` transactions | Free | Same-slot keys only | `pipeline(transaction=True)` constraints |
| `pipeline(transaction=False)` | Free | Free — cluster routes per key | Non-transactional pipelines work across slots |
| Pub/Sub (classic) | Works | Broadcast to all nodes (works, less efficient) | Use sharded pub/sub in Valkey 7+ |
| Streams (`XADD`) | Works | Single key → single slot | WAL stays on one shard (correct) |
| `EVAL`/`register_script` | Works | Single-key scripts work | Routes via KEYS[1] hash slot |
| Infrastructure | 3 masters + 6 replicas + 3 sentinels | 9 masters + 9 replicas, no sentinels | More Redis containers |

### Where writes actually land and how they scale

**Order-db**: WAL stream, `active_sagas` SET, `saga_state` hashes, idempotency keys, leader lock. These are write-heavy but their serial ordering is a correctness invariant (WAL must be ordered). Concentrating all WAL on one shard via hash tag is intentional, not a limitation.

**Stock-db**: `item:{item_id}` hashes. These are the primary write-scaling win — items naturally distribute across 3 shards by item_id hash. A checkout touching items 1, 2, 3 can execute their deductions **concurrently on 3 different shards**.

**Payment-db**: `user:{user_id}` hashes. Naturally distributed across 3 shards. Each user's credit lives on one shard.

---

## Critical Incompatibilities to Resolve

### 1. Multi-key Lua scripts in stock_lib.lua (BREAKING)

**2PC operations** (`stock_2pc_prepare`, `stock_2pc_commit`, `stock_2pc_abort`):
- Take `N` item keys + `N` lock keys + 1 status key = up to 7+ keys per call
- Items have different item_ids → different hash slots → `CROSSSLOT` error in cluster

**Saga operations** (`stock_saga_execute`):
- Takes `N` item keys + 1 status key + 1 amounts key = N+2 keys
- Same cross-slot problem

**`stock_saga_compensate`** — additional critical issue:
- After reading the amounts hash via `HGETALL`, it constructs item keys as a **string literal inside Lua** at line 190: `redis.call('HINCRBY', 'item:' .. item_id, 'available_stock', amount)`
- This hardcoded `'item:' .. item_id` format bypasses any hash-tag scheme entirely — even with per-item calls, if the Lua function reconstructs the key this way, it targets the wrong key name
- Must be rewritten to use hash-tagged key names passed as KEYS arguments

**Fix**: Rewrite all stock Lua functions as **single-item operations** called concurrently from Python via `asyncio.gather`. The 2PC protocol handles partial failures via compensation — atomic batch-per-order in Lua is an optimization, not a correctness requirement.

**New key naming** using `{item_{item_id}}` hash tag — forces item data, lock, and per-item status onto the same shard:
- `{item_{item_id}}:data` → item HASH (available_stock, price, reserved_stock)
- `{item_{item_id}}:lock:{saga_id}` → 2PC lock STRING with TTL
- `{item_{item_id}}:2pc-status:{saga_id}` → 2PC status HASH (status, amount)
- `{item_{item_id}}:amounts:{saga_id}` → saga amounts HASH (for compensate)
- `{item_{item_id}}:saga-status:{saga_id}` → saga status STRING (executed/compensated)

Note: 2PC status uses HSET (HASH) to store multiple fields; saga status uses SETEX (STRING). Different key suffixes (`2pc-status` vs `saga-status`) prevent type collisions on the same item.

**New single-item Lua function signatures**:
```lua
-- stock_2pc_prepare_one(KEYS[3], ARGV[4])
-- KEYS[1]={item_{id}}:data, KEYS[2]={item_{id}}:lock:{saga}, KEYS[3]={item_{id}}:2pc-status:{saga}
-- Returns: 1=success, 0=insufficient/aborted/committed

-- stock_2pc_commit_one(KEYS[3], ARGV[3])  -- same key layout
-- stock_2pc_abort_one(KEYS[3], ARGV[2])   -- same key layout

-- stock_saga_execute_one(KEYS[3], ARGV[3])
-- KEYS[1]={item_{id}}:data, KEYS[2]={item_{id}}:saga-status:{saga}, KEYS[3]={item_{id}}:amounts:{saga}

-- stock_saga_compensate_one(KEYS[3], ARGV[2])
-- Same key layout — KEYS[3] holds item_id→amount entry, writes HINCRBY on KEYS[1]
```

**Python call pattern** (stock/app.py):
```python
results = await asyncio.gather(*[
    db.fcall("stock_2pc_prepare_one", 3,
             f"{{item_{item_id}}}:data",
             f"{{item_{item_id}}}:lock:{saga_id}",
             f"{{item_{item_id}}}:2pc-status:{saga_id}",
             saga_id, item_id, str(amount), str(ttl))
    for item_id, amount in items
])
all_prepared = all(r == 1 for r in results)
```

### 2. Multi-key Lua scripts in payment_lib.lua (BREAKING — same severity as stock)

All payment Lua functions use 3 keys per call across different slots:
- **2PC**: `user:{user_id}`, `lock:2pc:{saga_id}:{user_id}`, `saga:{saga_id}:payment:status`
- **Saga**: `user:{user_id}`, `saga:{saga_id}:payment:status`, `saga:{saga_id}:payment:amounts`

`user:{user_id}` and `saga:{saga_id}:payment:status` are on different slots. **All 5 payment Lua functions are cross-slot incompatible** without key renaming.

**Fix**: Use `{user_{user_id}}` hash tag to co-locate all user-related keys:
- `{user_{user_id}}:data` → user HASH (available_credit)
- `{user_{user_id}}:lock:{saga_id}` → 2PC lock STRING with TTL
- `{user_{user_id}}:2pc-status:{saga_id}` → 2PC status HASH
- `{user_{user_id}}:saga-status:{saga_id}` → saga status STRING (executed/compensated)
- `{user_{user_id}}:amounts:{saga_id}` → saga amounts HASH

Since payment always deals with a single user per saga, all 3 keys per call share the `{user_{user_id}}` tag → same slot, no function signature changes needed (just key name changes in Python callers).

`payment_saga_compensate` reads amounts from KEYS[3] and writes to KEYS[1]. With the hash tag, both are on the same shard — the existing batch approach works. **No need to split into per-item calls** for payment (unlike stock, which has variable N items).

### 3. WAL cross-key transactions in orchestrator/wal.py

`WALEngine.log()` uses `pipeline(transaction=True)` with 3 keys:
- `saga-wal` (XADD to stream)
- `active_sagas` (SADD/SREM)
- `saga_state:{saga_id}` (HSET/DELETE)

These are on different slots. `MULTI/EXEC` in cluster requires same-slot keys.

Note: `log_terminal()` uses `pipeline(transaction=False)` — this is **fine** in cluster mode without any changes (cluster pipeline routes each command to its shard independently). Only `log()` needs fixing.

**Fix**: Force all WAL keys onto one shard with `{order-wal}` hash tag:
- `{order-wal}:saga-wal` → the WAL stream
- `{order-wal}:active_sagas` → active sagas SET
- `{order-wal}:state:{saga_id}` → saga state HASH

This concentrates WAL writes on one master shard. This is **correct by design** — the WAL's serial, ordered append semantics require single-shard concentration anyway. The write-scaling win for WAL is not the goal; item stock and user credit deductions are.

Update `WALEngine` constants:
```python
STREAM_KEY = "{order-wal}:saga-wal"
ACTIVE_SET  = "{order-wal}:active_sagas"
# state key format: f"{{order-wal}}:state:{saga_id}"
```

### 4. Leader election key in orchestrator/leader.py

`LOCK_KEY = "orchestrator:leader"` must become `"{order-wal}:leader"` so it co-locates with the WAL shard.

The `_RENEW_SCRIPT` and `_RELEASE_SCRIPT` use only `KEYS[1]` (single-key Lua). `RedisCluster.register_script()` routes `EVAL` to the correct shard based on `KEYS[1]`. These scripts work in cluster mode without modification beyond the key rename.

### 5. Order service checkout transaction

`order/app.py` line 404: `pipeline(transaction=True)` with:
- `idempotency:checkout:{order_id}` (SET with EX)
- `order:{order_id}` (HSET paid=true)

With hash tag `{order_{order_id}}`, both become `{order_{order_id}}:idempotency:checkout` and `{order_{order_id}}:data` — same slot, transaction valid. ✓

### 6. `order_load_and_claim` Lua (order_lib.lua)

`order_load_and_claim` touches 3 keys: `order:{id}`, `order:{id}:items`, `idempotency:checkout:{id}`.

**Fix**: Use `{order_{order_id}}` hash tag for all order keys:
- `{order_{order_id}}:data` → order HASH
- `{order_{order_id}}:items` → items LIST
- `{order_{order_id}}:idempotency:checkout` → idempotency STRING

`order_add_item` touches 2 keys: `order:{id}:items` and `order:{id}`. With hash tags: `{order_{order_id}}:items` and `{order_{order_id}}:data` — same slot. ✓

### 7. Pub/Sub for saga result delivery

`orchestrator/core.py _publish_result()` uses `pipeline(transaction=False)` with `set(result_key)` + `publish(channel)`. Non-transactional pipelines are fine in cluster. Classic `PUBLISH` broadcasts to all cluster nodes — subscribers on any node receive it. **Works in cluster without modification.**

`common/result.py wait_for_result()` uses `db.pubsub()` + `subscribe(channel)`. In `RedisCluster`, `pubsub()` creates a `ClusterPubSub` that subscribes to the shard owning that channel's slot. This is transparent to the caller. **Works without modification.**

**Optional upgrade**: Use sharded pub/sub (`SSUBSCRIBE`/`SPUBLISH`) for efficiency. If upgrading: channel names would use `{saga_id}:notify` (slot-aligned with `saga-result:{saga_id}`), and callers use `db.pubsub(sharded=True)` + `ssubscribe`. Sharded pub/sub limits message routing to one shard instead of broadcasting. Defer this unless classic pub/sub causes measurable overhead.

### 8. Sentinel failover subscription (REMOVE ENTIRELY)

`common/config.py subscribe_failover_invalidation()` subscribes to a Sentinel node's `+switch-master` pub/sub events and calls `connection_pool.disconnect()` on failover.

**Remove entirely.** `RedisCluster` handles topology changes automatically: it processes `MOVED` and `ASK` redirects, re-fetches cluster topology on `ClusterDownError`, and transparently reconnects. No manual pool invalidation is needed or possible (there's no single pool — there's one pool per cluster node).

Remove:
- `subscribe_failover_invalidation()` from `common/config.py`
- All call sites: `await subscribe_failover_invalidation(...)` in `order/app.py` line 168, `stock/app.py` line 58, `payment/app.py` line 56
- Shutdown cancellation code: `if failover_task: failover_task.cancel()` in all three service lifespans
- The `get_sentinel_hosts()` function (no longer needed)
- The Sentinel import `from redis.asyncio.sentinel import Sentinel`

### 9. Client creation (core change in common/config.py)

Replace the `Sentinel.master_for()` / `Sentinel.slave_for()` pattern with `RedisCluster`.

**Key behavioral change**: The current code maintains two separate client instances — `db` (master writes) and `db_read` (replica reads) — with a `try db, except try db_read` fallback pattern in `find_order` and `find_item`. In cluster mode, this fallback is unnecessary — `RedisCluster` handles `MOVED`/`ASK` redirects internally and a single client with `read_from_replicas=True` routes reads to replicas automatically.

**New connection factory**:
```python
from redis.asyncio.cluster import RedisCluster, ClusterNode

def create_redis_cluster_connection(
    startup_nodes: list[tuple[str, int]],
    pool_size: int = 512,
    read_from_replicas: bool = False,
    **kwargs,
) -> RedisCluster:
    default_kwargs = {
        "socket_timeout": 5,
        "socket_connect_timeout": 5,
        "health_check_interval": 30,
        "retry_on_error": [],   # preserve existing intentional omission
        "decode_responses": True,
    }
    merged = {**default_kwargs, **kwargs}
    nodes = [ClusterNode(host, port) for host, port in startup_nodes]
    return RedisCluster(
        startup_nodes=nodes,
        max_connections=pool_size,      # per-node pool size
        read_from_replicas=read_from_replicas,
        password=os.environ.get("REDIS_PASSWORD", "redis"),
        **merged,
    )
```

**Connection pool note**: In `RedisCluster`, `max_connections` is the pool size **per cluster node**. Each process maintains up to `max_connections` connections to each node. With 3 masters, total possible connections from one process = `3 × max_connections`. However, connections are allocated lazily — with keyslot distribution across 3 shards, only ~1/3 of `max_connections` are typically used per node. For `maxclients` planning: `n_instances × n_workers × max_connections ÷ n_shards` gives a realistic per-shard connection count.

**Service simplification**: Replace `db` + `db_read` with a single cluster client (or keep two for explicit read/write separation):
```python
# Option A: single client, reads to replicas
db = create_redis_cluster_connection(startup_nodes, pool_size, read_from_replicas=True)

# Option B: keep two clients (explicit separation, matches current code structure)
db = create_redis_cluster_connection(startup_nodes, pool_size, read_from_replicas=False)
db_read = create_redis_cluster_connection(startup_nodes, pool_size//2, read_from_replicas=True)
```

The `try db, except try db_read` fallback in `find_order`/`find_item` can be removed — cluster handles failures internally.

**Environment variables** (new format):
```
# Order service: env/order_redis.env
ORDER_CLUSTER_NODES=order-cluster-1:6379,order-cluster-2:6379,order-cluster-3:6379
REDIS_PASSWORD=redis
REDIS_DB=0
REDIS_MASTER_POOL_SIZE=512
REDIS_REPLICA_POOL_SIZE=256

# Note: STOCK_REDIS_* and PAYMENT_REDIS_* vars in the current order_redis.env
# are NOT used by order/app.py (order communicates with stock/payment via NATS only).
# Remove these dead variables.
```

```
# Stock service: env/stock_redis.env
STOCK_CLUSTER_NODES=stock-cluster-1:6379,stock-cluster-2:6379,stock-cluster-3:6379
REDIS_PASSWORD=redis
REDIS_DB=0
REDIS_MASTER_POOL_SIZE=512
REDIS_REPLICA_POOL_SIZE=256
```

```
# Payment service: env/payment_redis.env
PAYMENT_CLUSTER_NODES=payment-cluster-1:6379,payment-cluster-2:6379,payment-cluster-3:6379
REDIS_PASSWORD=redis
REDIS_DB=0
REDIS_MASTER_POOL_SIZE=512
REDIS_REPLICA_POOL_SIZE=256
```

### 10. Lua function loading in cluster mode (CRITICAL)

`await db.function_load(lua_code, replace=True)` at service startup sends `FUNCTION LOAD` to **one node only** by default. In cluster mode, Lua functions must be present on **every master** since any `FCALL` is routed to the shard owning KEYS[1].

**Fix**:
```python
from redis.asyncio.cluster import RedisCluster
await db.function_load(lua_code, replace=True, target_nodes=RedisCluster.ALL_PRIMARIES)
```

This must be updated in `order/app.py` (order_lib), `stock/app.py` (stock_lib), and `payment/app.py` (payment_lib) lifespan startup code.

---

## Infrastructure Changes

### Cluster topology (per service)

Replace: 1 master + 2 replicas + 3 sentinels → 3 masters + 3 replicas, no sentinels

Container naming:
- `order-cluster-1`, `order-cluster-2`, `order-cluster-3` — order masters
- `order-cluster-replica-1`, `order-cluster-replica-2`, `order-cluster-replica-3`
- Same pattern for stock-cluster and payment-cluster

**Container count**: was 3+6+3=12 Redis containers, becomes 9+9=18 (no sentinels).

### Redis cluster node configuration

Each node runs with:
```
cluster-enabled yes
cluster-config-file /data/nodes.conf
cluster-node-timeout 5000          # ms — analogous to Sentinel down-after-milliseconds
cluster-announce-ip <resolved_ip>  # Docker container IP, resolved at startup
cluster-announce-port 6379
appendonly yes
appendfsync everysec
requirepass redis
masterauth redis
maxmemory <Xmb>
maxclients <N>
io-threads <T>
```

`cluster-announce-ip` must be the container's actual IP (not hostname) so other cluster nodes can reach it when the DNS entry may be stale. Resolved at startup via entrypoint script.

### New entrypoint: `cluster-node-entrypoint.sh`

```sh
#!/bin/sh
# Resolve container's own IP for cluster-announce-ip
SELF_IP=$(hostname -i | awk '{print $1}')
# Write cluster config
cat > /tmp/cluster.conf <<EOF
cluster-enabled yes
cluster-config-file /data/nodes.conf
cluster-node-timeout 5000
cluster-announce-ip ${SELF_IP}
cluster-announce-port 6379
appendonly yes
appendfsync everysec
requirepass redis
masterauth redis
maxmemory ${MAXMEMORY:-200mb}
maxclients ${MAXCLIENTS:-4096}
io-threads ${IO_THREADS:-2}
io-threads-do-reads yes
EOF
exec redis-server /tmp/cluster.conf
```

### Cluster init service: `cluster-init`

A one-shot service that runs after all cluster nodes are healthy, forms 3 separate clusters (one per service):

```sh
#!/bin/sh
# Wait for nodes to be ready
for host in order-cluster-1 order-cluster-2 order-cluster-3 \
            order-cluster-replica-1 order-cluster-replica-2 order-cluster-replica-3; do
    until redis-cli -a redis -h $host ping 2>/dev/null | grep -q PONG; do sleep 1; done
done

# Form order cluster (3 masters + 3 replicas, 1 replica per master)
redis-cli -a redis --no-auth-warning --cluster create \
  $(getent hosts order-cluster-1 | awk '{print $1}'):6379 \
  $(getent hosts order-cluster-2 | awk '{print $1}'):6379 \
  $(getent hosts order-cluster-3 | awk '{print $1}'):6379 \
  $(getent hosts order-cluster-replica-1 | awk '{print $1}'):6379 \
  $(getent hosts order-cluster-replica-2 | awk '{print $1}'):6379 \
  $(getent hosts order-cluster-replica-3 | awk '{print $1}'):6379 \
  --cluster-replicas 1 --cluster-yes

# Repeat for stock-cluster and payment-cluster (same pattern)
```

Application services depend on `cluster-init: condition: service_completed_successfully`.

### HAProxy — no changes

HAProxy routes HTTP to application service instances (order-service, stock-service, payment-service). The Redis topology change is completely transparent to HAProxy.

### Docker Compose changes per config

Remove: all `sentinel-{1,2,3}` services, all `*-db-replica*` services
Add: `order-cluster-{1-3}`, `order-cluster-replica-{1-3}`, same for stock/payment, plus `cluster-init`
Update: `gateway depends_on` removes sentinel deps, application services depend on `cluster-init`

**CPU budget impact** (approximate, for medium config):
- Removed: 3 sentinels × 0.3 = 0.9 CPU + 6 replicas × 0.6 = 3.6 CPU = 4.5 CPU freed
- Added: 6 new cluster nodes × 0.5 = 3.0 CPU (order cluster nodes at 0.75 each + stock/payment at 0.5 each = ~3.5)
- Net: roughly neutral, slight increase. The freed sentinel CPU is partially offset by additional cluster nodes. Adjust app service CPU limits accordingly.

---

## File-by-File Change Summary

### `common/config.py`
- **Remove**: `from redis.asyncio.sentinel import Sentinel`, `get_sentinel_hosts()`, `create_redis_connection()` (Sentinel version), `create_replica_connection()`, `subscribe_failover_invalidation()`
- **Add**: `from redis.asyncio.cluster import RedisCluster, ClusterNode`, `create_redis_cluster_connection(startup_nodes, pool_size, read_from_replicas)`
- **Parse**: `*_CLUSTER_NODES` env var (comma-separated `host:port` list) instead of `SENTINEL_HOSTS` + `REDIS_HOST`
- **Keep**: `wait_for_redis()` — `db.ping()` works on `RedisCluster` (pings one primary node)

### `orchestrator/wal.py`
- Update class constants:
  ```python
  STREAM_KEY = "{order-wal}:saga-wal"
  ACTIVE_SET  = "{order-wal}:active_sagas"
  # state hash: f"{{order-wal}}:state:{saga_id}"
  ```
- `log()` uses `pipeline(transaction=True)` — all 3 keys now share `{order-wal}` slot → valid ✓
- `log_terminal()` uses `pipeline(transaction=False)` — no slot constraint, already fine ✓
- `get_incomplete_sagas()` uses `sscan` + `hgetall` — single-key ops, no constraint ✓

### `orchestrator/leader.py`
- Update `LOCK_KEY = "{order-wal}:leader"`
- Lua scripts unchanged (single-key operations) — cluster routes `EVAL` via `KEYS[1]` ✓

### `orchestrator/core.py`
- `_publish_result()`: uses `pipeline(transaction=False)` — works as-is in cluster ✓
- No changes needed for pub/sub (classic pub/sub works in cluster)

### `common/result.py`
- `wait_for_result()`: `db.pubsub()` on `RedisCluster` creates `ClusterPubSub` — works as-is ✓
- No changes needed (unless upgrading to sharded pub/sub later as optimization)

### `lua/order_lib.lua`
- Update `order_add_item` KEYS comments: `{order_{order_id}}:items` (LIST), `{order_{order_id}}:data` (HASH)
- Update `order_load_and_claim` KEYS comments: `{order_{order_id}}:data`, `{order_{order_id}}:items`, `{order_{order_id}}:idempotency:checkout`
- Internal Lua logic unchanged — same Redis commands, just different key names from caller

### `lua/stock_lib.lua` (Major rewrite)
- Replace all 7 functions with single-item versions
- New key naming convention with `{item_{item_id}}` hash tag
- Remove hardcoded `'item:' .. item_id` string construction in `stock_saga_compensate` (line 190) — must receive key names as KEYS arguments
- Registered function names: `stock_2pc_prepare_one`, `stock_2pc_commit_one`, `stock_2pc_abort_one`, `stock_saga_execute_one`, `stock_saga_compensate_one`, `stock_subtract_direct` (unchanged), `stock_add_direct` (unchanged)
- Note: `subtract_direct` and `add_direct` already use single KEYS[1] — only need key name update at call site

### `lua/payment_lib.lua`
- All functions keep their batch-per-payment signature (single user per call — no split needed)
- Update key names in Lua: `KEYS[1]` becomes `{user_{user_id}}:data`, `KEYS[2]` becomes `{user_{user_id}}:lock:{saga_id}` or `{user_{user_id}}:2pc-status:{saga_id}`, `KEYS[3]` becomes `{user_{user_id}}:2pc-status:{saga_id}` or `{user_{user_id}}:amounts:{saga_id}` depending on function
- Keys are passed from Python callers — internal Lua logic unchanged except comments
- Registered function names unchanged

### `order/app.py`
- **Remove**: `subscribe_failover_invalidation` import and all usage (lifespan lines 168-175)
- **Remove**: `create_replica_connection` import (replace with single cluster client or keep as second cluster client)
- **Update**: `create_redis_connection(...)` calls → `create_redis_cluster_connection(...)` with startup_nodes parsed from env
- **Update**: all key construction:
  - `f"order:{order_id}"` → `f"{{order_{order_id}}}:data"`
  - `f"order:{order_id}:items"` → `f"{{order_{order_id}}}:items"`
  - `f"idempotency:checkout:{order_id}"` → `f"{{order_{order_id}}}:idempotency:checkout"`
- **Update**: `function_load` call → add `target_nodes=RedisCluster.ALL_PRIMARIES`
- **Update**: `batch_init_users` key construction (same renames)
- **Simplify**: `find_order`'s `try db, except try db_read` fallback can be removed (cluster handles internally)
- **Pipeline OK**: `pipeline(transaction=True)` checkout transaction with `{order_{order_id}}` tag keys is now same-slot ✓

### `stock/app.py`
- **Remove**: `subscribe_failover_invalidation` import and usage
- **Update**: `create_redis_connection` → cluster factory
- **Update**: `function_load` → `target_nodes=RedisCluster.ALL_PRIMARIES`
- **Rewrite** all 5 internal functions (`_2pc_prepare`, `_2pc_commit`, `_2pc_abort`, `_saga_execute`, `_saga_compensate`) to use per-item `asyncio.gather` pattern with hash-tagged keys
- **Update**: `find_item`, `add_stock`, `remove_stock` key construction: `f"item:{item_id}"` → `f"{{item_{item_id}}}:data"`
- **Update**: `batch_init_users` key construction
- **Simplify**: `find_item` fallback to db_read can be removed

### `payment/app.py`
- **Remove**: `subscribe_failover_invalidation` import and usage
- **Update**: `create_redis_connection` → cluster factory
- **Update**: `function_load` → `target_nodes=RedisCluster.ALL_PRIMARIES`
- **Update**: `_2pc_prepare`, `_2pc_commit`, `_2pc_abort`, `_saga_execute`, `_saga_compensate` key construction to use `{user_{user_id}}` tags
- **Update**: `create_user`, HTTP endpoint key construction: `f"user:{user_id}"` → `f"{{user_{user_id}}}:data"`
- **Simplify**: find endpoint fallback can be removed

### `env/order_redis.env`
```
ORDER_CLUSTER_NODES=order-cluster-1:6379,order-cluster-2:6379,order-cluster-3:6379
REDIS_PASSWORD=redis
REDIS_DB=0
REDIS_MASTER_POOL_SIZE=512
REDIS_REPLICA_POOL_SIZE=256
```
Remove all `SENTINEL_HOSTS`, `REDIS_SENTINEL_SERVICE`, `REDIS_HOST`, `REDIS_PORT`, and the dead `STOCK_REDIS_*`/`PAYMENT_REDIS_*` variables (these are unused by current `order/app.py` — order communicates with stock/payment via NATS, not direct Redis).

### `env/stock_redis.env`, `env/payment_redis.env`
Same restructure: replace Sentinel vars with `*_CLUSTER_NODES`.

### `sentinel-entrypoint.sh`
**Delete** — replaced by `cluster-node-entrypoint.sh` and `cluster-init.sh`.

---

## Test Changes

### `test/topology.py` (Full rewrite)
Currently deeply Sentinel-specific: `MASTER_MAP`, `SENTINEL_CONTAINERS`, `get_sentinel_master_ip()`, `restore_topology()` with `SENTINEL REMOVE`/`MONITOR` commands.

New cluster version:
- **Replace** `get_sentinel_master_ip()` with `get_cluster_primary_for_slot()` — queries `CLUSTER NODES` from any cluster node to determine which master owns a given slot
- **Replace** `is_topology_drifted()` — checks `CLUSTER INFO` for `cluster_state:ok` and verifies expected primary count (`cluster_size:3` per service cluster)
- **Replace** `restore_topology()` — in cluster mode, topology restores automatically after a killed primary is restarted; the replica that was promoted stays as primary. Use `redis-cli --cluster fix` for any broken slots
- **Replace** `SENTINEL_CONTAINERS` with `CLUSTER_CONTAINERS` = `["order-cluster-1..3", "order-cluster-replica-1..3", ...]`
- **Replace** `restart_app_services()` — still needed but now checks cluster health via `CLUSTER INFO` instead of Sentinel state
- **Keep**: `wait_stack_healthy()`, `docker_compose()` helpers — unchanged

### `test/conftest.py`
- **Update** `_flush_databases_between_integration_tests`: flush all cluster node containers
  ```python
  db_containers = [
      ("order-cluster-1",   "redis-cli", "-a", "redis", "--no-auth-warning", "FLUSHALL"),
      ("order-cluster-2",   "redis-cli", "-a", "redis", "--no-auth-warning", "FLUSHALL"),
      ("order-cluster-3",   "redis-cli", "-a", "redis", "--no-auth-warning", "FLUSHALL"),
      # replicas are read-only, FLUSHALL would fail (silently ignored)
      # ... repeat for stock-cluster and payment-cluster
  ]
  ```
- **Update** `_ensure_clean_topology`: remove `is_topology_drifted`/`restore_topology` Sentinel calls, replace with `cluster_state:ok` health check

### `test/test_sentinel_failover.py` → `test/test_cluster_failover.py`
- Remove Sentinel failover simulation (`SENTINEL FAILOVER` command)
- Add cluster failover test: kill one cluster master container (`docker compose stop order-cluster-1`), wait for replica promotion (`CLUSTER INFO` shows `cluster_state:ok` again), verify requests succeed
- Remove all `subscribe_failover_invalidation` assertions

### Unit tests (`test_wal_metrics.py`, `test_executor.py`, `test_orchestrator_core.py`)
- Update key name assertions to use new hash-tagged names: `"saga-wal"` → `"{order-wal}:saga-wal"`, etc.
- Mock Redis pattern stays the same (AsyncMock) — the mock doesn't care about cluster topology

### `test/test_crash_recovery.py`, `test/test_recovery.py`
- Update WAL key name assertions to hash-tagged names
- Verify saga recovery still works after cluster master failover (not just process crash)

---

## Migration Steps (Implementation Order)

1. **Key schema changes** — Update WAL, leader, order, stock, payment key constants. Run unit tests.
2. **Rewrite stock Lua** — New single-item functions with hash tags, fix hardcoded `item:` string. Test with `redis-cli FCALL` on a single-node Redis first.
3. **Update payment Lua** — Key renames only, no signature changes.
4. **Update order Lua** — Key renames only.
5. **Update `common/config.py`** — New cluster client factory. Keep sentinel factory behind `REDIS_MODE=cluster|sentinel` env var during transition.
6. **Update all three service files** — Remove sentinel failover subscription, update key construction, rewrite stock fcall callers, add `ALL_PRIMARIES` to function_load.
7. **Write cluster entrypoint and init scripts** — `cluster-node-entrypoint.sh`, `cluster-init.sh`.
8. **Update Docker Compose (small config first)** — Swap Sentinel + replicas for cluster containers + cluster-init service.
9. **Update env files** — New cluster node vars.
10. **Update tests** — Rewrite topology.py, update conftest.py flush, rename test_sentinel_failover.py.
11. **Smoke test** — Bring up small config, verify cluster forms, verify a checkout succeeds end-to-end.
12. **Remove Sentinel artifacts** — Delete sentinel-entrypoint.sh, remove `REDIS_MODE` flag, clean up all Sentinel env vars.

---

## Verification

1. **Cluster formation**: `redis-cli -c -a redis -h order-cluster-1 cluster info | grep cluster_state` → `cluster_state:ok`, `cluster_slots_assigned:16384`, `cluster_known_nodes:6`

2. **Key slot distribution** (confirms hash tags work):
   ```
   redis-cli -c -a redis -h order-cluster-1 cluster keyslot "{order-wal}:saga-wal"
   redis-cli -c -a redis -h order-cluster-1 cluster keyslot "{order-wal}:active_sagas"
   # Both return the same slot number ✓

   redis-cli -c -a redis -h stock-cluster-1 cluster keyslot "{item_abc}:data"
   redis-cli -c -a redis -h stock-cluster-1 cluster keyslot "{item_abc}:lock:saga1"
   # Both return the same slot number ✓
   ```

3. **Lua function availability** (confirm loaded on all primaries):
   ```
   redis-cli -a redis -h order-cluster-1 FUNCTION LIST LIBRARYNAME order_lib
   redis-cli -a redis -h order-cluster-2 FUNCTION LIST LIBRARYNAME order_lib
   redis-cli -a redis -h order-cluster-3 FUNCTION LIST LIBRARYNAME order_lib
   # All three should return the library ✓
   ```

4. **Write distribution**: under load, compare `redis-cli -h order-cluster-{1,2,3} -a redis info stats | grep total_commands_processed` — commands should distribute across masters roughly equally.

5. **Unit tests**: `pytest test/ -v` — all existing unit tests pass with updated key names.

6. **End-to-end checkout**: `curl -X POST http://localhost:8000/orders/checkout/{order_id}` returns success. Verify via `docker logs` that Lua is executing on different shards for different items.

7. **Failover test**: `docker compose stop order-cluster-1`, wait 10s, issue checkout → succeeds. `docker compose start order-cluster-1`, verify it rejoins cluster as replica.

8. **Performance comparison**: Run `locust` against sentinel baseline and cluster config at same concurrency level. Expect higher throughput (requests/sec) at peak load due to parallel shard writes. Latency at low concurrency should be similar (one more network hop for cluster routing).
