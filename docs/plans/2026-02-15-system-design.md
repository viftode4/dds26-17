# System Design: Redis-Native Durable Saga Orchestrator

**Project:** Distributed Data Systems (DDS25) — Microservices Transaction Coordination
**Date:** 2026-02-15
**Status:** Approved
**Team:** TBD

---

## 1. Executive Summary

We present a **Redis-native durable saga orchestrator** — an event-driven microservices architecture that achieves serializable consistency within services, read-committed isolation across services, and crash-recoverable fault tolerance, all built exclusively on Python and Redis.

### Novel Contributions

Our architecture synthesizes techniques from five distinct areas of distributed systems research into a cohesive, production-grade design:

| Technique | Origin | Our Application |
|-----------|--------|-----------------|
| **Orchestrated Saga with TCC** | Garcia-Molina & Salem [1], Helland [2] | Cross-service checkout transactions with reservation-based isolation |
| **Deterministic Transaction Ordering** | Thomson et al. (Calvin) [4] | Partitioned checkout-log streams eliminate distributed locking |
| **Transactional Outbox** | Microservices.io pattern | Lua scripts atomically write state + events (solves dual-write problem) |
| **Durable Execution** | Uber Cadence / Temporal pattern | WAL-based crash recovery with deterministic replay |
| **Staged Event-Driven Architecture** | Welsh et al. (SEDA) [7] | Redis Streams with consumer groups provide per-stage backpressure |

### Key Properties

- **Consistency:** No lost money or items under any concurrency level (formally justified via I-confluence analysis [5])
- **Performance:** ~600-800 checkouts/sec throughput with <50ms p95 latency
- **Fault tolerance:** Survives any single container kill (including mid-transaction) with automatic recovery
- **Event-driven:** Fully asynchronous inter-service communication via Redis Streams
- **Scalable:** Horizontally scalable via consumer groups and partitioned orchestration

---

## 2. Theoretical Foundations

### 2.1 Why Coordination is Required

Per Bailis et al. [5], an operation can execute without coordination if and only if it satisfies **invariant confluence (I-confluence)**: the merge of any two valid database states must also satisfy the application's invariants.

Our system's invariants are:
- `stock >= 0` for all items
- `credit >= 0` for all users
- Conservation: `sum(credits) + sum(pending_charges) = constant`

These constraints are **not I-confluent** — two independent replicas could both decrement stock to 0, and merging yields negative stock. Therefore, stock decrement and payment operations **require coordination** [5, Theorem 1].

This formally justifies our use of a saga orchestrator: coordination-free approaches (CRDTs, eventual consistency alone) cannot satisfy these invariants.

### 2.2 Saga Theory

The saga pattern was introduced by Garcia-Molina and Salem [1] to decompose long-lived transactions (LLTs) into sequences of shorter sub-transactions `T1, T2, ..., Tn`, each with a compensating transaction `Ci` that semantically undoes `Ti`. The DBMS guarantees either full completion or compensated rollback.

**Critical distinction from the original paper:** Garcia-Molina assumed a single DBMS managing saga execution. Modern microservices sagas operate across multiple independent databases with network communication, making compensation itself a distributed operation that can fail. This necessitates additional mechanisms:

- **Idempotency** — not needed in the original single-DBMS model, essential with at-least-once message delivery [10, Ch. 11]
- **Durable execution log** — analogous to a database's WAL, enables crash recovery of the orchestrator itself
- **TCC reservations** — mitigate the loss of isolation that Garcia-Molina acknowledged as a trade-off [1, Section 4]

### 2.3 Try-Confirm-Cancel (TCC)

Helland [2] argued that as systems scale beyond a single database, applications must manage consistency through **tentative operations** that progress through states: tentative → confirmed or tentative → canceled. Helland and Campbell [3] formalized this further: in large-scale systems, all knowledge is approximate and tentative, and correctness emerges through confirmation or compensation.

Our TCC implementation uses the **Amazon two-column reservation model**: each resource maintains `available` and `reserved` counters. The Try phase atomically moves quantity from available to reserved; Confirm finalizes by removing from reserved; Cancel restores from reserved to available. Reservations have TTLs for defense-in-depth against orchestrator failure.

### 2.4 Deterministic Ordering

Thomson et al. [4] showed that pre-ordering transactions in a deterministic log before execution eliminates the need for distributed locking during execution. All replicas process the same transactions in the same order, arriving at identical states.

We apply this principle via **partitioned Redis Streams**: checkout requests are appended to `checkout-log:{partition}` streams, where partition is determined by `hash(item_ids) % N`. Within each partition, a single orchestrator processes sagas in stream order, guaranteeing serializability for conflicting operations. Non-conflicting operations (different partitions) execute in parallel.

