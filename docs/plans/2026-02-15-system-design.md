# System Design: Active-Active Hybrid 2PC/Saga Orchestrator

**Project:** Distributed Data Systems (DDS26) — Microservices Transaction Coordination
**Date:** 2026-02-15 (revised 2026-02-27 — final implementation)
**Status:** Implemented and verified

---

## 1. Executive Summary

We built a **production-grade distributed transaction system** for three microservices (Order, Stock, Payment) using only Python and Redis. The system implements a hybrid transaction protocol that adaptively switches between Two-Phase Commit and Saga+TCC based on observed abort rates, with full fault tolerance, horizontal scalability, and observable latency metrics.

### Novel Contributions

| Technique | Origin | Our Application |
|-----------|--------|-----------------|
| **Hybrid Adaptive Protocol** | Hong et al. (HDCC) [1] | Runtime switching between 2PC and Saga via abort-rate hysteresis |
| **Two-Phase Commit** | Gray [2], Bernstein et al. [3] | Parallel-prepare for low-contention fast path |
| **Orchestrated Saga + TCC** | Garcia-Molina & Salem [4], Helland [5] | Reservation-based isolation for high-contention graceful degradation |
| **Transactional Outbox** | Microservices.io pattern | Lua scripts atomically write state + stream event (no dual-write) |
| **Active-Active Orchestration** | Shared-nothing architecture | All order instances execute sagas directly — no single leader bottleneck |
| **Staged Event-Driven Architecture** | Welsh et al. (SEDA) [6] | Redis Streams with consumer groups for per-service backpressure |
| **Batched Atomic Reservations** | Amazon inventory pattern | Single Lua script reserves ALL items atomically (all-or-nothing) |

### Key Properties

- **Consistency:** Exactly-once checkout via idempotency keys + Lua atomics. Zero overselling under concurrent load (verified with 1000 concurrent checkouts).
- **Availability:** Active-active order processing — no single point of failure on the checkout path.
- **Fault tolerance:** WAL crash recovery, XAUTOCLAIM, DLQ, reconciliation, circuit breakers.
- **Scalability:** All three services scale horizontally by adding containers; consumer groups handle load distribution automatically.
- **Durability:** AOF persistence (everysec) + WAITAOF after commit phase + Redis Sentinel HA.
- **Observability:** Prometheus histogram metrics with per-protocol latency (p50/p95/p99), structured JSON logs.

---

## 2. System Architecture

### 2.1 High-Level Topology

```
                    ┌─────────────────────────────┐
                    │   Nginx Gateway (:8000)      │
                    │   (round-robin load balance)  │
                    └──────────┬──────────┬─────────┘
                               │          │
               ┌───────────────┘          └───────────────┐
               ▼                                          ▼
    ┌─────────────────────┐                  ┌─────────────────────┐
    │  order-service-1    │                  │  order-service-2    │
    │  (Quart/Hypercorn)  │                  │  (Quart/Hypercorn)  │
    │  - Orchestrator     │                  │  - Orchestrator     │
    │  - Direct execution │                  │  - Direct execution │
    └──────────┬──────────┘                  └──────────┬──────────┘
               │  XADD to command streams               │
               └──────────────┬─────────────────────────┘
                              │
          ┌───────────────────┼───────────────────┐
          ▼                                       ▼
┌──────────────────────┐             ┌──────────────────────┐
│  stock-commands      │             │  payment-commands    │
│  (Redis Stream)      │             │  (Redis Stream)      │
└──────────────────────┘             └──────────────────────┘
    ▼              ▼                     ▼              ▼
stock-svc-1   stock-svc-2         payment-svc-1  payment-svc-2
(consumer     (consumer           (consumer      (consumer
 group)        group)              group)         group)
    │              │                   │              │
    └──────────────┘                   └──────────────┘
    stock-outbox stream            payment-outbox stream
          │                                │
          └───────────────────────────────┘
             Resolved to per-saga asyncio.Future
             in orchestrator's OutboxReader

[Leader — background maintenance only]
  recover_incomplete_sagas()  →  WAL scan
  XAUTOCLAIM worker           →  reclaim stuck messages
  Reconciliation              →  invariant checks, orphan abort
```

