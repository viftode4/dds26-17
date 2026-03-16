# wdm-orchestrator

A hybrid 2PC/Saga transaction orchestrator with a pluggable Transport abstraction.
It coordinates distributed transactions across any number of services with
automatic protocol selection, crash recovery, circuit breaking, and adaptive
reservation TTLs — without knowing anything about the services it orchestrates.

## Installation

```bash
pip install -e orchestrator/
```

## Quick Start

```python
from orchestrator import Orchestrator, TransactionDefinition, Step

# 1. Define your transaction with payload builders
def stock_payload(saga_id: str, action: str, ctx: dict) -> dict:
    return {"items": json.dumps(ctx["items"]), "ttl": str(ctx.get("_reservation_ttl", 60))}

def payment_payload(saga_id: str, action: str, ctx: dict) -> dict:
    return {"user_id": ctx["user_id"], "amount": str(ctx["total_cost"]),
            "ttl": str(ctx.get("_reservation_ttl", 60))}

checkout = TransactionDefinition(name="checkout", steps=[
    Step("reserve_stock",   "stock",   stock_payload),
    Step("reserve_payment", "payment", payment_payload),
])

# 2. Create a transport (e.g., NATS)
from common.nats_transport import NatsOrchestratorTransport
transport = NatsOrchestratorTransport(nats_client)

# 3. Create orchestrator and execute
orch = Orchestrator(order_db, transport, [checkout])
await orch.start()

result = await orch.execute("checkout", {
    "order_id": "abc", "user_id": "u1", "items": [("item1", 2)], "total_cost": 500
})
# Success: {"status": "success", "saga_id": "...", "protocol": "2pc"}
# Failure: {"status": "failed",  "saga_id": "...", "protocol": "saga", "error": "..."}
```

## Transport Protocol

The orchestrator communicates with services through a `Transport` abstraction
(`orchestrator/transport.py`):

```python
class Transport(Protocol):
    async def send_and_wait(self, service: str, action: str,
                            payload: dict, timeout: float) -> dict: ...
```

The default implementation uses **NATS Core request-reply**:
- Subject scheme: `svc.{service}.{action}` (e.g., `svc.stock.prepare`)
- Queue groups: `{service}-workers` for load balancing across replicas
- ~0.28ms per-hop latency (vs ~100ms with polling-based approaches)

Any transport that implements `send_and_wait` can be used — the orchestrator is
fully transport-agnostic.

## Extensibility

The orchestrator is fully generic. Adding a new service requires only
application code — no changes to the orchestrator package. Define a
`payload_builder` for the new service and add a `Step` to your transaction:

```python
# Example: adding a hypothetical "warehouse" step
def warehouse_payload(saga_id: str, action: str, ctx: dict) -> dict:
    return {"items": json.dumps(ctx["items"]), "warehouse_id": ctx["warehouse_id"]}

extended_checkout = TransactionDefinition(name="extended_checkout", steps=[
    Step("reserve_stock",     "stock",     stock_payload),
    Step("reserve_payment",   "payment",   payment_payload),
    Step("reserve_warehouse", "warehouse", warehouse_payload),
])
```

Each participating service must subscribe to NATS subjects matching
`svc.{service}.*` and reply with a dict containing an `event` field
(e.g., `{"event": "prepared"}`, `{"event": "committed"}`).

## Exports

```python
from orchestrator import (
    Orchestrator, TransactionDefinition, Step,
    TwoPCExecutor, SagaExecutor,
    CircuitBreaker, WALEngine, RecoveryWorker,
    LeaderElection, MetricsCollector, LatencyHistogram,
    Transport,
)
```

## Architecture

```
                          ┌──────────────────────────┐
                          │      Orchestrator         │
   HTTP request ──────────┤                           │
                          │  ┌─────────┐ ┌─────────┐ │
                          │  │ 2PC Exe │ │Saga Exe │ │  ← adaptive selection
                          │  └────┬────┘ └────┬────┘ │
                          │       │           │       │
                          │  ┌────▼───────────▼────┐  │
                          │  │  transport.send_and  │  │  ← generic, uses payload_builder
                          │  │     _wait()          │  │
                          │  └────────┬────────────┘  │
                          └───────────┼────────────────┘
                                      │
                           NATS request-reply
                        svc.{service}.{action}
                                      │
                        ┌─────────────┴─────────────┐
                        ▼                           ▼
               Stock Service ×2             Payment Service ×2
               (queue group:                (queue group:
                stock-workers)              payment-workers)
```

## Features

- **Adaptive protocol selection**: Automatically switches between 2PC and Saga
  based on sliding-window abort rate (hysteresis at 5%/10% thresholds)
- **Write-Ahead Log (WAL)**: Every state transition is logged before execution,
  enabling full crash recovery
- **Circuit breakers**: Per-service fail-fast with configurable thresholds and
  half-open probing
- **Forward recovery**: Confirm failures are retried with exponential backoff
  before falling back to compensation
- **Reservation TTL**: Configurable TTL for locks/reservations (default 60s in the
  provided payload builders) to prevent resource leaks during long-tail failures
- **Transport-agnostic**: Any messaging system implementing the `Transport`
  protocol can be used (NATS, Redis Streams, gRPC, etc.)
- **AOF durability**: Valkey AOF persistence ensures committed data survives restarts

## Recovery

The `RecoveryWorker` handles background maintenance (leader-only):

```python
from orchestrator import RecoveryWorker, LeaderElection

recovery = RecoveryWorker(
    wal=orch.wal,
    transport=transport,
    definitions=orch.definitions,          # for payload_builder during recovery
    reconcile_fn=my_reconciliation_check,  # optional domain-specific callback
)

leader = LeaderElection(order_db)
if await leader.acquire():
    await recovery.recover_incomplete_sagas()
    await recovery.start_reconciliation()      # periodic invariant checks

# Teardown
await recovery.stop()    # cancels background tasks
await leader.release()   # relinquishes leadership
await leader.stop()      # cancels TTL refresh task
```

`LeaderElection` API: `acquire()`, `release()`, `stop()`, `wait_for_leadership()`,
`is_leader` (property).

The `reconcile_fn` callback runs application-specific invariant checks
(e.g., "no stock value is negative"). The orchestrator schedules it;
the application defines what to check.

WAL terminal states: `COMPLETED`, `FAILED`.

Timing constants:

| Constant | Value | Description |
|----------|-------|-------------|
| `RECONCILIATION_INTERVAL` | `60` s | Seconds between reconciliation runs |
| `ORPHAN_SAGA_TIMEOUT` | `120` s | Seconds before an orphaned saga is aborted |

## Configuration

| Parameter | Default | Description |
|-----------|---------|-------------|
| `protocol` | `"auto"` | `"auto"`, `"2pc"`, or `"saga"` |
| `CircuitBreaker(failure_threshold=)` | `5` | Failures before opening circuit |
| `CircuitBreaker(recovery_timeout=)` | `30.0` | Seconds before half-open probe |
| `MetricsCollector(window_size=)` | `100` | Sliding window for abort rate |
| `STEP_TIMEOUT` | `10.0s` | Per-step response timeout |
| `RECONCILIATION_INTERVAL` | `60` | Seconds between reconciliation runs |
| `ORPHAN_SAGA_TIMEOUT` | `120` | Seconds before orphaned sagas are aborted |

## Dependencies

- `python>=3.11`
- `redis[hiredis]>=5.0` — Redis/Valkey client with C parser
- `structlog>=23.0` — Structured logging
- NATS server (injected via Transport, not a package dependency)
