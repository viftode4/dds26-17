# Distributed Checkout System

A high-performance, fault-tolerant microservices checkout system built for the
TU Delft Distributed Data Systems course (DDS26). Implements hybrid 2PC/Saga
transaction coordination over NATS request-reply with automatic protocol selection,
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
              │   NATS request-reply │
              └──────────┬──────────┘
     ┌───────────────────┼───────────────────┐
     ▼                   ▼                   ▼
┌──────────┐      ┌───────────┐       ┌───────────┐
│ Order DB │      │ Stock ×2  │       │Payment ×2 │
│master    │      │ Lua atoms │       │ Lua atoms │
│ +2 rep   │      └─────┬─────┘       └─────┬─────┘
└──────────┘            ▼                   ▼
     ↑            ┌───────────┐       ┌───────────┐
  Sentinel ×3     │ Stock DB  │       │Payment DB │
  (failover)      │master     │       │master     │
                  │ +2 rep    │       │ +2 rep    │
                  └───────────┘       └───────────┘
```

**20 containers total:** 2 order, 2 stock, 2 payment, 3 Valkey masters,
6 Valkey replicas, 3 Sentinels, 1 NATS, 1 HAProxy gateway.

### Key Features

- **Hybrid 2PC/Saga** with adaptive protocol selection (hysteresis on abort rate)
- **Atomic Lua functions** for stock/payment operations (no race conditions)
- **NATS Core request-reply** for inter-service communication (~0.28ms latency)
- **Write-Ahead Log** for crash recovery (survives any single container kill)
- **Sentinel HA** with automatic failover (~5s recovery)
- **Read replicas** for GET endpoints (reduced master load)
- **Circuit breakers** per service (fail-fast under degradation)
- **Idempotent checkout** (exactly-once via SET NX keys)
- **Forward recovery** (retry confirms with exponential backoff before compensating)
- **Reservation TTL** = 60s (prevents resource leaks)

### Orchestrator Package

The orchestrator (`orchestrator/`) is a **standalone reusable package** with zero
application-specific code. It coordinates distributed transactions for any set of
services — adding a new service requires zero orchestrator changes. See
[`orchestrator/README.md`](orchestrator/README.md) for package documentation.

### Architectural Compliance Report

For a detailed analysis of how this project adheres to the assignment requirements and how
our design decisions compare to real-world production systems (Amazon, Uber, Stripe, etc.),
see [`docs/architectural_compliance_report.md`](docs/architectural_compliance_report.md).

### Stack

Services run **Starlette** (ASGI framework) with **Granian** as the HTTP server on **Python 3.13**.
**NATS** for inter-service messaging. **Valkey 8.1** (Redis-compatible) via `redis.asyncio` with
`hiredis`, `decode_responses=True`, `max_connections=512`.

## Prerequisites

- [Docker](https://docs.docker.com/get-docker/) and Docker Compose v2
- Python 3.11+ (for running tests and benchmarks locally)
- ~4 GB RAM available for Docker

For the Kubernetes deployment, see [Kubernetes Deployment (Minikube)](#kubernetes-deployment-minikube) below.

## Quick Start

### 1. Start the system

```bash
docker compose up --build -d
```

> **Low-resource machines (<16 CPUs / Docker Desktop / WSL2):** use the small config instead:
> `docker compose -f docker-compose-small.yml up --build -d`

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

Expected: **55 passed**.

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

## Deployment Configurations

Five compose files target different hardware profiles:

| Config | File | App Instances | CPU Target | Use Case |
|--------|------|---------------|------------|----------|
| Small | `docker-compose-small.yml` | 1/1/1 | ~6 CPU | Docker Desktop / WSL2 |
| 6 CPU | `docker-compose-6cpu.yml` | 2/2/2 | ~6 CPU | Horizontal scaling validation |
| Default | `docker-compose.yml` | 2/2/2 | ~30 CPU | Development / CI |
| Medium | `docker-compose-medium.yml` | 4/4/4 | ~50 CPU | Stress testing |
| Large | `docker-compose-large.yml` | 9/7/7 | ~90 CPU | Production benchmark |

"App Instances" = order / stock / payment service replicas.

Usage:

```bash
docker compose -f docker-compose-small.yml up --build -d
```

Each config has a matching HAProxy config (`haproxy-small.cfg`, `haproxy-6cpu.cfg`, etc.)
with per-server `maxconn` limits tuned for the target concurrency.

## Kubernetes Deployment (Minikube)

A minikube-based deployment is provided as a scalable alternative to docker-compose.
It mirrors the same architecture but replaces HAProxy with nginx and uses the
[Bitnami Valkey Helm chart](https://github.com/bitnami/charts/tree/main/bitnami/valkey)
for Redis with Sentinel.

### Prerequisites

- [minikube](https://minikube.sigs.k8s.io/docs/start/) v1.32+
- [kubectl](https://kubernetes.io/docs/tasks/tools/)
- [Helm](https://helm.sh/docs/intro/install/) v3

### Deploy

```bash
./minikube-deploy.sh
```

The script:
1. Starts minikube (if not already running) with `--memory=8192 --cpus=8`
2. Builds all Docker images inside minikube's Docker daemon (so no registry is needed)
3. Deploys three Valkey clusters (one per service) with Sentinel via Helm
4. Deploys NATS, the application services, and the nginx gateway into the `distributed-systems` namespace

When complete, the gateway URL is printed:

```
Gateway: http://<minikube-ip>:30080
```

Test with:

```bash
MINIKUBE_IP=$(minikube ip)
curl http://${MINIKUBE_IP}:30080/orders/health
curl http://${MINIKUBE_IP}:30080/stock/health
curl http://${MINIKUBE_IP}:30080/payment/health
```

### Teardown

```bash
./minikube-teardown.sh
```

Removes all application resources, Helm releases, and the `distributed-systems` namespace.

### Monitoring resource usage

Run in a separate terminal while load testing:

```bash
watch -n2 'minikube ssh "docker stats --no-stream \
  --format \"table {{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}\" 2>/dev/null \
  | grep -E \"k8s_(order|stock|payment|nginx|valkey|nats)\" \
  | grep -v POD | grep distributed-systems"'