This is inspired by the VLDB 2025 HDCC paper [9], which demonstrates that hybrid deterministic/non-deterministic approaches adaptively outperform pure deterministic or pure optimistic systems under varying contention.

### 2.5 Consistency Model Analysis

| Layer | Consistency Level | Mechanism | Formal Basis |
|-------|------------------|-----------|--------------|
| Within each service | **Serializable** | Redis Lua scripts execute atomically (single-threaded event loop) | Redis execution model guarantee |
| Across services (saga) | **Read Committed** | TCC two-column model — reservations isolate in-progress sagas from each other | Daraghmi & Zhang [8] quota-cache concept |
| End-to-end | **Saga consistency** | All-or-nothing at saga level + non-negativity invariants enforced by atomic Lua checks | Garcia-Molina [1] + Bailis [5] |

**Minimum required for "no lost money":** Atomic conditional decrements + saga-level atomicity + idempotent operations. Our design exceeds this minimum by providing read-committed isolation via TCC.

---

## 3. Constraints

| Constraint | Value | Source |
|-----------|-------|--------|
| Language | Python only | Project statement |
| Data store | Redis only | Project statement |
| Framework | Quart (async Flask) | Explicitly allowed: "async Flask with Quart" |
| ASGI server | Hypercorn | Required for Quart async support |
| External API | Locked endpoints and response formats | Project statement |
| Benchmark | Must work with wdm-project-benchmark unchanged | Project statement |
| Max resources | 20 CPUs | Project statement |
| Failure model | One container killed at a time, must recover | Project statement |
| Microservice principles | Decentralized data management | Fowler, referenced in statement |

---

## 4. Architecture Overview

```
                        +-------------------+
        Client -------->|   Nginx Gateway   |
                        |      :8000        |
                        +--------+----------+
                                 |
                +----------------+----------------+
                |                |                |
         +------v------+  +-----v-------+  +-----v-------+
         | Order Svc   |  | Stock Svc   |  | Payment Svc |
         | (Quart/     |  | (Quart/     |  | (Quart/     |
         |  Hypercorn) |  |  Hypercorn) |  |  Hypercorn) |
         | x2 inst     |  | x2 inst     |  | x2 inst     |
         +------+------+  +------+------+  +------+------+
                |                |                |
         Saga Orchestrator       |                |
         (consumer group,  stock-commands    payment-commands
          N partitions)    stock-outbox      payment-outbox
                |                |                |
         +------v------+  +-----v-------+  +-----v-------+
         | Order Redis  |  | Stock Redis |  | Payment     |
         | Sentinel     |  | Sentinel    |  | Redis       |
         | (master +    |  | (master +   |  | Sentinel    |
         |  replica)    |  |  replica)   |  | (master +   |
         +-------------+  +-------------+  |  replica)   |
                                            +-------------+

         + Recovery Worker (XAUTOCLAIM, saga timeout checker)
         + Reconciliation Worker (periodic integrity verification)
```

### Design Principles

1. **Decentralized data management** [Fowler]: each service owns its Redis instance exclusively
2. **Event-driven inter-service communication**: Redis Streams, not synchronous HTTP
3. **Atomic local operations**: Redis Lua scripts provide serializable execution within each service
4. **Durable state**: WAL stream + AOF persistence + Sentinel replication
5. **Defense in depth**: multiple overlapping recovery mechanisms (WAL, TTLs, reconciliation, XAUTOCLAIM)

---

## 5. Data Models

### 5.1 Order Redis

```
# Domain data
order:{order_id}              -> msgpack{user_id, items, total_cost, paid, status}

# Saga infrastructure
saga-wal                      -> Redis Stream
                                 Fields: saga_id, step, status, timestamp, data
checkout-log:{partition}      -> Redis Stream (one per partition, N partitions)
                                 Fields: saga_id, order_id, user_id, items, total_cost
saga:{saga_id}:state          -> Redis Hash
                                 Fields: step, status, items_reserved, payment_reserved

# Result delivery & idempotency
saga-result:{saga_id}         -> msgpack{status, error}       TTL: 60s
idempotency:{key}             -> msgpack{status, result}      TTL: 24h
```

### 5.2 Stock Redis — Two-Column Reservation Model

Inspired by Amazon's inventory reservation pattern and the quota-cache concept from Daraghmi & Zhang [8]:

```
# Domain data (two-column model)
item:{item_id}                    -> Redis Hash
                                     Fields: available_stock, reserved_stock, price
                                     Invariant: available_stock + reserved_stock = total_stock

# TCC reservation tracking
reservation:{saga_id}:{item_id}   -> amount (string)           TTL: 60s
saga:{saga_id}:stock:status       -> "reserved"|"confirmed"|"cancelled"  TTL: 24h

# Event streams
stock-commands                    -> Redis Stream (consumer group: "stock-workers")
                                     Fields: saga_id, action, item_id, amount
stock-outbox                      -> Redis Stream
                                     Fields: saga_id, event, item_id, amount
dead-letter:stock-commands        -> Redis Stream (failed messages after MAX_RETRIES)
```

