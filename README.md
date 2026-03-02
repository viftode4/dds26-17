# Distributed Checkout System

A high-performance, fault-tolerant microservices checkout system built for the
TU Delft Distributed Data Systems course (DDS26). Implements hybrid 2PC/Saga
transaction coordination over Redis Streams with automatic protocol selection,
crash recovery, and horizontal scaling.

## Architecture

```
                     ┌──────────┐
        HTTP :8000 → │ HAProxy  │ (path-prefix routing)
                     └────┬─────┘
              ┌──────────┼──────────┐
              ▼                     ▼
       ┌────────────┐        ┌────────────┐
       │  Order #1  │        │  Order #2  │   ← active-active, 2PC or Saga
       │ Orchestrator│        │ Orchestrator│
       └──────┬─────┘        └──────┬─────┘
              │     Redis Streams    │
     ┌────────┴──────────────────────┴────────┐
     ▼                  ▼                     ▼
┌──────────┐     ┌───────────┐         ┌───────────┐
│ Order DB │     │ Stock ×2  │         │Payment ×2 │
│master+rep│     │ Lua atoms │         │ Lua atoms │
└──────────┘     └─────┬─────┘         └─────┬─────┘
     ↑                 ▼                     ▼
  Sentinel ×3    ┌───────────┐         ┌───────────┐
  (failover)     │ Stock DB  │         │Payment DB │
                 │master+rep │         │master+rep │
                 └───────────┘         └───────────┘
```

**16 containers total:** 2 order, 2 stock, 2 payment, 3 Redis masters,
3 Redis replicas, 3 Sentinels, 1 HAProxy gateway.

### Key Features

- **Hybrid 2PC/Saga** with adaptive protocol selection (hysteresis on abort rate)
- **Direct FCALL bypass** for checkout hot path (skips Redis Streams entirely on fast path)
- **Concurrent consumer dispatch** with asyncio Semaphore (eliminates head-of-line blocking)
- **Atomic Lua functions** for stock/payment operations (no race conditions)
- **Redis Streams** for async inter-service communication (event-driven)
- **Write-Ahead Log** for crash recovery (survives any single container kill)
- **Sentinel HA** with automatic failover (~5s recovery)
- **Read replicas** for GET endpoints (reduced master load)
- **Circuit breakers** per service (fail-fast under degradation)
- **Idempotent checkout** (exactly-once via SET NX keys)
- **Forward recovery** (retry confirms with exponential backoff before compensating)
- **Adaptive reservation TTL** = f(p99 latency)

### Orchestrator Package

The orchestrator (`orchestrator/`) is a **standalone reusable package** with zero
application-specific code. It coordinates distributed transactions for any set of
services — adding a new service requires zero orchestrator changes. See
[`orchestrator/README.md`](orchestrator/README.md) for package documentation.

### Stack

Services run **Starlette** (ASGI framework) with **Granian** as the HTTP server on **Python 3.13**.
Redis client is `redis.asyncio` with `hiredis`, `decode_responses=True`, `max_connections=512`.

## Prerequisites