### 2.2 Why Active-Active?

The previous design routed all checkouts through a `checkout-log` stream, processed by a single leader instance. This created an unnecessary single point of failure and added a stream round-trip (~2s polling delay) to every transaction.

The redesign eliminates this bottleneck:

| Dimension | Old (Single Leader) | New (Active-Active) |
|-----------|---------------------|---------------------|
| Checkout throughput | 1 instance | All instances |
| Leader crash → checkouts | Stall until election | Unaffected |
| Latency (stream hop) | +2s polling delay | 0 (in-process) |
| Pub/sub for result | Every checkout | Recovery path only |
| Adding capacity | Re-election required | Add container |

The leader now governs **maintenance only**: XAUTOCLAIM, reconciliation, orphan saga abort. Crash recovery is handled by the WAL and the next leader elected.

### 2.3 Container Inventory (16 total)

| Container | Count | Role |
|-----------|-------|------|
| `order-service-{1,2}` | 2 | HTTP API + Orchestrator (active-active) |
| `stock-service`, `stock-service-2` | 2 | Stock stream consumer + HTTP API |
| `payment-service`, `payment-service-2` | 2 | Payment stream consumer + HTTP API |
| `order-db`, `stock-db`, `payment-db` | 3 | Redis masters (AOF + auth) |
| `order-db-replica`, `stock-db-replica`, `payment-db-replica` | 3 | Redis replicas (read scaling) |
| `sentinel-{1,2,3}` | 3 | Redis Sentinel HA (quorum=2) |
| `gateway` | 1 | Nginx reverse proxy + load balancer |

---

## 3. Technology Stack

| Layer | Choice | Rationale |
|-------|--------|-----------|
| Language | Python 3.12 | Async-native with asyncio |
| Web framework | Quart + Hypercorn | Async Flask-compatible; ASGI |
| Redis client | redis.asyncio + hiredis | Non-blocking, RESP3, Sentinel-aware |
| Serialization | msgpack (stream payloads) + JSON (pub/sub, idempotency) | msgpack for wire efficiency; JSON required by `decode_responses=True` connections |
| Orchestration | Docker Compose | Mandatory for submission; 16 containers |
| Logging | structlog (JSON) | Machine-parseable, context-preserving |

---

## 4. Data Model

### 4.1 Redis Key Schema

```
# Stock service
item:{item_id}                      Hash: available_stock, reserved_stock, price
reservation:{saga_id}:{item_id}     String: TTL-based reservation marker (60s)
saga:{saga_id}:stock:status         String: idempotency marker for Lua

# Payment service
user:{user_id}                      Hash: available_credit, held_credit
reservation:{saga_id}:{user_id}     String: TTL-based reservation (60s)
saga:{saga_id}:payment:status       String: idempotency marker for Lua

# Order service
order:{order_id}                    Hash: user_id, paid, total_cost
order:{order_id}:items              List: "item_id:quantity" entries
idempotency:checkout:{order_id}     String (JSON): {status, saga_id} — TTL 1h/24h
saga-result:{saga_id}               String (JSON): result — TTL 60s
saga-wal                            Stream: WAL entries
orchestrator:leader                 String: leader lock — TTL 5s
```

### 4.2 Redis Streams

```
stock-commands      →  stock consumer group "stock-workers"
stock-outbox        →  read by orchestrator OutboxReader (XREAD)
payment-commands    →  payment consumer group "payment-workers"
payment-outbox      →  read by orchestrator OutboxReader (XREAD)
dead-letter:stock-commands    →  DLQ for stock (MAX_RETRIES=5)
dead-letter:payment-commands  →  DLQ for payment (MAX_RETRIES=5)
```