**API response semantics:** The `/stock/find/{item_id}` endpoint returns `{"stock": available_stock + reserved_stock, "price": price}`. The sum represents total stock (matching the benchmark's consistency checks), while internally the two-column model tracks reservations separately. The benchmark verifies `stock_sold + stock_remaining = initial_stock`, so `stock_remaining` must reflect the total regardless of reservation state.

**batch_init migration:** The benchmark's `batch_init` endpoint populates items via `MSET`. With the new Hash-based model, `batch_init` must use `HSET item:{id} available_stock {stock} reserved_stock 0 price {price}` for each item. A Redis pipeline batches these for performance.

### 5.3 Payment Redis — Two-Column Reservation Model

```
# Domain data (two-column model)
user:{user_id}                       -> Redis Hash
                                        Fields: available_credit, held_credit
                                        Invariant: available_credit + held_credit = total_credit

# TCC reservation tracking
reservation:{saga_id}:{user_id}      -> amount (string)        TTL: 60s
saga:{saga_id}:payment:status        -> "reserved"|"confirmed"|"cancelled"  TTL: 24h

# Event streams
payment-commands                     -> Redis Stream (consumer group: "payment-workers")
                                        Fields: saga_id, action, user_id, amount
payment-outbox                       -> Redis Stream
                                        Fields: saga_id, event, user_id, amount
dead-letter:payment-commands         -> Redis Stream (failed messages after MAX_RETRIES)
```

---

## 6. Saga State Machine

### 6.1 States and Transitions

```
STARTED
  |
  | TCC Try -> Stock (reserve items sequentially; WAL records each item)
  v
STOCK_RESERVING --[partial fail]--> CANCELLING_STOCK ---+
  |                                  (cancel only items  |
  | all items reserved               already reserved)   |
  v                                                      |
STOCK_RESERVED                                           |
  |                                                      |
  | TCC Try -> Payment (reserve credit)                  |
  v                                                      |
PAYMENT_RESERVED --[fail]----> CANCELLING_ALL ------+    |
  |                            (cancel stock +      |    |
  | TCC Confirm -> Stock + Payment (in parallel)    |    |
  v                                                 |    |
CONFIRMING --------[timeout]--> retry confirms      |    |
  |                 [permanent fail]--> CANCELLING_ALL    |
  v                                     |                |
COMPLETED                               v                v
                                     FAILED (after all cancels confirmed)
                                       |
                                       | [cancel fails after MAX_RETRIES]
                                       v
                                     ABANDONED (DLQ — manual intervention)
```

**Multi-item reservation:** Orders may contain N items. Each is reserved via a separate Lua script call. The WAL records which items have been successfully reserved (`saga:{saga_id}:state.items_reserved` list). If item K of N fails, the orchestrator cancels items 1..K-1 individually. On crash recovery, the WAL tells the orchestrator exactly which items need cancellation.

**Compensation failure:** If Cancel commands fail after `MAX_RETRIES` (default: 3), the saga transitions to ABANDONED and is written to the Dead Letter Queue. Reservation TTLs (60s) act as the ultimate safety net — even abandoned sagas will auto-release resources when TTLs expire.

**Confirm failure:** If Confirm fails because the reservation TTL expired (permanent failure, not transient), the saga transitions to CANCELLING_ALL rather than retrying infinitely. The Confirm Lua script returns distinct error codes: `1` = success, `0` = already confirmed (idempotent), `-1` = reservation expired (permanent failure).

### 6.2 WAL (Write-Ahead Log) Protocol

Inspired by classical database WAL and Temporal's durable execution model. Every state transition is logged to the `saga-wal` stream **before** the action is taken (write-ahead property):

| State | WAL Entry | Next Action |
|-------|-----------|-------------|
| STARTED | `{saga_id, step: "start", status: "pending"}` | Send TCC Try to Stock (item 1) |
| STOCK_RESERVING | `{saga_id, step: "stock_reserving", items_reserved: [...]}` | Send TCC Try to Stock (next item) |
| STOCK_RESERVED | `{saga_id, step: "stock_reserved", status: "completed"}` | Send TCC Try to Payment |
| PAYMENT_RESERVED | `{saga_id, step: "payment_reserved", status: "completed"}` | Send TCC Confirm to both |
| CONFIRMING | `{saga_id, step: "confirming", status: "pending"}` | Wait for confirms |
| COMPLETED | `{saga_id, step: "completed", status: "done"}` | Store result, notify HTTP handler |
| CANCELLING_STOCK | `{saga_id, step: "cancelling_stock", items_to_cancel: [...]}` | Send TCC Cancel for reserved items |
| CANCELLING_ALL | `{saga_id, step: "cancelling_all"}` | Send TCC Cancel to Stock + Payment |
| FAILED | `{saga_id, step: "failed", reason: "..."}` | Store result, notify HTTP handler |
| ABANDONED | `{saga_id, step: "abandoned"}` | Write to DLQ, rely on reservation TTLs |
| FAILED | `{saga_id, step: "failed", status: "done", error: "..."}` | Cancel active reservations, notify |

### 6.3 Crash Recovery Protocol

On orchestrator restart:

1. Read `saga-wal` for all sagas in non-terminal state
2. **Sort by checkout-log stream ID** (preserves deterministic ordering across crashes)
3. For each stuck saga, resume based on last completed step:

| Last Completed Step | Recovery Action | Justification |
|--------------------|-----------------|----|
| `STARTED` (pending) | Cancel any partial reservations, fail saga | Safe: TCC Cancel is idempotent |
| `STOCK_RESERVED` | Resume from payment Try | Reservation still active (TTL > recovery time) |
| `PAYMENT_RESERVED` | Resume from Confirm phase | Both reservations active, proceed to finalize |
| `CONFIRMING` (pending) | Retry all confirms | TCC Confirm is idempotent |

4. All TCC operations are idempotent via saga-scoped keys (`saga:{saga_id}:{service}:status`), making re-sends safe.

### 6.4 Timeout Handling

| Timeout | Value | Action |
|---------|-------|--------|
| Per-step response | 10 seconds | Treat as failure, enter compensation path |
| Reservation TTL | 60 seconds | Auto-expire if orchestrator dies (defense in depth) |
| Saga max age | 120 seconds | Background sweeper cancels orphaned sagas |
| Sentinel down-after | 5 seconds | Trigger failover election |

---

## 7. TCC Lua Scripts

All scripts implement the **Transactional Outbox pattern**: state changes and outbox events are written atomically within the same Lua script, solving the dual-write problem.

### 7.1 Stock: TCC Try (Reserve)

```lua
-- KEYS[1] = item:{item_id} (Hash: available_stock, reserved_stock, price)
-- KEYS[2] = reservation:{saga_id}:{item_id}
-- KEYS[3] = saga:{saga_id}:stock:status (idempotency key)
-- KEYS[4] = stock-outbox (Stream)
-- ARGV[1] = amount, ARGV[2] = saga_id, ARGV[3] = item_id, ARGV[4] = reservation_ttl

-- Idempotency check (Stripe pattern: check before execute, cache result)
local status = redis.call('GET', KEYS[3])
if status == 'reserved' then
    -- Already processed — re-emit outbox event for orchestrator (safe: streams are append-only)
    redis.call('XADD', KEYS[4], '*',
        'saga_id', ARGV[2], 'event', 'reserved', 'item_id', ARGV[3])
    return 1
end

-- Validate item exists
local available = tonumber(redis.call('HGET', KEYS[1], 'available_stock'))
if not available then
    redis.call('XADD', KEYS[4], '*',
        'saga_id', ARGV[2], 'event', 'failed', 'reason', 'item_not_found')
    return 0
end

-- Check sufficient available stock (non-negativity invariant [5])
local amount = tonumber(ARGV[1])
if available < amount then
    redis.call('XADD', KEYS[4], '*',
        'saga_id', ARGV[2], 'event', 'failed', 'reason', 'insufficient_stock')
    return 0
end

-- Two-column reservation: move from available to reserved
redis.call('HINCRBY', KEYS[1], 'available_stock', -amount)
redis.call('HINCRBY', KEYS[1], 'reserved_stock', amount)

-- Create reservation record with TTL (defense in depth)
redis.call('SET', KEYS[2], ARGV[1])
redis.call('EXPIRE', KEYS[2], tonumber(ARGV[4]))

-- Idempotency marker
redis.call('SETEX', KEYS[3], 86400, 'reserved')

-- Transactional outbox: event emitted atomically with state change
redis.call('XADD', KEYS[4], '*',
    'saga_id', ARGV[2], 'event', 'reserved',
    'item_id', ARGV[3], 'amount', ARGV[1])
return 1
```

### 7.2 Stock: TCC Confirm

```lua
-- KEYS[1] = item:{item_id}, KEYS[2] = reservation:{saga_id}:{item_id}
-- KEYS[3] = saga:{saga_id}:stock:status, KEYS[4] = stock-outbox
-- ARGV[1] = saga_id, ARGV[2] = item_id

-- Idempotency
local status = redis.call('GET', KEYS[3])
if status == 'confirmed' then return 1 end

-- Check reservation still exists (may have expired via TTL)
local amount = redis.call('GET', KEYS[2])
if not amount then
    redis.call('XADD', KEYS[4], '*',
        'saga_id', ARGV[1], 'event', 'confirm_failed', 'reason', 'reservation_expired')
    return 0
end

-- Finalize: decrement reserved (stock is now permanently sold)
redis.call('HINCRBY', KEYS[1], 'reserved_stock', -tonumber(amount))
redis.call('DEL', KEYS[2])
redis.call('SETEX', KEYS[3], 86400, 'confirmed')

redis.call('XADD', KEYS[4], '*',
    'saga_id', ARGV[1], 'event', 'confirmed', 'item_id', ARGV[2])
return 1
```

### 7.3 Stock: TCC Cancel

```lua
-- KEYS[1] = item:{item_id}, KEYS[2] = reservation:{saga_id}:{item_id}
-- KEYS[3] = saga:{saga_id}:stock:status, KEYS[4] = stock-outbox
-- ARGV[1] = saga_id, ARGV[2] = item_id

-- Idempotency
local status = redis.call('GET', KEYS[3])
if status == 'cancelled' then return 1 end

local amount = redis.call('GET', KEYS[2])
if not amount then
    -- Reservation already expired or never existed — idempotent no-op
    redis.call('SETEX', KEYS[3], 86400, 'cancelled')
    redis.call('XADD', KEYS[4], '*',
        'saga_id', ARGV[1], 'event', 'cancelled', 'item_id', ARGV[2])
    return 1
end

-- Restore: move from reserved back to available
redis.call('HINCRBY', KEYS[1], 'available_stock', tonumber(amount))
redis.call('HINCRBY', KEYS[1], 'reserved_stock', -tonumber(amount))
redis.call('DEL', KEYS[2])
redis.call('SETEX', KEYS[3], 86400, 'cancelled')

redis.call('XADD', KEYS[4], '*',
    'saga_id', ARGV[1], 'event', 'cancelled', 'item_id', ARGV[2])
return 1
```

> **Note:** Payment Lua scripts follow the identical pattern, operating on `available_credit` / `held_credit` instead of `available_stock` / `reserved_stock`.

---

## 8. Inter-Service Communication

Our inter-service communication layer embodies the **Staged Event-Driven Architecture (SEDA)** [7]: each service acts as an independent stage connected by explicit queues (Redis Streams), with per-stage backpressure managed through consumer groups and bounded stream lengths. This decouples request admission from processing, preventing cascade failures under load. The partitioned checkout-log approach also draws from the Calvin line of deterministic execution [4], with recent advances in Aria [6] and HDCC [9] demonstrating that deterministic ordering remains state-of-the-art for partitioned workloads.

### 8.1 Event Flow (Redis Streams)

```
Order Service                Stock Service              Payment Service
     |                            |                           |
     |--XADD stock-commands------>|                           |
     |                            |--Lua: try_reserve         |
     |                            |--XADD stock-outbox (atomic with Lua)
     |<--XREADGROUP stock-outbox--|                           |
     |                            |                           |
     |--XADD payment-commands-----|-------------------------->|
     |                            |                    Lua: try_reserve
     |                            |                    XADD payment-outbox
     |<--XREADGROUP payment-outbox|---------------------------|
```

### 8.2 Consumer Groups and Scaling

| Stream | Consumer Group | Consumers | Scaling |
|--------|---------------|-----------|---------|
| `checkout-log:{N}` | `saga-orchestrators` | 1 per partition | Add partitions for throughput |
| `stock-commands` | `stock-workers` | 2+ (one per Stock instance) | Add Stock instances |
| `payment-commands` | `payment-workers` | 2+ (one per Payment instance) | Add Payment instances |

### 8.3 Consumer Failure Recovery (XAUTOCLAIM)

Redis 6.2+ `XAUTOCLAIM` reclaims messages from dead consumers in a consumer group:

```python
# Recovery worker runs periodically (every 30s)
claimed = await db.xautoclaim(
    'stock-commands', 'stock-workers', 'recovery-worker',
    min_idle_time=30000,  # 30 seconds idle = presumed dead
    start='0-0'
)
# Reclaimed messages are re-processed (idempotent handlers make this safe)
```

### 8.4 Dead Letter Queue

After `MAX_RETRIES` (5) failed delivery attempts:

```python
pending = await db.xpending_range(stream, group, '-', '+', count=100)
for msg in pending:
    if msg['times_delivered'] > MAX_RETRIES:
        full_msg = await db.xrange(stream, msg['message_id'], msg['message_id'])
        await db.xadd(f'dead-letter:{stream}', full_msg[0][1])
        await db.xack(stream, group, msg['message_id'])
        logger.critical(f"Message {msg['message_id']} moved to DLQ")
```

---

## 9. HTTP-to-Saga Bridge

The benchmark expects synchronous HTTP responses. The saga is event-driven internally. Bridge uses **reliable key polling** (not pub/sub, which is fire-and-forget and can lose messages):

```python
@app.post('/checkout/<order_id>')
async def checkout(order_id: str):
    saga_id = str(uuid.uuid4())
    idempotency_key = f"checkout:{order_id}"

    # 1. Idempotency check (Stripe pattern [11])
    cached = await db.get(f"idempotency:{idempotency_key}")
    if cached:
        result = msgpack.decode(cached)
        return respond(result)

    # 2. Claim idempotency key atomically (SET NX)
    acquired = await db.set(
        f"idempotency:{idempotency_key}",
        msgpack.encode({"status": "processing"}),
        nx=True, ex=3600
    )
    if not acquired:
        return await poll_for_result(saga_id, timeout=30)

    # 3. Submit to partitioned checkout-log (Calvin-style ordering [4])
    order = await get_order(order_id)
    partition = hash(primary_item_id(order)) % NUM_PARTITIONS
    await db.xadd(f'checkout-log:{partition}', {
        'saga_id': saga_id, 'order_id': order_id,
        'user_id': order.user_id,
        'items': msgpack.encode(order.items),
        'total_cost': str(order.total_cost)
    })

    # 4. Poll for saga result (reliable — no pub/sub)
    result = await poll_for_result(saga_id, timeout=30)

    # 5. Cache result for future idempotent requests
    await db.set(f"idempotency:{idempotency_key}",
                 msgpack.encode(result), ex=86400)
    return respond(result)

async def poll_for_result(saga_id, timeout=30):
    """Poll Redis key with 10ms interval. Reliable unlike pub/sub."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = await db.get(f"saga-result:{saga_id}")
        if result:
            return msgpack.decode(result)
        await asyncio.sleep(0.01)
    return {"status": "failed", "error": "timeout"}
```

---

## 10. Fault Tolerance

### 10.1 Container Kill Scenarios

| Scenario | Impact | Recovery Mechanism | Recovery Time |
|----------|--------|--------------------|---------------|
| Order service dies mid-saga | WAL has partial state | Second Order instance picks up via consumer group + XAUTOCLAIM | ~30s (XAUTOCLAIM idle time) |
| Stock service dies mid-Try | Command pending in stream | Stock restarts, rejoins consumer group, processes message. Lua idempotency prevents double-processing | Container restart time |
| Payment service dies mid-Try | Same as Stock | Same as Stock | Container restart time |
| Order Redis master dies | Potential loss of un-replicated writes | Sentinel promotes replica. `min-replicas-to-write 1` minimizes data loss window | 6-10s (Sentinel failover) |
| Stock Redis master dies | Reservation data at risk | Sentinel promotes replica. Reservation TTLs auto-cancel if data lost | 6-10s |
| Orchestrator dies after Try, before Confirm | Both reservations active but unconfirmed | New orchestrator reads WAL, resumes saga. Reservation TTLs auto-cancel if no resume within 60s | WAL resume or TTL cancel |

### 10.2 Redis Configuration for Durability

```conf
# Required for all Redis instances
appendonly yes
appendfsync everysec

# Prevent writes when replica is down (reduces data loss on failover)
min-replicas-to-write 1
min-replicas-max-lag 1
```

### 10.3 Redis Sentinel Configuration

```conf
sentinel down-after-milliseconds mymaster 5000
sentinel failover-timeout mymaster 10000
```

All services use Sentinel-aware connections:
```python
from redis.asyncio.sentinel import Sentinel
sentinel = Sentinel([(sentinel_host, 26379)], password='redis')
db = sentinel.master_for('order-master', password='redis')
```

### 10.4 Reconciliation Worker (Integrity Verification)

Runs every 60 seconds as a background task. Inspired by Airbnb's transactional integrity measurement system.

```python
async def reconcile():
    # Verify two-column invariant for all items
    for item_id in await get_all_item_ids():
        available = int(await stock_db.hget(f'item:{item_id}', 'available_stock'))
        reserved = int(await stock_db.hget(f'item:{item_id}', 'reserved_stock'))
        if available < 0 or reserved < 0:
            logger.critical(f"INVARIANT VIOLATION: item:{item_id}")

    # Verify two-column invariant for all users
    for user_id in await get_all_user_ids():
        available = int(await payment_db.hget(f'user:{user_id}', 'available_credit'))
        held = int(await payment_db.hget(f'user:{user_id}', 'held_credit'))
        if available < 0 or held < 0:
            logger.critical(f"INVARIANT VIOLATION: user:{user_id}")

    # Find and cancel orphaned sagas
    for saga in await get_all_active_sagas():
        if saga.age_seconds > MAX_SAGA_AGE:
            await cancel_saga(saga.id)
            logger.warning(f"Cancelled orphaned saga: {saga.id}")
```

---

## 11. Docker Topology & CPU Budget

| Component | CPUs | Instances | Total |
|-----------|------|-----------|-------|
| Nginx Gateway | 0.5 | 1 | 0.5 |
| Order Service (+ orchestrator) | 2.0 | 2 | 4.0 |
| Stock Service | 1.0 | 2 | 2.0 |
| Payment Service | 1.0 | 2 | 2.0 |
| Order Redis master | 0.5 | 1 | 0.5 |
| Order Redis replica | 0.5 | 1 | 0.5 |
| Stock Redis master | 0.5 | 1 | 0.5 |
| Stock Redis replica | 0.5 | 1 | 0.5 |
| Payment Redis master | 0.5 | 1 | 0.5 |
| Payment Redis replica | 0.5 | 1 | 0.5 |
| Sentinel (3 processes) | 0.25 | 3 | 0.75 |
| **Total** | | | **12.25** |
| **Headroom for scaling** | | | **7.75** |

> Redis is single-threaded for command processing; allocating >1 CPU to a Redis instance wastes resources. 0.5 CPU per instance is sufficient since Redis will not saturate even at high throughput for our data model.

---

## 12. Performance Characteristics

### 12.1 Consistency Test (1000 concurrent checkouts, 1 item)

- All 1000 requests route to the same partition (same item_id hash)
- Processed serially within that partition (deterministic ordering)
- Each saga: ~10-15ms (4-6 Redis round-trips at ~0.5ms + Lua execution)
- Total: ~10-15 seconds for all 1000 sagas
- First 100 succeed (stock exhausted), remaining 900 fail fast at stock reservation Lua check
- Benchmark timeout: 300s (aiohttp default) — well within limits
- **Expected result: 0 inconsistencies**

### 12.2 Stress Test (100K items, high throughput)

- Low contention: checkouts distributed across 100K items and 8 partitions
- Each partition handles ~12,500 items independently
- Per-partition throughput: ~75-100 sagas/sec
- **Total throughput: ~600-800 sagas/sec** with 8 partitions
- p50 latency: ~15-20ms, p95: ~30-50ms, p99: ~80-100ms

### 12.3 Optimization Levers (if needed)

- Increase partition count (16, 32) for more parallelism
- Redis pipelining for batch_init operations
- Connection pooling: `redis.asyncio.ConnectionPool(max_connections=50)`
- Hypercorn workers: 2-4 per service (async — each handles thousands of concurrent connections)
- Skip Confirm phase for failed sagas (Cancel only)

---

## 13. Technology Stack

| Component | Technology | Why |
|-----------|-----------|-----|
| Web framework | Quart | Async Flask — same API, drop-in migration from template |
| ASGI server | Hypercorn | Native Quart support, async workers |
| Redis client | redis[hiredis] (async) | hiredis C parser for 10x faster Redis parsing |
| Serialization | msgspec (msgpack) | Already in template, faster than JSON |
| Data store | Redis 7.2 | Required. Streams, Functions, WAITAOF available |
| HA | Redis Sentinel | Automatic failover, built into Redis |
| Gateway | Nginx 1.25 | Template default, proven load balancer |
| Containers | Docker / docker-compose | Local development and testing |
| Orchestration | Kubernetes + Helm | Cluster deployment |

---

## 14. References

### Academic Papers

[1] Garcia-Molina, H. & Salem, K. "Sagas." *Proceedings of the ACM SIGMOD International Conference on Management of Data*, pp. 249-259. ACM, 1987.

[2] Helland, P. "Life beyond Distributed Transactions: an Apostate's Opinion." *Proceedings of the 3rd Biennial Conference on Innovative Data Systems Research (CIDR)*, 2007. Revised in *ACM Queue*, 14(5), 2016.

[3] Helland, P. & Campbell, D. "Building on Quicksand." *Proceedings of the 4th Biennial Conference on Innovative Data Systems Research (CIDR)*, 2009.

[4] Thomson, A., Diamond, T., Weng, S-C., Ren, K., Shao, P. & Abadi, D.J. "Calvin: Fast Distributed Transactions for Partitioned Database Systems." *Proceedings of the ACM SIGMOD International Conference on Management of Data*, pp. 1-12. ACM, 2012.

[5] Bailis, P., Fekete, A., Franklin, M.J., Ghodsi, A., Hellerstein, J.M. & Stoica, I. "Coordination Avoidance in Database Systems." *Proceedings of the VLDB Endowment*, 8(3), pp. 185-196, 2015.

[6] Lu, Y., Yu, X., Cao, L. & Madden, S. "Aria: A Fast and Practical Deterministic OLTP Database." *Proceedings of the VLDB Endowment*, 13(12), pp. 2047-2060, 2020.

[7] Welsh, M., Culler, D. & Brewer, E. "SEDA: An Architecture for Well-Conditioned, Scalable Internet Services." *Proceedings of the 18th ACM Symposium on Operating Systems Principles (SOSP)*, pp. 230-243. ACM, 2001.

[8] Daraghmi, E., Zhang, C-P. & Yuan, S-M. "Enhancing Saga Pattern for Distributed Transactions within a Microservices Architecture." *Applied Sciences (MDPI)*, 12(12), 6242, 2022.

[9] Hong, Y., Zhao, H., Lu, W., Du, X., Chen, Y., Pan, A. & Zheng, L. "A Hybrid Approach to Integrating Deterministic and Non-Deterministic Concurrency Control." *Proceedings of the VLDB Endowment*, 18, pp. 1376+, 2025.

[10] Kleppmann, M. *Designing Data-Intensive Applications*. O'Reilly Media, 2017.

### Production Systems and Industry References

[11] Stripe Engineering. "Designing Robust and Predictable APIs with Idempotency." 2017. — Idempotency key pattern used in our saga step handlers.

[12] Uber Engineering. "Cadence: The Open Source Orchestration Engine." 2020. — Durable execution model that inspired our WAL-based crash recovery.

[13] Airbnb Engineering. "Avoiding Double Payments in a Distributed Payments System" (Orpheus framework). 2019. — DAG-based idempotent payment orchestration.

[14] Airbnb Engineering. "Measuring Transactional Integrity in Airbnb's Distributed Payment Ecosystem." 2020. — Reconciliation worker concept.

[15] Richardson, C. *Microservices Patterns*. Manning, 2018. — Saga orchestration and two-column reservation patterns.

[16] Netflix Technology Blog. "Netflix Conductor: A Microservices Orchestrator." 2016. — Workflow state management patterns.

---

## 15. Implementation Phases

### Phase 1 — System Design Document (due March 3)

- Extract message-flow diagrams from this design into a 2-page PDF
- Focus on: checkout happy path, failure/compensation path, crash recovery flow
- Include consistency model justification citing [1, 5]

### Phase 2 — Core Implementation (due March 21)

| Week | Tasks | Owner |
|------|-------|-------|
| Week 1 (Mar 3-9) | Quart migration for all services + Lua scripts for atomic operations + two-column data model | TBD |
| Week 2 (Mar 10-16) | Redis Streams saga orchestrator + WAL + checkout-log partitioning | TBD |
| Week 3 (Mar 17-21) | TCC full lifecycle + idempotency + HTTP bridge + benchmark testing | TBD |

### Phase 3 — Fault Tolerance (due April 11)

| Week | Tasks | Owner |
|------|-------|-------|
| Week 4 (Mar 24-28) | Redis Sentinel setup + Sentinel-aware connections + AOF config | TBD |
| Week 5 (Mar 31-Apr 4) | XAUTOCLAIM recovery + reconciliation worker + reservation sweeper | TBD |
| Week 6 (Apr 7-11) | Fault injection testing + stress test optimization + final benchmarks | TBD |

---

## Appendix A: Simpler Alternative Approaches

For teams with less time or fewer members, these simplified versions achieve good (but not maximum) scores:

### A.1 Option C: Event-Driven Saga without TCC (estimated 8/10)

Same as the full design but:
- Simple saga compensations instead of TCC reservations (subtract stock, add back on failure)
- No two-column data model (single `stock` field)
- Still uses Redis Streams, WAL, Lua scripts, idempotency
- Loses: read-committed isolation, TTL-based auto-cancel
- Gains: simpler data model, fewer Lua scripts, faster implementation

### A.2 Option A: Synchronous Saga with Lua (estimated 6-7/10)

- Quart with async HTTP calls between services (no Redis Streams)
- Lua scripts for atomic operations (passes consistency test)
- Redis Sentinel for HA
- Idempotency keys
- No WAL, no event-driven communication, no TCC
- Simple to implement but scores poorly on architecture difficulty and event-driven criteria

### A.3 Template Baseline (estimated 2-3/10)

The unmodified template with Flask + synchronous REST + GET/SET Redis operations.
- Fails consistency test under any concurrency (race conditions in every endpoint)
- No fault tolerance
- Synchronous blocking architecture
- Not a viable submission
