# Critical Quality Assessment Report
**Project:** Distributed Data Systems
**Date:** 2026-02-25

Below is a comprehensive and highly critical assessment of the system's architecture, reliability, and code quality. The review aligns the written implementation ([`docs/PROJECT_OVERVIEW.md`](file:///home/babu/Workspace/distributed-data-systems/docs/PROJECT_OVERVIEW.md)) with the actual code in the repositories, identifying severe bugs, permanent data loss scenarios, and architectural flaws.

## 1. Critical Architectural & Design Flaws

> [!CAUTION]
> The following flaws lead to **permanent data loss**, leaking stock, and robbing users of credit without an eventual consistency safety net.

### 1.1 2PC Deadlock and TTL Data Loss Leak (Severe)
**Files:** [`order/tpc.py`](file:///home/babu/Workspace/distributed-data-systems/order/tpc.py), [`stock/app.py`](file:///home/babu/Workspace/distributed-data-systems/stock/app.py), [`payment/app.py`](file:///home/babu/Workspace/distributed-data-systems/payment/app.py)

According to `PROJECT_OVERVIEW.md`, 2PC locks use a 30s TTL to prevent deadlocks, meaning if the Order coordinator crashes, the locks will safely expire. However, in `handle_2pc_prepare`, the stock/credit is **already decremented** from availability via Lua scripts *at the time the lock is acquired*.
If the Order service crashes, the lock will auto-expire after 30s. When the Order service revives and attempts to run recovery (`_recover_2pc`), it sends `abort` commands. But the participant's `handle_2pc_abort` has this condition:
```python
if await db.exists(lock_key):
    await db.hincrby(item_id, 'available_stock', quantity)
    await db.delete(lock_key)
```
**Consequence:** Because the lock expired at 30 seconds, `await db.exists(lock_key)` evaluates at false. The balance is **never restored**. Stock and credit are permanently lost into the void.

### 1.2 SAGA Idempotency Key Expiry Causes Rollback Failures
**Files:** [`stock/app.py`](file:///home/babu/Workspace/distributed-data-systems/stock/app.py), [`payment/app.py`](file:///home/babu/Workspace/distributed-data-systems/payment/app.py)

SAGA operations rely strictly on an idempotency system. `op:reserve:...` keys are stored with a **300-second TTL**.
In the implementation of `handle_saga_compensate`, the system decides whether to refund stock based solely on parsing the expired reservation key:
```python
reserve_result = await db.get(reserve_key)
if reserve_result is not None: ... # refund happens here
```
**Consequence:** If the Order service orchestrator or Redis instance experiences downtime exceeding 5 minutes, recovery will kick in and send compensation commands. Because the reserve key expired, `reserve_result` will be `None`, and the rollback will be silently skipped. Stock and user credit are permanently lost.

### 1.3 Incomplete TCC / Missing Reservation Isolation
**Files:** [`stock/app.py`](file:///home/babu/Workspace/distributed-data-systems/stock/app.py), [`payment/app.py`](file:///home/babu/Workspace/distributed-data-systems/payment/app.py), `docs/PROJECT_OVERVIEW.md`

The system design document explicitly advertises a dual-field approach ("Moving units from `available` to `reserved`" / TCC pattern). 
However, checking `SUBTRACT_LUA` in `stock/app.py` and `PAY_LUA` in `payment/app.py` exposes that **this was never fully implemented**:
```lua
redis.call('HINCRBY', KEYS[1], 'available_stock', -amount)
```
The scripts immediately deduct from availability but completely ignore the `reserved_stock`/`held_credit` keys. The architectural claim of separating reserved transactions is a lie; in-flight transaction resources are entirely invisible.

### 1.4 Abandoning Compensation on SAGA Retries
**File:** [`order/saga.py`](file:///home/babu/Workspace/distributed-data-systems/order/saga.py)

If a service is completely down, `_compensate_stock` and `_compensate_payment` attempt to retry sending compensation 3 times (amounting to ~30s delay).
If those retries get exhausted, the orchestrator just logs: `logger.error("...EXHAUSTED retries")` and proceeds. The order is set to `ABORTED` and the WAL is stamped with the `failed` step.
**Consequence:** Because `reconciliation.py` treats `failed` as a `terminal_step`, no system will ever try to perform this compensation again. SAGA cannot guarantee Eventual Consistency under prolonged participant downtime.

---

## 2. Race Conditions & Code Concurrency Issues

> [!WARNING]
> Poor data access patterns leading to unsafe modifications under load.

### 2.1 Order Service `addItem` Data Overwrite Race Condition
**File:** [`order/app.py`](file:///home/babu/Workspace/distributed-data-systems/order/app.py)

The `addItem` route operates as follows:
1. `order_entry = await get_order_from_db(order_id)`
2. `item_reply = await http_client.get(f"{GATEWAY_URL}/stock/find/{item_id}")` (yields the loop)
3. Modifies `order_entry` and calls `await db.set(...)`

**Consequence:** Because of the network yield to the gateway in step 2 without any pessimistic/optimistic locking, two concurrent `addItem` requests on the exact same `order_id` will race. Both will read the same `order_entry` base snapshot, wait for the network, append their respective items, and save it. **One item will get completely overwritten and lost**, violating correctness.

---

## 3. Scalability & Performance Roadblocks

> [!CAUTION]
> The application holds severe O(N) memory anti-patterns that will instantly crack under standard load testing.

### 3.1 Unbounded WAL and Recovery Memory Expansion 
**Files:** [`order/wal.py`](file:///home/babu/Workspace/distributed-data-systems/order/wal.py), [`order/reconciliation.py`](file:///home/babu/Workspace/distributed-data-systems/order/reconciliation.py)

* **O(N) STREAM Loading:** The function `get_incomplete_sagas(db)` blindly initiates an `await db.xrange(WAL_STREAM)` which iterates through up to 50,000 entries and places all dictionaries squarely into application memory. 
* **O(N) SET Loading:** The function `await db.smembers("pending_orders")` loads the entirety of the system's active orders into memory. For thousands of concurrently pending checkouts, it will cause the event-loop to block or trigger Python memory limits.
* **Corrupt WAL Fetch:** The function `get_saga_state(db, saga_id)` executes an `xrevrange(..., count=500)`. Any SAGA whose logs have fallen under 500 interleaved requests won't be recovered properly.

### 3.2 Poor Network Routing & Redundant Hops
**File:** [`order/app.py`](file:///home/babu/Workspace/distributed-data-systems/order/app.py) 

Inside `addItem`, instead of directly utilizing the ready-and-active connection pool it possesses to `stock_db`, the Order service attempts an HTTP client jump to `${GATEWAY_URL}/stock/find/...`. This introduces a pointless DNS-lookup, an artificial internal round-trip HTTP dependency passing through Nginx, and adds significant latency.

---

## 4. Code Quality and Redundancy Observations

> [!NOTE]
> Issues regarding maintainability, readability, and basic anti-patterns.

1. **Massive Boilerplate Duplication**: The `consumer_loop()` logic and identical retry/XAUTOCLAIM setups are literally copy-pasted across `stock/app.py` and `payment/app.py`. A shared library utilizing identical stream processors is missing.
2. **Missing Input Validations**: REST inputs like amounts are merely cast to `int()`. Passing floats passing through HTTP strings will cause a `ValueError` raising a generic unstructured 500 error instead of a graceful 400 Bad Request.
3. **No Dead-Letter Queues (DLQ)**: Failed stream commands are just logged using `app.logger.warning(...)`.
4. **Incorrect implementation of TPC Prepare Timeout Fallback**: If `prepare()` blocks and times out, `payment_prepared` is rendered `False`. If it timed out but the participant *actually received* the request, the Order service will not send it an `abort()`. The service gets completely reliant on the 30s TTL to magically roll it back (which we saw permanently leaks data). 

## Conclusion

While the project displays complex orchestration between Quart, Redis Streams, and Dual-Protocols (a demanding architectural achievement), the concrete implementations contain crippling flaws.
Under a rigorous fault-tolerant benchmark (failing containers rapidly), the 2PC protocol and SAGA idempotency setups guarantee unrecoverable inconsistency and money loss. The architecture requires critical redesign in state rollback tracking and memory iteration limits.