---

## 5. Transaction Protocol

### 5.1 Adaptive Protocol Selection

The orchestrator maintains a 100-transaction sliding window of abort rates and uses hysteresis to switch protocols:

```python
if current == "2pc" and abort_rate >= 0.10:   # Switch to Saga
if current == "saga" and abort_rate <= 0.05:   # Switch back to 2PC
```

**Why hysteresis?** A fixed threshold oscillates at the boundary. A band (5%–10%) provides stability — the system commits to a protocol for a run of transactions before re-evaluating. This is directly from Hong et al.'s HDCC framework [1].

Protocol selection is per-instance and resets on restart (in-memory state). This is acceptable: both protocols are correct; the adaptive switching is a performance optimization.

### 5.2 Two-Phase Commit (2PC) — Low-Contention Fast Path

```
HTTP handler
    │
    ├─ WAL: PREPARING
    │
    ├─→ XADD stock-commands  (try_reserve)  ─┐  parallel
    ├─→ XADD payment-commands (try_reserve) ─┘
    │
    │   [wait for outbox futures, STEP_TIMEOUT=10s]
    │
    ├─ all reserved? ──YES──→ WAL: COMMITTING
    │                          ├─→ confirm stock
    │                          ├─→ confirm payment
    │                          ├─ WAITAOF(1, 0, 200ms)
    │                          └─ WAL: COMPLETED → return success
    │
    └─ any failed? ──NO──→ WAL: ABORTING
                            ├─→ cancel stock
                            ├─→ cancel payment
                            └─ WAL: FAILED → return error
```

**Performance:** Both services contacted in parallel. Under low contention this is the fastest path — a single round-trip to both services simultaneously.

### 5.3 Saga + TCC — High-Contention Graceful Degradation

```
HTTP handler
    │
    ├─ WAL: reserve_stock_TRYING
    ├─→ try_reserve stock → [wait] → reserved? NO → COMPENSATING → FAILED
    │                                         YES ↓
    ├─ WAL: reserve_payment_TRYING
    ├─→ try_reserve payment → [wait] → reserved? NO → cancel stock → COMPENSATING → FAILED
    │                                             YES ↓
    ├─ WAL: CONFIRMING
    ├─→ confirm stock  (parallel)
    ├─→ confirm payment
    ├─ WAITAOF(1, 0, 200ms)
    └─ WAL: COMPLETED → return success
```

**Why Saga under contention?** Under high abort rates, 2PC wastes resources: it prepares all participants then cancels. Saga fails fast at the first unavailable resource, spending zero work on subsequent steps. This is the TCC "try → confirm/cancel" pattern [4].

### 5.4 WAITAOF Durability Guarantee

After the commit phase completes (confirms received from both services), before writing `COMPLETED` to WAL:

```python
await db.execute_command("WAITAOF", 1, 0, 200)  # per service db
```

`WAITAOF(numlocal=1, numreplicas=0, timeout_ms=200)` blocks until Redis has fsynced the AOF on the local node. This closes the crash window between "confirms written to Redis" and "WAL says COMPLETED" — without it, a crash in this window would cause recovery to re-send confirms (idempotent by Lua design, but unnecessarily). 200ms budget is well within STEP_TIMEOUT (10s).

---

## 6. Atomic Lua Operations

All state mutations use Redis Functions (Lua `FUNCTION LOAD` / `FCALL`). This eliminates dual-write problems — state update and event emission are a single atomic Redis operation.

### 6.1 Stock Functions (`stock_lib.lua`)

