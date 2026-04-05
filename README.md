# Distributed Checkout System

A high-performance, fault-tolerant microservices checkout system built for the
TU Delft Distributed Data Systems course (DDS26). Implements hybrid 2PC/Saga
transaction coordination over NATS JetStream with automatic protocol selection,
crash recovery, and horizontal scaling.

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
┌──────────┐      ┌───────────┐       ┌───────────┐
│ Order DB │      │ Stock DB  │       │Payment DB │
│ master   │      │ master    │       │ master    │
│ +2 rep   │      │ +2 rep    │       │ +2 rep    │
└──────────┘      └───────────┘       └───────────┘
     ↑                                      ↑
  Sentinel ×3 ─────────────────────────────┘
  (failover for all 3 clusters)
```

**Default deployment (23 containers):** 2 order, 2 stock, 2 payment, 3 Dragonfly
masters, 6 Dragonfly replicas, 3 Sentinels, 1 NATS, 1 HAProxy, 1 Jaeger, 1
Prometheus, 1 Grafana.

### Key Features

**Transaction Coordination:**
- **Hybrid 2PC/Saga** with adaptive protocol selection (hysteresis on abort rate: 2PC→Saga at 10%, Saga→2PC at 5%)
- **Parallel saga execution** — stock and payment steps run concurrently via `_broadcast()`
- **16 atomic Lua functions** across 3 libraries — 2PC prepare/commit/abort + saga execute/compensate + direct ops, all with idempotency checks and poison pill guards
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
- **Dual-structure WAL** — Redis Stream (audit trail) + SET (active sagas) + HASH (per-saga state) for O(1) recovery instead of O(n) stream scan
- **Recovery state machine** — per-state strategy: PREPARING→abort all, COMMITTING→must commit (irrevocable), EXECUTING→compensate, COMPENSATING→retry
- **Parallel recovery** — recovery worker processes failed sagas concurrently
- **Reconciliation loop** — periodic (60s) orphan saga detection; aborts sagas idle >120s
- **Dead Letter Queue** for permanently failed sagas (audit trail + manual resolution)
- **Active-active leader election** (atomic Lua SET NX + TTL with heartbeat) — only one order instance runs recovery; all instances execute checkouts
- **In-process result delivery** — asyncio.Future on happy path (no stream hop); pub/sub + key polling fallback for cross-instance recovery

**8-Layer Consistency Defense:**

| # | Mechanism | What it prevents |
|---|-----------|-----------------|
| 1 | Force 2PC when circuit breakers open | Irrevocable saga mutations during suspected partitions |
| 2 | Dual Sentinel failover detection (PubSub + 10s reconciler) | Stale connections reaching demoted old master |
| 3 | Poison pill in Lua scripts | Late prepare/execute after abort/compensate decision |
| 4 | Selective NATS retry (1 attempt for prepare/execute) | Double-deduction across Sentinel failover |
| 5 | No redis-py `retry_on_error` | Late Lua execution after orchestrator moves on |
| 6 | 2PC commit re-deduction | Lost prepare data after failover (re-applies deduction) |
| 7 | Saga compensate-on-timeout | Uncompensated mutations from ambiguous timeouts |
| 8 | 24h status hash TTL in Lua | Recovery data survives lock key expiry |

Five critical conservation bugs were found during chaos testing (2PC data loss on failover,
NATS double-deduction, late redis-py retry after abort, timeout without compensation,
abort/prepare race condition) — all fixed in production code with <0.5ms latency overhead.
See [`docs/CONSISTENCY_REPORT.md`](docs/CONSISTENCY_REPORT.md) for the full root cause analysis.

**High Availability & Performance:**
- **Sentinel HA** with automatic failover (~5s detection, ~10s failover) and dual detection (fast PubSub event + slow 10s reconciler)
- **Read replicas** with master-first fallback — GET endpoints use replicas; fall back to master on replica error
- **Circuit breakers** per service (5 failures → open, 30s recovery → half-open probe)
- **Connection prewarm** — pre-creates Redis pool connections at startup (256 order, 128 stock/payment)
- **GC tuning** — gen0 threshold raised 70x (700→50000) to reduce pause frequency for stable throughput
- **Concurrent JetStream publish + inbox wait** — stream ack (~28µs) runs in parallel with reply wait, no sequential penalty

**Chaos Engineering:**
- **Fault injection framework** — inject crashes (`os._exit(1)`), delays, errors at any saga phase via HTTP API
- **Chaos test suite** — network partitions (DB + NATS), cascading multi-service failure, data loss detection, WAL recovery after failover, Sentinel TILT bypass

**Logging:**
- **Structured JSON logging** via structlog with OpenTelemetry trace ID + span ID auto-injection on every log line

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

### Data Management & Consistency Model

**Database-per-service** (Fowler's "Decentralized Data Management"): each service owns an
isolated database instance. Order cannot access stock's data — all cross-service coordination
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

**Replication strategy:**
- **Async master-replica replication** for read scaling and fault tolerance (2 replicas per master)
- GET endpoints read from replicas (round-robin) with master fallback on error
- Write operations always go to master
- Sentinel monitors all 3 clusters and promotes replicas automatically on master failure

**Data partitioning:**
- **Functional partitioning** at service boundary — order, stock, payment each own their keyspace
- **Vertical scaling** on this branch (single master per service); the `finishing-touches` branch
  adds horizontal hash-slot sharding via Valkey Cluster (3 masters per service)

### Stack

| Component | Technology |
|-----------|-----------|
| HTTP framework | Starlette (ASGI) |
| HTTP server | Granian with uvloop |
| Language | Python 3.13 |
| Messaging | NATS JetStream (commands) + Core NATS (replies) |
| Serialization | msgpack |
| Database | Dragonfly (Redis-compatible, multi-threaded) |
| Client library | redis.asyncio with hiredis |
| Failover | Valkey Sentinel ×3 |
| Gateway | HAProxy (leastconn) |
| Tracing | OpenTelemetry → Jaeger |
| Metrics | Prometheus + Grafana |

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
| Database | Redis/Valkey | — | **Dragonfly** (multi-threaded) |
| Lua scripts | Individual `.lua` files | — | **Function libraries** (FUNCTION LOAD) |
| Saga execution | Sequential | — | **Parallel** (`_broadcast()`) |

## Prerequisites

- [Docker](https://docs.docker.com/get-docker/) and Docker Compose v2
- Python 3.11+ (for running tests locally)
- ~4 GB RAM available for Docker

For Kubernetes deployment, see [Kubernetes Deployment (Minikube)](#kubernetes-deployment-minikube) below.

## Quick Start

### 1. Start the system

```bash
docker compose up --build -d
```

> **Low-resource machines (<16 CPUs / Docker Desktop / WSL2):** use the small config:
> ```bash
> docker compose -f docker-compose-small.yml up --build -d
> ```

Wait for all containers to report healthy (~15-20 seconds):

```bash
docker compose ps
```

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

Clone the official benchmark:

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

Four compose files target different hardware profiles:

| Config | File | App Instances | CPU Target | Containers | Use Case |
|--------|------|---------------|------------|------------|----------|
| Small | `docker-compose-small.yml` | 1/1/1 | ~16 CPU | 23 | Docker Desktop / WSL2 |
| Default | `docker-compose.yml` | 2/2/2 | ~30 CPU | 23 | Development / CI |
| Medium | `docker-compose-medium.yml` | 4/4/4 | ~47 CPU | 26 | Stress testing |
| Large | `docker-compose-large.yml` | 9/7/7 | ~90 CPU | 36 | Production benchmark |

"App Instances" = order / stock / payment service replicas. Small and default configs
include the full observability stack (Jaeger, Prometheus, Grafana); medium and large
omit it. Small uses 3 read replicas per DB cluster; all others use 2.

Usage:

```bash
docker compose -f docker-compose-medium.yml up --build -d
```

Each config has a matching HAProxy config (`haproxy-small.cfg`, `haproxy-medium.cfg`,
`haproxy-large.cfg`) with `maxconn` limits tuned for the target concurrency.

## Observability

### Metrics

All services expose Prometheus-format metrics:

```bash
curl http://localhost:8000/orders/metrics
curl http://localhost:8000/stock/metrics
curl http://localhost:8000/payment/metrics
```

### Grafana Dashboard

Open http://localhost:3000 (admin/admin). A pre-provisioned "Checkout System" dashboard
shows real-time saga throughput, latency percentiles, abort rates, and protocol switches.

### Distributed Tracing

Open http://localhost:16686 (Jaeger UI). Traces span across HTTP → orchestrator → NATS →
service handler → Redis, with full W3C context propagation.

### DLQ Status

```bash
curl http://localhost:8000/orders/dlq/status
```

Shows count and recent entries from the Dead Letter Queue.

## Fault Injection (Chaos Engineering)

Stock and payment services expose a fault injection API for testing failure scenarios:

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

## Kubernetes Deployment (Minikube)

> **Note:** The Kubernetes manifests were written for an earlier version of the stack
> (pre-Dragonfly, pre-JetStream, pre-observability) and have **not been updated or tested**
> with the current architecture. They may serve as a starting point but will need
> modifications to match the current docker-compose setup.

A minikube-based deployment mirrors the same architecture but replaces HAProxy with
nginx and uses the [Bitnami Valkey Helm chart](https://github.com/bitnami/charts/tree/main/bitnami/valkey)
for Redis with Sentinel.

### Prerequisites

- [minikube](https://minikube.sigs.k8s.io/docs/start/) v1.32+
- [kubectl](https://kubernetes.io/docs/tasks/tools/)
- [Helm](https://helm.sh/docs/intro/install/) v3

### Deploy

```bash
./minikube-deploy.sh
```

The script starts minikube, builds images inside its Docker daemon, deploys three
Valkey clusters with Sentinel via Helm, and deploys NATS + services + nginx gateway.

```bash
MINIKUBE_IP=$(minikube ip)
curl http://${MINIKUBE_IP}:30080/orders/health
```

### Teardown

```bash
./minikube-teardown.sh
```

### Key differences from docker-compose

| | docker-compose | minikube |
|---|---|---|
| Gateway | HAProxy (leastconn) | nginx (round-robin via kube-proxy) |
| Database | Dragonfly | Bitnami Valkey 9.x Helm chart |
| Sentinel | 3 standalone containers | Sidecar per Valkey pod |
| Shared code | Volume-mounted at runtime | Baked into image at build time |
| Service discovery | Docker DNS | CoreDNS + kube-proxy |
| Entry point | `localhost:8000` | `<minikube-ip>:30080` (NodePort) |

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

Exercises concurrent checkouts and verifies no money or stock is lost.

### Using Locust

```bash
pip install locust
locust -f test/locustfile.py --host=http://localhost:8000 --users 200 --spawn-rate 20
```

Open http://localhost:8089 for the Locust web UI with live throughput/latency charts.

## Testing Fault Tolerance

### Kill a service instance

```bash
docker compose stop order-service-1
# System continues via order-service-2
docker compose start order-service-1
```

### Kill a database master (triggers Sentinel failover)

```bash
docker compose stop stock-db
# Sentinel promotes replica within ~5 seconds
# Services reconnect automatically
docker compose start stock-db
```

### Kill during checkout

```bash
# Terminal 1: start checkout
curl -X POST http://localhost:8000/orders/checkout/{order_id}