```

`CPUPerc` is relative to one full CPU — `100%` = 1 CPU, `200%` = 2 CPUs. If a
container is at or near its limit it is being throttled by the CFS scheduler.

### Key differences from docker-compose

| | docker-compose | minikube |
|---|---|---|
| Gateway | HAProxy (leastconn) | nginx (round-robin via kube-proxy) |
| Redis | `valkey/valkey:8.1` direct | Bitnami Valkey 9.x Helm chart |
| Sentinel | 3 standalone containers | Sidecar per Valkey pod (2 replicas = 3 sentinels) |
| Shared code | Volume-mounted at runtime | Baked into image at build time |
| Service discovery | Docker DNS | CoreDNS + kube-proxy |
| Entry point | `localhost:8000` | `<minikube-ip>:30080` (NodePort) |
| Namespace | — | `distributed-systems` |

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

### Kill a Valkey master (triggers Sentinel failover)

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
├── stock/                  # Stock service (NATS subscriber)
│   ├── app.py
│   └── ...
├── payment/                # Payment service (NATS subscriber)
│   ├── app.py
│   └── ...
├── orchestrator/           # Reusable 2PC/Saga orchestrator package
│   ├── README.md           # Package documentation
│   ├── pyproject.toml      # pip install -e orchestrator/
│   ├── core.py             # Main orchestrator class
│   ├── executor.py         # TwoPCExecutor + SagaExecutor
│   ├── recovery.py         # RecoveryWorker (WAL scan, reconciliation)
│   ├── leader.py           # Leader election (SET NX + TTL)
│   ├── wal.py              # Write-ahead log
│   ├── transport.py        # Transport protocol (messaging abstraction)
│   ├── definition.py       # Step + TransactionDefinition
│   └── metrics.py          # Latency histograms + abort rate
├── common/                 # Shared utilities
│   ├── config.py           # Redis connection factory (Sentinel-aware)
│   ├── db.py               # Database helpers
│   ├── nats_transport.py   # NatsTransport + NatsOrchestratorTransport
│   ├── logging.py          # structlog setup
│   └── result.py           # Pub/sub wait_for_result
├── lua/                    # Atomic Lua function libraries
│   ├── order_lib.lua
│   ├── stock_lib.lua
│   └── payment_lib.lua
├── test/                   # 85 unit + 27 integration = 112 tests
│   ├── test_microservices.py
│   ├── test_circuit_breaker.py
│   ├── test_crash_recovery.py
│   ├── test_executor.py
│   ├── test_orchestrator_core.py
│   ├── test_recovery.py
│   ├── test_sentinel_failover.py
│   ├── test_stress.py
│   ├── test_wal_metrics.py
│   ├── test_new_unit_tests.py      # Conservation, payment crash, multi-item, etc.
│   ├── test_new_integration_tests.py  # Integration coverage for new scenarios
│   ├── test_chaos.py               # Network partition, cascading failure tests
│   ├── conftest.py
│   ├── helpers.py              # Shared test helpers
│   ├── topology.py             # Docker topology introspection
│   ├── locustfile.py
│   ├── analyze_stress.py       # Stress test result analyzer
│   ├── parse_results.py        # Benchmark result parser
│   └── utils.py
├── docs/
│   ├── plans/2026-02-15-system-design.md   # Design document
│   ├── architectural_compliance_report.md  # Compliance analysis
│   ├── benchmark_chart.py                  # Chart generator
│   └── stress_test_results.png             # Benchmark results
├── tla/                    # TLA+ formal specification (CheckoutProtocol.tla)
├── env/                    # Redis connection env vars
├── docker-compose.yml            # Default 20-container deployment (~30 CPU)
├── docker-compose-small.yml      # Minimal single-instance (~6 CPU)
├── docker-compose-6cpu.yml       # Dual-instance, constrained (~6 CPU)
├── docker-compose-medium.yml     # 4× instances (~50 CPU)
├── docker-compose-large.yml      # 9/7/7 instances (~90 CPU)
├── haproxy.cfg                   # HAProxy config (default)
├── haproxy-small.cfg             # HAProxy config (small)
├── haproxy-6cpu.cfg              # HAProxy config (6cpu)
├── haproxy-medium.cfg            # HAProxy config (medium)
├── haproxy-large.cfg             # HAProxy config (large)
├── sentinel.conf           # Redis Sentinel configuration
├── sentinel-entrypoint.sh  # Sentinel startup script
├── minikube-deploy.sh      # Minikube full deployment script
├── minikube-teardown.sh    # Minikube teardown script
├── k8s/                    # Kubernetes manifests
│   ├── order-app.yaml
│   ├── stock-app.yaml
│   ├── payment-app.yaml
│   ├── gateway.yaml        # nginx ConfigMap + Service (NodePort 30080) + Deployment
│   └── nats.yaml
├── helm-config/
│   └── redis-helm-values.yaml  # Bitnami Valkey chart values (sentinel, resources)
├── requirements.txt        # Python dependencies (top-level)
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

Benchmarked using the **default** compose config (`docker-compose.yml`, 2/2/2 instances,
~30 CPU) on Docker Desktop with NATS request-reply transport:

**10,000 concurrent users (checkout-only, 120s, 1000/s ramp):**

| Metric | Value |
|--------|-------|
| Total requests | 72,184 |
| Throughput | 585 req/s |
| Failures | 0% |
| Checkout p50 | 2s |
| Checkout p95 | 4.9s |
| Checkout p99 | 23s |
| Consistency | 0 inconsistencies |

**Fault tolerance:** 0% failures during container kills (stock-service, NATS).

> **Note:** Performance on Docker Desktop / WSL2 is lower (~300-800 RPS) due to
> Hyper-V virtualization overhead on Redis AOF writes. Use the `small` or `6cpu`
> configs for these environments. The `medium` and `large` configs are designed
> for dedicated Linux machines.

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