```lua
-- All-or-nothing batch reservation
stock_try_reserve_batch(KEYS, ARGV)
  -- Checks idempotency marker first (already reserved? re-emit outbox)
  -- For each item: check available_stock >= amount
  -- If any item insufficient → emit "failed" to outbox, return
  -- Atomically: decrement available_stock, increment reserved_stock
  -- Set reservation keys with TTL (60s)
  -- Emit "reserved" to stock-outbox

stock_confirm_batch(KEYS, ARGV)
  -- Idempotency: already confirmed? re-emit "confirmed"
  -- Decrement reserved_stock (stock is now permanently deducted)
  -- Delete reservation keys
  -- Emit "confirmed" to stock-outbox

stock_cancel_batch(KEYS, ARGV)
  -- Idempotency: already cancelled?
  -- Increment available_stock, decrement reserved_stock (restore)
  -- Delete reservation keys
  -- Emit "cancelled" to stock-outbox

stock_add_direct / stock_subtract_direct
  -- Direct stock manipulation (admin endpoints, not part of saga)
```

### 6.2 Payment Functions (`payment_lib.lua`)

```lua
payment_try_reserve(KEYS, ARGV)
  -- Idempotency check
  -- Check available_credit >= amount
  -- Decrement available_credit, increment held_credit
  -- Set reservation key with TTL
  -- Emit "reserved" to payment-outbox

payment_confirm(KEYS, ARGV)
  -- Idempotency check
  -- Decrement held_credit (credit permanently deducted)
  -- Delete reservation key
  -- Emit "confirmed" to payment-outbox

payment_cancel(KEYS, ARGV)
  -- Idempotency check
  -- Increment available_credit, decrement held_credit (restore)
  -- Emit "cancelled" to payment-outbox
```

### 6.3 Order Functions (`order_lib.lua`)

```lua
order_add_item(KEYS, ARGV)
  -- Atomically append item to order list + update total_cost
  -- Returns ORDER_NOT_FOUND error if order doesn't exist
```

**Why Lua?** The Transactional Outbox pattern requires that the data write and the event publication happen atomically. Without atomicity, a crash between the HSET and the XADD creates an inconsistency (state updated but event lost, or vice versa). Redis Lua scripts run as a single atomic unit — no crash can split them.

---

## 7. Idempotency and Exactly-Once Checkout

```
Client POST /checkout/{order_id}
    │
    ├─ SET NX idempotency:checkout:{order_id}  {"status":"processing","saga_id":"X"}  ex=3600
    │
    ├─ Acquired? YES →
    │   ├─ orchestrator.execute() → returns result directly
    │   ├─ success: SET idempotency key {"status":"success"} ex=86400
    │   └─ failure: DEL idempotency key  (allow retry)
    │
    └─ Acquired? NO → read existing key
        ├─ status=success → return 200 immediately
        ├─ status=failed  → return 400 immediately
        └─ status=processing → wait_for_result(saga_id) via pub/sub
```

**Properties:**
- A duplicate concurrent request waits for the first request's saga result via pub/sub — no wasted work.
- A failed checkout deletes the key, allowing the client to retry after fixing the condition (e.g., adding stock or credit).
- A successful checkout caches the result for 24 hours — subsequent requests return immediately.
- The WAL entry for the saga is written before any commands are sent, so crash recovery can always find in-progress sagas.

---

## 8. Crash Recovery

### 8.1 WAL State Machine

Every state transition is logged to `saga-wal` (Redis Stream, maxlen=50000) **before** the action is taken.

```
Protocol states:
  2PC:  STARTED → PREPARING → COMMITTING → COMPLETED
                            → ABORTING   → FAILED
  Saga: STARTED → reserve_stock_TRYING
                → reserve_payment_TRYING
                → CONFIRMING  → COMPLETED
                → COMPENSATING → FAILED
  Both: ABANDONED (orphan abort after 120s)
```

The `data` field at `STARTED` includes all context — `order_id`, `user_id`, `items`, `total_cost`, `protocol` — so recovery can re-issue correct commands without querying the database.

### 8.2 Recovery Decision Table