- [Docker](https://docs.docker.com/get-docker/) and Docker Compose v2
- Python 3.11+ (for running tests and benchmarks locally)
- ~4 GB RAM available for Docker

## Quick Start

### 1. Start the system

```bash
docker compose up --build -d
```

Wait for all containers to report healthy (~15-20 seconds):

```bash
docker compose ps
```

All 6 application services should show `healthy`. The gateway is available at
**http://localhost:8000**.

### 2. Run correctness tests

With the system running:

```bash
pip install requests    # if not already installed
python -m pytest test/test_microservices.py -v
```

Expected output: **3/3 passed**.

Unit tests (no Docker required):

```bash
pip install -r test/requirements-test.txt
python -m pytest test/ -v -m "not integration"
```

Expected: **78/78 passed**.

### 3. Verify consistency

Clone the official benchmark and run it against the system:

```bash
git clone https://github.com/delftdata/wdm-project-benchmark.git
cd wdm-project-benchmark
pip install -r requirements.txt
python run.py
```

Expected: **0 inconsistencies** across 1000 concurrent checkouts.

### 4. Stop the system

```bash
docker compose down
```

To also remove volumes (reset all data):

```bash
docker compose down -v
```

## Stress Testing / Benchmarking

### Using the course benchmark

```bash
cd wdm-project-benchmark
python run.py
```

This exercises concurrent checkouts and verifies no money or stock is lost.

### Using Locust (load testing)

Install Locust:

```bash
pip install locust
```

Use the included locust file or one from `wdm-project-benchmark`:

```bash
locust -f test/locustfile.py --host=http://localhost:8000 --users 200 --spawn-rate 20
```

Open http://localhost:8089 for the Locust web UI with live throughput/latency charts.

### Viewing metrics

The order service exposes Prometheus-compatible metrics:

```bash
curl http://localhost:8000/orders/metrics
```

Includes: saga success/failure counts, abort rate, current protocol (2pc/saga),
leader status, and per-protocol latency histograms (p50/p95/p99).

### Generating benchmark charts

After collecting results, update data in `docs/benchmark_chart.py` and run:

```bash
python docs/benchmark_chart.py
# → saves docs/stress_test_results.png
```

## Testing Fault Tolerance

### Kill a service instance

```bash
docker compose stop order-service-1
# System continues via order-service-2
docker compose start order-service-1
```

### Kill a Redis master (triggers Sentinel failover)

```bash
docker compose stop stock-db
# Sentinel promotes replica within ~5 seconds
# Services reconnect automatically
docker compose start stock-db
```

### Kill during checkout

Run a checkout while simultaneously killing a service:

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
│   ├── Dockerfile
│   └── requirements.txt
├── stock/                  # Stock service (stream consumer)
│   ├── app.py
│   └── ...
├── payment/                # Payment service (stream consumer)
│   ├── app.py
│   └── ...
├── orchestrator/           # Reusable 2PC/Saga orchestrator package
│   ├── README.md           # Package documentation
│   ├── pyproject.toml      # pip install -e orchestrator/
│   ├── core.py             # Main orchestrator class
│   ├── executor.py         # TwoPCExecutor + SagaExecutor
│   ├── recovery.py         # RecoveryWorker (XAUTOCLAIM, reconciliation)
│   ├── leader.py           # Leader election (SET NX + TTL)
│   ├── wal.py              # Write-ahead log
│   ├── definition.py       # Step + TransactionDefinition
│   └── metrics.py          # Latency histograms + abort rate
├── common/                 # Shared utilities
│   ├── config.py           # Redis connection factory (Sentinel-aware)
│   ├── consumer.py         # Stream consumer loop + DLQ sweep
│   ├── logging.py          # structlog setup
│   └── result.py           # Pub/sub wait_for_result
├── lua/                    # Atomic Lua function libraries
│   ├── order_lib.lua
│   ├── stock_lib.lua
│   └── payment_lib.lua
├── test/                   # 78 unit + 11 integration tests
│   ├── test_microservices.py
│   ├── test_circuit_breaker.py
│   ├── test_consumer.py
│   ├── test_crash_recovery.py
│   ├── test_executor.py
│   ├── test_orchestrator_core.py
│   ├── test_outbox_reader.py
│   ├── test_recovery.py
│   ├── test_sentinel_failover.py
│   ├── test_stress.py
│   ├── test_wal_metrics.py
│   ├── conftest.py
│   ├── locustfile.py
│   └── utils.py
├── docs/
│   ├── plans/2026-02-15-system-design.md   # Design document
│   ├── benchmark_chart.py                  # Chart generator
│   └── stress_test_results.png             # Benchmark results
├── env/                    # Redis connection env vars
├── docker-compose.yml      # Full 16-container deployment
├── haproxy.cfg             # HAProxy reverse proxy config
├── sentinel.conf           # Redis Sentinel configuration
├── sentinel-entrypoint.sh  # Sentinel startup script
├── requirements.txt        # Python dependencies (top-level)
├── tla/                    # TLA+ formal specification (CheckoutProtocol.tla)
├── contributions.txt       # Team contributions
└── README.md               # This file
```

## API Reference

All endpoints are available via the gateway at `http://localhost:8000`.

### Order Service (`/orders/`)

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/orders/create/{user_id}` | Create order, returns `{"order_id": "..."}` |
| GET | `/orders/find/{order_id}` | Get order details (id, paid, items, user, cost) |
| POST | `/orders/addItem/{order_id}/{item_id}/{quantity}` | Add item to order |
| POST | `/orders/checkout/{order_id}` | Execute checkout (2PC/Saga) |
| GET | `/orders/metrics` | Prometheus metrics |

### Stock Service (`/stock/`)

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/stock/item/create/{price}` | Create item, returns `{"item_id": "..."}` |
| GET | `/stock/find/{item_id}` | Get item stock and price |
| POST | `/stock/add/{item_id}/{amount}` | Add stock |
| POST | `/stock/subtract/{item_id}/{amount}` | Subtract stock |

### Payment Service (`/payment/`)

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/payment/create_user` | Create user, returns `{"user_id": "..."}` |
| GET | `/payment/find_user/{user_id}` | Get user credit |
| POST | `/payment/add_funds/{user_id}/{amount}` | Add credit |
| POST | `/payment/pay/{user_id}/{amount}` | Direct payment (deduct credit) |

## Performance Results

Benchmarked on Docker Desktop (run r8 — direct FCALL + skip_outbox + merged Lua):

**100 concurrent users:**

| Metric | Value |
|--------|-------|
| Throughput | 573 req/s |
| Checkout p50 | 10 ms |
| Checkout p99 | 91 ms |
| Consistency | 0 inconsistencies |

**200 concurrent users:**

| Metric | Value |
|--------|-------|
| Throughput | 759 req/s |
| Checkout p50 | 67 ms |
| Checkout p99 | 210 ms |
| Consistency | 0 inconsistencies |

## Logs

View structured JSON logs from any service:

```bash
docker compose logs -f order-service-1 order-service-2
docker compose logs -f stock-service stock-service-2
docker compose logs -f payment-service payment-service-2
```

## Design Document

See [`docs/plans/2026-02-15-system-design.md`](docs/plans/2026-02-15-system-design.md)
for the full system design, protocol descriptions, failure analysis, and
architectural decisions.