# Terminal 2: kill stock mid-transaction
docker compose stop stock-service
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
│   ├── leader.py           # Leader election (SET NX + TTL)
│   ├── wal.py              # Write-ahead log (Redis Streams)
│   ├── transport.py        # Transport protocol abstraction
│   ├── definition.py       # Step + TransactionDefinition
│   ├── metrics.py          # Latency histograms + abort rate
│   └── README.md           # Package documentation
├── common/                 # Shared utilities
│   ├── config.py           # Redis connection factory (Sentinel-aware)
│   ├── db.py               # Database helpers
│   ├── nats_transport.py   # NATS JetStream commands + Core inbox replies
│   ├── result.py           # Pub/sub wait_for_result
│   ├── dlq.py              # Dead Letter Queue (Redis Stream)
│   ├── fault_injection.py  # Chaos engineering fault injector
│   ├── tracing.py          # OpenTelemetry setup + W3C propagation
│   └── logging.py          # structlog setup
├── lua/                    # 16 atomic Lua functions across 3 libraries + individual scripts
│   ├── order_lib.lua       # order_add_item, order_load_and_claim
│   ├── stock_lib.lua       # 2PC prepare/commit/abort + saga execute/compensate + direct add/subtract
│   ├── payment_lib.lua     # 2PC prepare/commit/abort + saga execute/compensate + direct add/subtract
│   └── *.lua               # Individual script files (loaded via EVALSHA)
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
│   ├── test_sentinel_failover.py     # Sentinel failover integration
│   ├── test_stress.py                # Load/stress tests
│   ├── test_wal_metrics.py           # WAL + metrics
│   ├── test_chaos.py                 # Network partition, cascading failure
│   ├── test_chaos_framework.py       # Fault injection framework
│   ├── test_new_unit_tests.py        # Conservation, multi-item, etc.
│   ├── test_new_integration_tests.py # Integration coverage
│   └── locustfile.py                 # Locust load test definition
├── tla/                    # TLA+ formal specification
│   └── ServicesConsistencyPlusCal.tla
├── docs/
│   ├── plans/2026-02-15-system-design.md     # System design document
│   ├── architectural_compliance_report.md    # Assignment compliance analysis
│   ├── CONSISTENCY_REPORT.md                 # Conservation bug audit + fixes
│   ├── tla_consistency_fault_tolerance.md    # TLA+ consistency proofs
│   └── stress_test_results.png               # Benchmark chart
├── docker-compose.yml            # Default 23-container deployment (~30 CPU)
├── docker-compose-small.yml      # Single-instance + observability (~16 CPU)
├── docker-compose-medium.yml     # 4x instances (~47 CPU)
├── docker-compose-large.yml      # 9/7/7 instances (~90 CPU)
├── haproxy*.cfg                  # HAProxy configs per deployment size
├── sentinel.conf                 # Sentinel configuration
├── sentinel-entrypoint.sh        # Sentinel startup script
├── k8s/                          # Kubernetes manifests
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