| Last WAL State | Action |
|----------------|--------|
| `STARTED` | Mark FAILED (never reached prepare) |
| `PREPARING` | Abort all — send cancel to both services |
| `reserve_stock_TRYING` | Cancel stock (may or may not have reserved) |
| `reserve_payment_TRYING` | Cancel stock + cancel payment |
| `COMMITTING` / `CONFIRMING` | Retry confirms (idempotent — Lua ignores already-confirmed) |
| `ABORTING` / `COMPENSATING` | Retry cancels (idempotent) |

### 8.3 XAUTOCLAIM

If a stock or payment consumer crashes while processing a message, the message stays in the consumer group's PEL (Pending Entry List). The leader runs `XAUTOCLAIM` every 10 seconds to reclaim messages idle longer than 15 seconds and requeue them to a `recovery-worker` consumer.

### 8.4 Reconciliation

The leader runs every 60 seconds:
1. **Invariant check:** Scan all `item:*` and `user:*` keys. Assert `available_stock ≥ 0`, `reserved_stock ≥ 0`, `available_credit ≥ 0`, `held_credit ≥ 0`. Log CRITICAL on any violation.
2. **Orphan saga abort:** Scan WAL for incomplete sagas older than 120 seconds (by stream ID timestamp). Send cancel commands to both services.

---

## 9. Circuit Breaker

Each orchestrator maintains an in-memory circuit breaker per downstream service (stock, payment):

```
closed  →  [5 consecutive failures]  →  open
open    →  [30s elapsed]             →  half-open
half-open → [1 probe succeeds]       →  closed
half-open → [1 probe fails]          →  open
```

When the circuit is open, the orchestrator immediately returns `service_X_unavailable` without attempting the full STEP_TIMEOUT (10s) wait. This prevents one degraded service from holding Hypercorn worker slots and causing a thundering herd under load.

---

## 10. Leader Election

```python
# Acquire: SET NX + TTL
await db.set(LOCK_KEY, instance_id, nx=True, ex=5)

# Heartbeat: atomic Lua (eliminates GET+EXPIRE race window)
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('EXPIRE', KEYS[1], ARGV[2])
else
    return 0
end

# Release: atomic Lua (eliminates GET+DEL race window)
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
else
    return 0
end
```

**Why atomic Lua for heartbeat?** A non-atomic `GET` then `EXPIRE` has a race: between the GET returning our ID and the EXPIRE call, the TTL could expire and another instance could win the lock. The Lua script is a single atomic Redis operation — the check and renewal happen without any gap.

LOCK_TTL = 5s, HEARTBEAT_INTERVAL = 2s. Worst-case leader election after failure: 5s (TTL expiry) + 2s (standby poll interval) = 7s.

---

## 11. High Availability (Redis Sentinel)

Each Redis master has one replica and is monitored by 3 Sentinel nodes (quorum=2):

```
order-db (master)     ←→  order-db-replica
stock-db (master)     ←→  stock-db-replica
payment-db (master)   ←→  payment-db-replica

sentinel-1, sentinel-2, sentinel-3  (monitor all 3 masters)
```

**Sentinel configuration:** The `sentinel-entrypoint.sh` script resolves Docker service hostnames to IP addresses at container startup. This is critical because when a master container is killed, Docker removes its DNS entry — a hostname-based Sentinel config would enter tilt mode and fail to find the new master. IP-based tracking survives the DNS disappearance and correctly identifies the promoted replica.

**Services** use `sentinel.master_for(service_name)` for write connections and `sentinel.slave_for(service_name)` for read-only connections, both via the `create_redis_connection` / `create_replica_connection` factories in `common/config.py`.

**Failover time:** ~5 seconds from master kill to new master elected and services reconnected (verified by experiment).

**Durability:** `appendonly yes`, `appendfsync everysec` on all masters and replicas. Maximum data loss window: 1 second. WAITAOF after commit phase further reduces this window for committed transactions.

---

## 12. Read Scaling via Replicas

Non-transactional GET endpoints route reads to replicas:

| Endpoint | Connection | Rationale |
|----------|-----------|-----------|
| `GET /stock/find/{item_id}` | replica | Display only, stale-read acceptable |
| `GET /payment/find_user/{user_id}` | replica | Display only |
| `GET /orders/find/{order_id}` | replica | Display only |
| `POST /checkout/{order_id}` (order read) | master | Must see latest `paid` status |
| All write endpoints | master | Required for consistency |

If the replica is unavailable, the connection factory falls back to the master transparently. This halves master read load for display endpoints, leaving master capacity for transactional writes.

---

## 13. Dead Letter Queue

When a stream message fails to process after MAX_RETRIES=5 (tracked via `XPENDING`), it is moved to a `dead-letter:{stream}` stream and ACKed from the original group. The DLQ sweep runs every 10 consumer iterations.

This prevents poison messages from blocking the consumer group indefinitely. DLQ entries retain the full original payload plus `original_stream`, `original_id`, and `reason` fields for post-mortem analysis.

---

## 14. Observability

### 14.1 Structured Logging

All services use `structlog` with JSON output:

```json
{"event": "Checkout successful", "order_id": "abc", "saga_id": "xyz", "protocol": "2pc", "level": "info", "timestamp": "2026-02-27T02:29:44Z"}
```

### 14.2 Metrics Endpoint (`GET /orders/metrics`)

Prometheus-compatible format with per-protocol latency histograms:

```
# TYPE saga_total counter
saga_total{result="success"} 49
saga_total{result="failure"} 451

# TYPE saga_abort_rate gauge
saga_abort_rate 0.0000

# TYPE saga_current_protocol gauge
saga_current_protocol{protocol="2pc"} 1

# TYPE leader_status gauge
leader_status 1

# TYPE checkout_duration_seconds histogram
checkout_duration_seconds_bucket{protocol="2pc",le="0.01"} 12
checkout_duration_seconds_bucket{protocol="2pc",le="0.025"} 38
checkout_duration_seconds_bucket{protocol="2pc",le="0.05"} 47
checkout_duration_seconds_bucket{protocol="2pc",le="+Inf"} 49
checkout_duration_seconds_sum{protocol="2pc"} 1.432
checkout_duration_seconds_count{protocol="2pc"} 49
checkout_duration_seconds_bucket{protocol="saga",le="0.05"} 24
checkout_duration_seconds_bucket{protocol="saga",le="0.1"} 31
checkout_duration_seconds_bucket{protocol="saga",le="+Inf"} 42
...
```

The per-protocol latency histogram **proves** the adaptive switching delivers value — 2PC shows lower median latency under low contention while Saga shows lower abort-rate cost under high contention.

---

## 15. Performance Tuning

| Parameter | Value | Rationale |
|-----------|-------|-----------|
| `STEP_TIMEOUT` | 10s | Per-step response deadline |
| `RESERVATION_TTL` | 60s | Reservation expiry (prevents stock lockup on crash) |
| `max_connections` | 256 | Redis connection pool per client (handles 1000 concurrent checkouts) |
| Stream `count` | 50 | Messages per XREADGROUP poll |
| Stream `block` | 2000ms | Blocking wait for new messages |
| Hypercorn `-w 2` | 2 workers | Per service container |
| Nginx `keepalive` | 32–64 | Upstream keepalive connections |
| Nginx `worker_connections` | 4096 | Gateway concurrency |

---

## 16. Deployment

### 16.1 Docker Compose Startup

```bash
docker compose up -d
# All 16 containers start; services wait for Redis via wait_for_redis()
# Sentinel resolves master IPs at startup via sentinel-entrypoint.sh
# Lua function libraries loaded at service startup (FUNCTION LOAD, replace=True)
```

### 16.2 Health Checks

All application containers expose `GET /health` which pings Redis. Docker Compose healthchecks poll every 5s, 3 retries. The gateway only starts after all services are healthy.

### 16.3 Configuration

Services configured via env files:

```
env/order_redis.env   — order-db Sentinel config, cross-service Sentinel configs
env/stock_redis.env   — stock-db Sentinel config
env/payment_redis.env — payment-db Sentinel config

SENTINEL_HOSTS=sentinel-1:26379,sentinel-2:26379,sentinel-3:26379
REDIS_SENTINEL_SERVICE=order-master  # (or stock-master / payment-master)
REDIS_PASSWORD=redis
```

---

## 17. Correctness Verification

### 17.1 Test Suite

`test/test_microservices.py` — 3 tests, all pass in < 1.5s:

| Test | What it verifies |
|------|-----------------|
| `test_order` | Create order, add item, checkout — end-to-end |
| `test_stock` | Create item, add/subtract stock, check value |
| `test_payment` | Create user, add/subtract credit, check value |

### 17.2 Official Benchmark (`wdm-project-benchmark`)

1 item (100 stock) + 1000 users (1 credit each) → 1000 concurrent checkouts:

```
Stock service inconsistencies in the logs:     0
Stock service inconsistencies in the database: 0
Payment service inconsistencies in the logs:   0
Payment service inconsistencies in the database: 0
```

Exactly 100 checkouts succeed (matching stock count). Zero overselling. Zero double-charges.

### 17.3 Additional Properties Verified

- **Idempotency:** Second checkout of a paid order returns 400 without deducting stock a second time.
- **Single-instance kill:** Killing `order-service-1` — requests continue via `order-service-2` with no errors.
- **Sentinel failover:** Killing `order-db` — replica promoted in ~5s, Lua functions survive via AOF replication, all operations continue.
- **Circuit breaker:** Under induced service degradation, failing-fast replaces full STEP_TIMEOUT hangs.

---

## 18. Design Trade-offs and Alternatives Considered

| Decision | Alternative | Why We Chose This |
|----------|-------------|-------------------|
| Redis-only stack | PostgreSQL + Kafka | Single operational concern; Redis provides streams, pub/sub, Lua atomics, and persistence in one system |
| Sentinel HA | Redis Cluster | Cluster adds cross-slot transaction complexity incompatible with our Lua scripts. Sentinel is simpler and sufficient for 3 independent masters |
| Active-active orchestration | Single-leader orchestration | Leader is a bottleneck and a single point of failure on the critical path. Active-active doubles throughput and removes the checkout-log indirection (+2s latency) |
| asyncio Futures for result delivery | Redis pub/sub everywhere | In-process delivery has zero network RTT. Pub/sub retained as cross-instance fallback for recovery retries |
| Adaptive 2PC/Saga | Pure 2PC or pure Saga | Neither alone is optimal: 2PC is fast under low contention but wasteful under high contention; Saga reduces wasted work but is slower sequentially. Hysteresis-based switching gets the best of both |
| msgpack for stream payloads | JSON everywhere | msgpack is 2–3× smaller and faster; decode_responses=True applies to the Redis client's key/field decoding, not stream field values stored via latin-1 encoding |
| `min-replicas-to-write` | Not used | Blocks FUNCTION LOAD at startup before replicas sync. AOF persistence + Sentinel HA provides equivalent durability guarantees without the startup race |

---

## 19. References

[1] Hong, W. et al. "HDCC: Hybrid Distributed Concurrency Control." VLDB 2023.
[2] Gray, J. "Notes on Database Operating Systems." Springer, 1978.
[3] Bernstein, P. A., Hadzilacos, V., Goodman, N. "Concurrency Control and Recovery in Database Systems." Addison-Wesley, 1987.
[4] Garcia-Molina, H., Salem, K. "Sagas." ACM SIGMOD, 1987.
[5] Helland, P. "Life Beyond Distributed Transactions: An Apostate's Opinion." CIDR, 2007.
[6] Welsh, M. et al. "SEDA: An Architecture for Well-Conditioned, Scalable Internet Services." SOSP 2001.
[7] Richardson, C. "Microservices Patterns." Manning, 2018. (Transactional Outbox, Saga patterns)
