# Distributed Checkout System

A high-performance, fault-tolerant microservices checkout system built for the
TU Delft Distributed Data Systems course (DDS26). Implements hybrid 2PC/Saga
transaction coordination over NATS JetStream with automatic protocol selection,
crash recovery, and horizontal scaling.

**This branch (`finishing-touches`) uses Valkey Cluster for database sharding and
automatic failover. The `main` branch uses Sentinel + Dragonfly instead.**

## Architecture

```
                     ┌──────────┐
        HTTP :8000 → │ HAProxy  │ (path-prefix routing, leastconn)
                     └────┬─────┘
              ┌──────────┼──────────┐
              ▼          ▼          ▼
       ┌────────────┐ ┌──────┐ ┌──────────┐
       │  Order ×N  │ │Stock │ │Payment×N │
       │Orchestrator│ │  ×N  │ │          │
       └──────┬─────┘ └──┬───┘ └────┬─────┘
              │   NATS JetStream    │
              └──────────┬──────────┘
     ┌───────────────────┼───────────────────┐
     ▼                   ▼                   ▼
┌──────────────┐  ┌──────────────┐  ┌──────────────┐
│ Order Cluster│  │ Stock Cluster│  │Payment Cluster│
│ 3 masters    │  │ 3 masters    │  │ 3 masters    │
│ 3 replicas   │  │ 3 replicas   │  │ 3 replicas   │
└──────────────┘  └──────────────┘  └──────────────┘
       ↑                ↑                 ↑
   Cluster gossip — automatic failover, no Sentinel
```

**Default deployment (30 containers):** 2 order, 2 stock, 2 payment, 18 Valkey
cluster nodes (3 clusters × 3 masters + 3 replicas), 1 cluster-init, 1 NATS,
1 HAProxy, 1 Jaeger, 1 Prometheus, 1 Grafana.

### Key Features

**Transaction Coordination:**
- **Hybrid 2PC/Saga** with adaptive protocol selection (hysteresis on abort rate: 2PC→Saga at 10%, Saga→2PC at 5%)
- **Parallel saga execution** — stock and payment steps run concurrently via `_broadcast()`
- **16 atomic Lua functions** in 3 consolidated libraries — registered via `FUNCTION LOAD` (Redis 7.0+), invoked via `FCALL`; all with idempotency checks and poison pill guards
- **Per-item parallel FCALL** — different items may hash to different shards, so each item is processed in its own FCALL via `asyncio.gather()` with partial failure rollback
- **Hash-tagged keys** for cluster slot co-location — `{item_X}`, `{user_X}`, `{order_X}`, `{order-wal}` ensure multi-key Lua operations stay on one shard
- **Transport-agnostic orchestrator** — pluggable `Transport` protocol; zero application-specific code
- **Idempotent checkout** with TTL differentiation — 60s for failed (allow retry), 86400s for success (prevent re-charge)
- **Backpressure control** — async semaphore caps 500 concurrent in-flight checkouts
- **Forward recovery** (retry confirms with exponential backoff before compensating)
- **Reservation TTL** = 60s (prevents resource leaks)

