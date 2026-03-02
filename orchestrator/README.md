# wdm-orchestrator

A hybrid 2PC/Saga transaction orchestrator for Redis Streams-based microservices.
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
    Step("reserve_stock",   "stock",   "try_reserve", "cancel", "confirm", stock_payload,
         direct_executor=stock_direct),    # optional: bypass Redis Streams
    Step("reserve_payment", "payment", "try_reserve", "cancel", "confirm", payment_payload,
         direct_executor=payment_direct),  # optional: bypass Redis Streams
])

# 2. Create orchestrator and start outbox readers
orch = Orchestrator(order_db, {"stock": stock_db, "payment": payment_db}, [checkout])
await orch.start()

# 3. Execute a transaction
result = await orch.execute("checkout", {
    "order_id": "abc", "user_id": "u1", "items": [("item1", 2)], "total_cost": 500
})
# Success: {"status": "success", "saga_id": "...", "protocol": "2pc"}
# Failure: {"status": "failed",  "saga_id": "...", "protocol": "saga", "error": "..."}
```

## direct_executor

`Step` accepts an optional `direct_executor` keyword argument — a callable that
executes the step via FCALL instead of publishing to a Redis Stream:

```python
async def stock_direct_executor(
    saga_id: str, action: str, context: dict, db: aioredis.Redis
) -> dict:
    items = context.get("items", [])
    n = len(items)
    keys = (
        [f"item:{iid}" for iid, _ in items]
        + [f"reservation:{saga_id}:{iid}" for iid, _ in items]
        + [f"reservation_amount:{saga_id}:{iid}" for iid, _ in items]
        + ["stock-outbox", f"saga:{saga_id}:stock:status"]
    )
    if action == "try_reserve":
        ttl = context.get("_reservation_ttl", 60)
        args = [saga_id, str(ttl)] + [v for pair in items for v in (str(pair[0]), str(pair[1]))] + ["1"]
        result = await db.fcall("stock_try_reserve_batch", len(keys), *keys, *args)
        return {"event": "reserved"} if result == 1 else {"event": "failed", "reason": "insufficient_stock"}
    elif action == "confirm":
        result = await db.fcall("stock_confirm_batch", len(keys), *keys, saga_id, str(n), "1")
        return {"event": "confirmed"} if result == 1 else {"event": "confirm_failed", "reason": "reservation_expired"}
    elif action == "cancel":
        await db.fcall("stock_cancel_batch", len(keys), *keys, saga_id, str(n), "1")
        return {"event": "cancelled"}
    return {"event": "failed", "reason": f"unknown_action:{action}"}

Step("reserve_stock", "stock", "try_reserve", "cancel", "confirm",
     stock_payload, direct_executor=stock_direct_executor)
```

When **all** steps in a transaction have a `direct_executor`, the orchestrator
skips outbox consumers entirely — no streams are read, reducing latency to a
single round-trip per step.

## Extensibility

The orchestrator is fully generic. Adding a new service requires only
application code — no changes to the orchestrator package. Define a
`payload_builder` for the new service and add a `Step` to your transaction:

```python
# Example: adding a hypothetical "warehouse" step
def warehouse_payload(saga_id: str, action: str, ctx: dict) -> dict:
    return {"items": json.dumps(ctx["items"]), "warehouse_id": ctx["warehouse_id"]}

extended_checkout = TransactionDefinition(name="extended_checkout", steps=[
    Step("reserve_stock",     "stock",     "try_reserve", "cancel", "confirm", stock_payload),
    Step("reserve_payment",   "payment",   "try_reserve", "cancel", "confirm", payment_payload),
    Step("reserve_warehouse", "warehouse", "try_reserve", "cancel", "confirm", warehouse_payload),
])
```

Each participating service must follow the stream convention:
- Read commands from `{service}-commands` (Redis Stream, consumer group `{service}-workers`)
- Write results to `{service}-outbox` (Redis Stream, fields include `saga_id` and `event`)

Convention-based naming means zero configuration for stream routing.

## Exports

```python
from orchestrator import (
    Orchestrator, TransactionDefinition, Step,
    TwoPCExecutor, SagaExecutor, OutboxReader,
    CircuitBreaker, WALEngine, RecoveryWorker,
    LeaderElection, MetricsCollector, LatencyHistogram,
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
                          │  │   _send_command()    │  │  ← generic, uses payload_builder
                          │  └────────┬────────────┘  │
                          └───────────┼────────────────┘
                                      │
                        ┌─────────────┴─────────────┐
                        ▼                           ▼
               stock-commands              payment-commands
                        │                           │
               ┌────────▼────────┐        ┌────────▼────────┐
               │  Stock Service   │        │ Payment Service  │
               └────────┬────────┘        └────────┬────────┘
                        │                           │
               stock-outbox                payment-outbox
                        │                           │
                        └─────────────┬─────────────┘
                                      ▼
                              OutboxReader (Futures)
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
- **Adaptive reservation TTL**: TTL = f(p99 latency) — automatically adjusts
  to current system load: `max(30, min(120, int(p99_ms / 1000 * 3) + 10))`,
  clamped to 30–120 seconds
- **Exactly-once outbox delivery**: XREADGROUP + XACK consumer groups prevent
  duplicate or lost events
- **AOF durability**: Redis AOF persistence ensures committed data survives restarts

## Recovery

The `RecoveryWorker` handles background maintenance (leader-only):

```python
from orchestrator import RecoveryWorker, LeaderElection

recovery = RecoveryWorker(
    wal=orch.wal,
    service_dbs=service_dbs,
    outbox_reader=orch.outbox_reader,
    definitions=orch.definitions,          # for payload_builder during recovery
    reconcile_fn=my_reconciliation_check,  # optional domain-specific callback
)

leader = LeaderElection(order_db)
if await leader.acquire():
    await recovery.recover_incomplete_sagas()
    await recovery.start_claim_worker()        # XAUTOCLAIM stuck messages
    await recovery.start_reconciliation()      # periodic invariant checks

# Teardown
await recovery.stop()    # cancels background tasks
await leader.release()   # relinquishes leadership
await leader.stop()      # cancels TTL refresh task
```

`LeaderElection` API: `acquire()`, `release()`, `stop()`, `wait_for_leadership()`,
`is_leader` (property).

The `reconcile_fn` callback receives `service_dbs` and runs application-specific
invariant checks (e.g., "no stock value is negative"). The orchestrator schedules
it; the application defines what to check.

WAL terminal states: `COMPLETED`, `FAILED`, `ABANDONED`.

Timing constants:

| Constant | Value | Description |
|----------|-------|-------------|
| `IDLE_THRESHOLD_MS` | `15000` | ms before XAUTOCLAIM reclaims a pending message |
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
| `CONFIRM_MAX_RETRIES` | `3` | Forward recovery attempts |
| `MAX_RETRIES` (recovery) | `5` | Recovery retry attempts for incomplete sagas |
| `WALEngine.MAX_LEN` | `50000` | Max WAL stream entries (trimmed on append) |
| `IDLE_THRESHOLD_MS` | `15000` | ms before XAUTOCLAIM reclaims messages |
| `RECONCILIATION_INTERVAL` | `60` | Seconds between reconciliation runs |
| `ORPHAN_SAGA_TIMEOUT` | `120` | Seconds before orphaned sagas are aborted |

## Dependencies

- `python>=3.11`
- `redis[hiredis]>=5.0` — Redis client with C parser
- `structlog>=23.0` — Structured logging