The `tla/` directory contains a TLA+/PlusCal specification (`ServicesConsistencyPlusCal.tla`)
that formally verifies the consistency and fault tolerance properties of the checkout
protocol. See [`docs/tla_consistency_fault_tolerance.md`](docs/tla_consistency_fault_tolerance.md)
for the analysis.

## Documentation

| Document | Description |
|----------|-------------|
| [`docs/plans/2026-02-15-system-design.md`](docs/plans/2026-02-15-system-design.md) | Full system design, protocol descriptions, failure analysis |
| [`docs/architectural_compliance_report.md`](docs/architectural_compliance_report.md) | How design decisions compare to production systems (Amazon, Uber, Stripe) |
| [`docs/CONSISTENCY_REPORT.md`](docs/CONSISTENCY_REPORT.md) | Conservation bugs found during chaos testing, root causes, and fixes |
| [`docs/tla_consistency_fault_tolerance.md`](docs/tla_consistency_fault_tolerance.md) | TLA+ formal verification of consistency properties |
| [`orchestrator/README.md`](orchestrator/README.md) | Orchestrator package API and design |
| [`KNOWN_ISSUES.md`](KNOWN_ISSUES.md) | Known issues, optimization history, and WSL2 performance notes |

## Logs

```bash
docker compose logs -f order-service-1 order-service-2
docker compose logs -f stock-service stock-service-2
docker compose logs -f payment-service payment-service-2
```