**Messaging & Serialization:**
- **NATS JetStream** commands (durable, deduplicated, WorkQueue retention, memory storage ~28µs ack) + Core NATS inbox replies (lowest latency)
- **Selective deduplication** — prepare/execute get deterministic `Msg-Id` (prevent double-delivery); commit/abort/compensate intentionally skip it (allow retries, they're idempotent at Lua level)
- **msgpack serialization** for NATS messages (compact binary, faster than JSON)
- **NATS reconnect resilience** — auto re-subscribe consumers and recreate memory-storage streams after disconnect

**Crash Recovery & Consistency:**
- **Dual-structure WAL** — Redis Stream (`{order-wal}:saga-wal`) + SET (`{order-wal}:active_sagas`) + HASH (`{order-wal}:state:{id}`) for O(1) recovery; all hash-tagged to same cluster slot
- **Recovery state machine** — per-state strategy: PREPARING→abort all, COMMITTING→must commit (irrevocable), EXECUTING→compensate, COMPENSATING→retry
- **Parallel recovery** — recovery worker processes failed sagas concurrently
- **Reconciliation loop** — periodic (60s) orphan saga detection; aborts sagas idle >120s
- **Dead Letter Queue** for permanently failed sagas (audit trail + manual resolution)
- **Active-active leader election** (atomic Lua `eval` with SET NX + TTL) — only one order instance runs recovery; all instances execute checkouts
- **In-process result delivery** — asyncio.Future on happy path; separate pub/sub connection as fallback (RedisCluster doesn't expose `pubsub()`, so a standalone connection to one seed node is used; cluster gossips PUBLISH to all nodes)

**Consistency Defense Layers:**

| # | Mechanism | What it prevents |
|---|-----------|-----------------|
| 1 | Force 2PC when circuit breakers open | Irrevocable saga mutations during suspected partitions |
| 2 | Cluster automatic failover via gossip | No external Sentinel dependency; ~5s node-timeout detection |
| 3 | Poison pill in Lua scripts | Late prepare/execute after abort/compensate decision |
| 4 | Selective NATS retry (1 attempt for prepare/execute) | Double-deduction across failover |
| 5 | No redis-py `retry_on_error` | Late Lua execution after orchestrator moves on |
| 6 | 2PC commit re-deduction | Lost prepare data after failover (re-applies deduction) |
| 7 | Saga compensate-on-timeout | Uncompensated mutations from ambiguous timeouts |
| 8 | 24h status hash TTL in Lua | Recovery data survives lock key expiry |

Five critical conservation bugs were found during chaos testing (2PC data loss on failover,
NATS double-deduction, late redis-py retry after abort, timeout without compensation,
abort/prepare race condition) — all fixed in production code with <0.5ms latency overhead.
See [`docs/CONSISTENCY_REPORT.md`](docs/CONSISTENCY_REPORT.md) for the full root cause analysis.

**High Availability & Performance:**
- **Valkey Cluster** with automatic failover via gossip protocol (5s node-timeout, no external Sentinel)
- **Cluster sharding** — hash slots distribute data across 3 masters per cluster; horizontal scaling
- **Circuit breakers** per service (5 failures → open, 30s recovery → half-open probe)
- **Connection prewarm** — pre-creates Redis pool connections at startup
- **GC tuning** — gen0 threshold raised 70x (700→50000) to reduce pause frequency for stable throughput
- **Concurrent JetStream publish + inbox wait** — stream ack (~28µs) runs in parallel with reply wait, no sequential penalty

**Chaos Engineering:**
- **Fault injection framework** — inject crashes (`os._exit(1)`), delays, errors at any saga phase via HTTP API
- **Chaos test suite** — network partitions (DB + NATS), cascading multi-service failure, data loss detection, WAL recovery after failover

**Logging:**
- **Structured JSON logging** via structlog with OpenTelemetry trace ID + span ID auto-injection on every log line

### Data Management & Consistency Model

**Database-per-service** (Fowler's "Decentralized Data Management"): each service owns an
isolated database cluster. Order cannot access stock's data — all cross-service coordination
flows through the orchestrator via NATS messaging. This enforces loose coupling and allows
independent scaling, deployment, and schema evolution per service.

**Consistency guarantees depend on the active protocol:**

| Protocol | Consistency model | When used |
|----------|------------------|-----------|
| **2PC** | Serializability — atomic all-or-nothing commit across services | Default; abort rate < 10% |
| **Saga** | Eventual consistency — execute then compensate on failure | Abort rate >= 10% (high contention) |

The adaptive protocol selector dynamically adjusts the consistency level based on system
health. Under normal operation, 2PC provides the strongest guarantee. When the system is
under stress (high abort rate from contention or timeouts), it switches to Saga to maintain
throughput at the cost of a weaker consistency window during compensation.

**Data partitioning (two levels):**
- **Functional partitioning** at service boundary — order, stock, payment each own their keyspace in independent clusters
- **Hash-slot sharding** within each cluster — 16384 slots distributed across 3 masters; hash tags (`{item_X}`, `{user_X}`) control slot assignment for data locality

**Replication strategy:**
- **Async master-replica replication** within each cluster (1 replica per master, 3 replicas per cluster)
- Cluster gossip detects master failure and auto-promotes replicas (~5s node-timeout)
- Stock/payment clusters optionally enable `min-replicas-to-write 1` for synchronous replication durability

### Valkey Cluster Architecture (what made this hard)

Migrating from Sentinel to Cluster required rethinking every Redis interaction in the system.
This is not a config swap — it's a fundamental architectural change.

**1. Hash-tagged key design (every key in the system redesigned):**

All keys that participate in the same Lua `FCALL` must hash to the same cluster slot.
Redis Cluster uses the `{...}` hash tag to control slot assignment:

| Entity | Hash tag | Example keys |
|--------|----------|-------------|
| Order | `{order_X}` | `{order_0}:data`, `{order_0}:items`, `{order_0}:idempotency:checkout` |
| Stock item | `{item_X}` | `{item_5}:data`, `{item_5}:lock:saga-id`, `{item_5}:2pc-status:saga-id`, `{item_5}:saga-status:saga-id` |
| Payment user | `{user_X}` | `{user_3}:data`, `{user_3}:lock:saga-id`, `{user_3}:amounts:saga-id` |
| WAL/Leader | `{order-wal}` | `{order-wal}:saga-wal`, `{order-wal}:active_sagas`, `{order-wal}:state:saga-id`, `{order-wal}:leader` |

**2. Per-item parallel FCALL (the biggest architectural change):**

On Sentinel, one Lua call atomically handles ALL items in a checkout (e.g., `stock_2pc_prepare`
takes N items, checks all, deducts all in one atomic script). This is impossible on Cluster
because different items hash to different shards (`{item_0}` and `{item_5}` may be on
different masters).

Solution: Lua functions use the `_one` suffix (`stock_2pc_prepare_one`, `stock_saga_execute_one`,
etc.) and operate on a single item. The Python service layer calls them **in parallel** via
`asyncio.gather(*[db.fcall("stock_2pc_prepare_one", ...) for item in items])`. Each FCALL
routes to the correct shard automatically.

This introduces **partial failure** — some items may succeed while others fail. The saga
execute handler detects partial success and auto-compensates the succeeded items before
reporting failure. This complexity doesn't exist on the Sentinel branch.

**3. Lua script consolidation (16 files → 3 function libraries):**

Redis Cluster requires `FUNCTION LOAD` + `FCALL` (not `EVALSHA`). Functions are registered
once and broadcast to all primary nodes:

```python
db.execute_command("FUNCTION", "LOAD", "REPLACE", lua_code,
                   target_nodes=RedisCluster.PRIMARIES)
```

Each library uses `redis.register_function()` (Redis 7.0+) to register all functions at once.
`FCALL` routes to the correct node based on the key's hash slot.

**4. WAL pipeline forced non-transactional:**

The async `RedisCluster` client rejects `pipeline(transaction=True)` with
`RedisClusterException("transaction is deprecated in cluster mode")`. WAL logging
uses `transaction=False` — the recovery worker handles partial writes from non-atomic pipelines.

**5. Pub/sub workaround (cluster doesn't expose `pubsub()`):**

`RedisCluster` has no `pubsub()` method (confirmed limitation in redis-py async).
A separate standalone `aioredis.Redis` connection to one seed node is used for subscriptions.
Redis Cluster gossips `PUBLISH` messages to all nodes via the cluster bus, so subscribing
on any single node receives all publishes cluster-wide.

**6. Leader election uses inline `eval()` (not registered scripts):**

`register_script()` doesn't exist on `RedisCluster`. The renew and release Lua scripts
are passed inline via `db.eval(script, 1, key, ...)` which routes to the correct node
based on the key argument.

**7. Cluster initialization (`cluster-init.sh`):**

An ephemeral container waits for all 18 nodes to accept connections, then forms 3
independent clusters via `redis-cli --cluster create --cluster-replicas 1`. Idempotent:
checks `cluster_state:ok` before attempting creation. Each node resolves its own IP
via `hostname -i` for `cluster-announce-ip` (avoids stale Docker DNS after topology shifts).

**8. Per-node configuration (`cluster-node-entrypoint.sh`):**

Each node generates its config at startup: `cluster-enabled yes`, `appendonly yes`,
`appendfsync everysec`, `cluster-node-timeout 5000`, IP advertisement. Stock/payment
nodes optionally enable `min-replicas-to-write 1` for synchronous replication durability.

### Observability Stack

All three services expose Prometheus-format `/metrics` endpoints. The small and default
compose configs include a full observability stack:

- **Prometheus** — scrapes all service instances every 5s (http://localhost:9090)
- **Grafana** — pre-provisioned dashboard with saga latencies, throughput, abort rates (http://localhost:3000, admin/admin)
- **Jaeger** — distributed traces via OpenTelemetry, spans across NATS + Redis (http://localhost:16686)

Metrics include: saga success/failure counts, abort rate, current protocol (2pc/saga),
leader status, per-protocol latency histograms (p50/p95/p99), and circuit breaker state.

### Orchestrator Package

The orchestrator (`orchestrator/`) is a **standalone reusable package** with zero
application-specific code. It coordinates distributed transactions for any set of
services — adding a new service requires zero orchestrator changes. See
[`orchestrator/README.md`](orchestrator/README.md) for package documentation.

### Stack

| Component | Technology |
|-----------|-----------|
| HTTP framework | Starlette (ASGI) |
| HTTP server | Granian with uvloop |
| Language | Python 3.13 |
| Messaging | NATS JetStream (commands) + Core NATS (replies) |
| Serialization | msgpack |
| Database | Valkey 8.1 Cluster (3 masters + 3 replicas per service) |
| Client library | redis.asyncio RedisCluster with hiredis |
| Failover | Cluster gossip protocol (automatic, no external dependency) |
| Gateway | HAProxy (leastconn) |
| Tracing | OpenTelemetry → Jaeger |
| Metrics | Prometheus + Grafana |

### Valkey Cluster vs Sentinel (branch comparison)

| Aspect | `main` (Sentinel + Dragonfly) | `finishing-touches` (Valkey Cluster) |
|--------|-------------------------------|--------------------------------------|
| Database | Dragonfly (multi-threaded) | Valkey 8.1 (single-threaded per shard) |
| Topology | 1 master + 2 replicas + 3 Sentinels | 3 masters + 3 replicas (sharded) |
| Failover | Sentinel election (~5s detect + ~10s failover) | Cluster gossip (~5s node-timeout) |
| Scaling | Vertical only (bigger master) | Horizontal (add shards) |
| Lua scripts | 16 individual files (EVALSHA) | 3 consolidated libraries (FUNCTION LOAD + FCALL) |
| Key design | Flat namespace | Hash-tagged (`{item_X}`, `{user_X}`, `{order-wal}`) |
| Multi-item ops | Single atomic Lua call for N items | Per-item parallel FCALL with partial failure handling |
| Pub/Sub | Native on master connection | Separate standalone connection (cluster limitation) |
| Init | Sentinel entrypoint script | `cluster-init.sh` (one-shot `redis-cli --cluster create`) |
| Pool size | Per-connection | Per-node (cluster distributes automatically) |
| Consistency | Serializability (2PC) / Eventual (Saga) | Same — protocol-level, not DB-level |

### Technology Evolution

The system went through multiple architectural iterations, each driven by benchmarking:

| Layer | v1 | v2 | v3 (current) |
|-------|----|----|---------------|
| HTTP framework | Flask (sync) | Quart (async) | **Starlette** (ASGI) |
| HTTP server | Gunicorn | Hypercorn | **Granian** + uvloop |
| Python | 3.11 | 3.12 | **3.13** |
| Gateway | Nginx (round-robin) | — | **HAProxy** (leastconn) |
| Messaging | Redis Streams (poll) | NATS Core (request-reply) | **NATS JetStream** (durable) + Core inbox |
| Serialization | JSON | JSON | **msgpack** |
| Database | Redis/Valkey + Sentinel | Dragonfly + Sentinel | **Valkey Cluster** (sharded) |
| Lua scripts | Individual `.lua` files | — | **Function libraries** (FUNCTION LOAD) |
| Saga execution | Sequential | — | **Parallel** (`_broadcast()`) |

## Prerequisites

- [Docker](https://docs.docker.com/get-docker/) and Docker Compose v2
- Python 3.11+ (for running tests locally)
- ~4 GB RAM available for Docker

## Quick Start

### 1. Start the system

```bash
docker compose up --build -d
```

> **Low-resource machines (<16 CPUs / Docker Desktop / WSL2):** use the small config:
> ```bash
> docker compose -f docker-compose-small.yml up --build -d
> ```

Wait for cluster initialization + service health (~30-40 seconds):

```bash
docker compose ps
```

The `cluster-init` container runs once to form the 3 Valkey clusters, then exits.
All application services should show `healthy`. The gateway is at **http://localhost:8000**.

### 2. Seed test data

```bash
curl -X POST http://localhost:8000/stock/batch_init/100/1000/10
curl -X POST http://localhost:8000/payment/batch_init/100/100000
curl -X POST http://localhost:8000/orders/batch_init/100/100/100/10
```

### 3. Smoke test

```bash
curl -X POST http://localhost:8000/orders/checkout/0
# → "Checkout successful"
```

### 4. Run correctness tests

Integration tests (requires running system):

```bash
pip install requests
python -m pytest test/test_microservices.py -v
```

Unit tests (no Docker required):

```bash
pip install -r test/requirements-test.txt
python -m pytest test/ -v -m "not integration"
```

### 5. Verify consistency

```bash
git clone https://github.com/delftdata/wdm-project-benchmark.git
cd wdm-project-benchmark
pip install -r requirements.txt
python run.py
```

Expected: **0 inconsistencies** across 1000 concurrent checkouts.

### 6. Stop the system

```bash
docker compose down
docker compose down -v  # also reset all data
```

## Deployment Configurations

Five compose files target different hardware profiles:

| Config | File | App Instances | CPU Target | Containers | Use Case |
|--------|------|---------------|------------|------------|----------|
| Small | `docker-compose-small.yml` | 1/1/1 | ~16 CPU | 27 | Docker Desktop / WSL2 |
| 6 CPU | `docker-compose-6cpu.yml` | 2/2/2 | ~6 CPU | 30 | Horizontal scaling validation |
| Default | `docker-compose.yml` | 2/2/2 | ~30 CPU | 30 | Development / CI |
| Medium | `docker-compose-medium.yml` | 4/4/4 | ~47 CPU | 36 | Stress testing |
| Large | `docker-compose-large.yml` | 9/7/7 | ~90 CPU | 47 | Production benchmark |

"App Instances" = order / stock / payment service replicas. All configs use 18 Valkey
cluster nodes (3 clusters × 6 nodes). Small and default include the observability stack.

Usage:

```bash
docker compose -f docker-compose-medium.yml up --build -d
```

Each config has a matching HAProxy config with `maxconn` limits tuned for the target concurrency.

## Observability

### Metrics

```bash
curl http://localhost:8000/orders/metrics
curl http://localhost:8000/stock/metrics
curl http://localhost:8000/payment/metrics
```

### Grafana Dashboard

Open http://localhost:3000 (admin/admin). Pre-provisioned "Checkout System" dashboard
with real-time saga throughput, latency percentiles, abort rates, and protocol switches.

### Distributed Tracing

Open http://localhost:16686 (Jaeger UI). Traces span across HTTP → orchestrator → NATS →
service handler → Redis, with full W3C context propagation.

### DLQ Status

```bash
curl http://localhost:8000/orders/dlq/status
```

## Fault Injection (Chaos Engineering)

Stock and payment services expose a fault injection API:

```bash
# Inject a 500ms delay before the prepare phase
curl -X POST http://localhost:8000/stock/fault/set \
  -H "Content-Type: application/json" \
  -d '{"point":"before_prepare","action":"delay","value":500}'

# View active fault rules
curl http://localhost:8000/stock/fault/rules

# Clear all faults
curl -X POST http://localhost:8000/stock/fault/clear
```

Supported injection points: `before_prepare`, `after_prepare`, `before_execute`,
`after_execute`, `before_compensate`, `after_compensate`.

Supported actions: `crash` (hard kill), `delay:<ms>` (simulate slow service),
`error` (raise exception).

## Performance Results

Benchmarked using `docker-compose-small.yml` (1/1/1 instances) on an M4 MacBook Air
(32GB RAM, Docker Desktop):

| Metric | Value |
|--------|-------|
| Throughput | ~3,000 req/s |
| Consistency | 0 inconsistencies |

> **Note:** Docker Desktop on macOS adds significant virtualization overhead. On a native
> Linux machine with 20 CPUs, throughput is expected to be substantially higher. See
> [`KNOWN_ISSUES.md`](KNOWN_ISSUES.md) for WSL2/Docker Desktop performance analysis.

## Stress Testing / Benchmarking

### Using the course benchmark

```bash
cd wdm-project-benchmark
python run.py
```

### Using Locust

```bash
pip install locust
locust -f test/locustfile.py --host=http://localhost:8000 --users 200 --spawn-rate 20
```

Open http://localhost:8089 for the Locust web UI.

## Testing Fault Tolerance

### Kill a service instance

```bash
docker compose stop order-service-1
# System continues via order-service-2
docker compose start order-service-1
```

### Kill a cluster master (triggers automatic failover)

```bash
docker compose stop stock-cluster-1
# Cluster gossip promotes replica within ~5 seconds
# Services reconnect automatically via cluster redirection
docker compose start stock-cluster-1
```

### Kill during checkout

```bash
# Terminal 1: start checkout
curl -X POST http://localhost:8000/orders/checkout/{order_id}

# Terminal 2: kill stock mid-transaction
docker compose stop stock-service-1
```

The WAL ensures the saga is either completed or compensated on recovery.

## Project Structure

```
├── order/                  # Order service (hosts orchestrator)
│   ├── app.py              # HTTP endpoints + checkout_tx definition
│   └── Dockerfile
├── stock/                  # Stock service (NATS subscriber)
│   └── app.py
├── payment/                # Payment service (NATS subscriber)
│   └── app.py
├── orchestrator/           # Reusable 2PC/Saga orchestrator package
│   ├── core.py             # Main orchestrator (adaptive protocol selection)
│   ├── executor.py         # TwoPCExecutor + SagaExecutor (parallel broadcast)
│   ├── recovery.py         # RecoveryWorker (WAL scan, reconciliation)
│   ├── leader.py           # Leader election (eval + SET NX + TTL)
│   ├── wal.py              # Write-ahead log (Redis Streams, hash-tagged)
│   ├── transport.py        # Transport protocol abstraction
│   ├── definition.py       # Step + TransactionDefinition
│   ├── metrics.py          # Latency histograms + abort rate
│   └── README.md           # Package documentation
├── common/                 # Shared utilities
│   ├── config.py           # Redis Cluster connection factory + pub/sub workaround
│   ├── db.py               # Database helpers
│   ├── nats_transport.py   # NATS JetStream commands + Core inbox replies
│   ├── result.py           # Pub/sub wait_for_result (standalone connection fallback)
│   ├── dlq.py              # Dead Letter Queue (Redis Stream)
│   ├── fault_injection.py  # Chaos engineering fault injector
│   ├── tracing.py          # OpenTelemetry setup + W3C propagation
│   └── logging.py          # structlog setup
├── lua/                    # 3 consolidated Lua function libraries (FUNCTION LOAD)
│   ├── order_lib.lua       # order_add_item, order_load_and_claim, order_mark_paid
│   ├── stock_lib.lua       # 2PC prepare/commit/abort + saga execute/compensate + direct ops (_one suffix)
│   └── payment_lib.lua     # 2PC prepare/commit/abort + saga execute/compensate + direct ops
├── observability/
│   ├── prometheus.yml      # Prometheus scrape config
│   └── grafana/            # Grafana provisioning + dashboards
├── test/                   # ~113 tests (unit + integration + chaos)
│   ├── test_microservices.py         # End-to-end API tests
│   ├── test_circuit_breaker.py       # Circuit breaker behavior
│   ├── test_crash_recovery.py        # WAL recovery scenarios
│   ├── test_executor.py              # 2PC + Saga executor logic
│   ├── test_orchestrator_core.py     # Orchestrator + leader election
│   ├── test_recovery.py              # Recovery worker
│   ├── test_stress.py                # Load/stress tests
│   ├── test_wal_metrics.py           # WAL + metrics
│   ├── test_chaos.py                 # Network partition, cascading failure
│   ├── test_chaos_framework.py       # Fault injection framework
│   ├── test_new_unit_tests.py        # Conservation, multi-item, etc.
│   ├── test_new_integration_tests.py # Integration coverage
│   └── locustfile.py                 # Locust load test definition
├── tla/                    # TLA+ formal specification
│   └── CheckoutProtocol.tla
├── docs/
│   ├── plans/2026-02-15-system-design.md     # System design document
│   ├── architectural_compliance_report.md    # Assignment compliance analysis
│   ├── CONSISTENCY_REPORT.md                 # Conservation bug audit + fixes
│   └── stress_test_results.png               # Benchmark chart
├── cluster-init.sh               # One-shot cluster formation (redis-cli --cluster create)
├── cluster-node-entrypoint.sh    # Per-node startup (cluster-enabled, announce-ip)
├── docker-compose.yml            # Default 30-container deployment (~30 CPU)
├── docker-compose-small.yml      # Single-instance + observability (~16 CPU)
├── docker-compose-6cpu.yml       # Dual-instance, constrained (~6 CPU)
├── docker-compose-medium.yml     # 4x instances (~47 CPU)
├── docker-compose-large.yml      # 9/7/7 instances (~90 CPU)
├── haproxy*.cfg                  # HAProxy configs per deployment size
├── env/                          # Cluster node connection env vars
├── k8s/                          # Kubernetes manifests (untested with cluster)
├── helm-config/                  # Bitnami Valkey Helm values
├── minikube-deploy.sh            # Minikube deployment script
├── minikube-teardown.sh          # Minikube teardown script
├── KNOWN_ISSUES.md               # Known issues + optimization history
├── contributions.txt             # Team contributions
└── README.md
```

## API Reference

All endpoints are available via the gateway at `http://localhost:8000`.

### Order Service (`/orders/`)

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/orders/create/{user_id}` | Create order, returns `{"order_id": "..."}` |
| GET | `/orders/find/{order_id}` | Get order details (id, paid, items, user, cost) |
| POST | `/orders/addItem/{order_id}/{item_id}/{quantity}` | Add item to order |
| POST | `/orders/checkout/{order_id}` | Execute checkout (2PC or Saga) |
| POST | `/orders/batch_init/{n}/{n_items}/{n_users}/{item_price}` | Seed test data |
| GET | `/orders/metrics` | Prometheus-format metrics |
| GET | `/orders/dlq/status` | Dead Letter Queue status |
| GET | `/orders/health` | Health check |

### Stock Service (`/stock/`)

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/stock/item/create/{price}` | Create item, returns `{"item_id": "..."}` |
| GET | `/stock/find/{item_id}` | Get item stock and price |
| POST | `/stock/add/{item_id}/{amount}` | Add stock |
| POST | `/stock/subtract/{item_id}/{amount}` | Subtract stock |
| POST | `/stock/batch_init/{n}/{starting_stock}/{item_price}` | Seed test data |
| GET | `/stock/metrics` | Prometheus-format metrics |
| POST | `/stock/fault/set` | Set fault injection rule |
| POST | `/stock/fault/clear` | Clear all fault rules |
| GET | `/stock/fault/rules` | View active fault rules |
| GET | `/stock/health` | Health check |

### Payment Service (`/payment/`)

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/payment/create_user` | Create user, returns `{"user_id": "..."}` |
| GET | `/payment/find_user/{user_id}` | Get user credit |
| POST | `/payment/add_funds/{user_id}/{amount}` | Add credit |
| POST | `/payment/pay/{user_id}/{amount}` | Direct payment (deduct credit) |
| POST | `/payment/batch_init/{n}/{starting_money}` | Seed test data |
| GET | `/payment/metrics` | Prometheus-format metrics |
| POST | `/payment/fault/set` | Set fault injection rule |
| POST | `/payment/fault/clear` | Clear all fault rules |
| GET | `/payment/fault/rules` | View active fault rules |
| GET | `/payment/health` | Health check |

## Formal Verification

The `tla/` directory contains a TLA+ specification (`CheckoutProtocol.tla`)
that formally verifies the consistency and fault tolerance properties of the checkout
protocol — both 2PC and Saga patterns, including crash recovery.

## Documentation

| Document | Description |
|----------|-------------|
| [`docs/plans/2026-02-15-system-design.md`](docs/plans/2026-02-15-system-design.md) | Full system design, protocol descriptions, failure analysis |
| [`docs/architectural_compliance_report.md`](docs/architectural_compliance_report.md) | How design decisions compare to production systems (Amazon, Uber, Stripe) |
| [`docs/CONSISTENCY_REPORT.md`](docs/CONSISTENCY_REPORT.md) | Conservation bugs found during chaos testing, root causes, and fixes |
| [`orchestrator/README.md`](orchestrator/README.md) | Orchestrator package API and design |
| [`KNOWN_ISSUES.md`](KNOWN_ISSUES.md) | Known issues, optimization history, and WSL2 performance notes |

## Logs

```bash
docker compose logs -f order-service-1 order-service-2
docker compose logs -f stock-service-1 stock-service-2
docker compose logs -f payment-service-1 payment-service-2
```
