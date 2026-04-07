# Consistency & Partition Safety Report

## Summary

This report documents the conservation bugs found during chaos testing, their root causes, and the fixes applied. The core invariant under test:

> **total_credit_spent == total_stock_sold x price**

The fixes described below are implemented in production code (not test workarounds), but this report is intentionally limited to mechanisms and test evidence that are still present on the current branch. Claims here are grounded in the current codebase and fresh reruns.

---

## Bugs Found

### Bug 1: 2PC Commit Loses Stock Deduction After Sentinel Failover

**Severity:** Critical — money charged, stock not deducted

**Symptom:** `credit_spent=250 != stock_sold_cost=200` during `test_network_partition_stock_db`

**Root cause:** During a 2PC transaction, the `prepare` phase deducts stock on the Redis master and stores the deduction amount in a status hash. If the master fails before this data replicates to the replica, Sentinel promotes the replica to master. The `commit` phase then runs on the new master, finds no `prepare` data, and simply marks the transaction as "committed" without deducting stock. Meanwhile, payment was committed on a different (healthy) Redis instance.

**The race:**
1. Stock `prepare` deducts stock on old master, sets `status=prepared`
2. Old master dies before async replication completes
3. Sentinel promotes replica (no prepare data)
4. `commit` runs on new master — `status != prepared` → marks `committed` without deducting
5. Payment `commit` succeeds on payment-db → credit permanently deducted
6. Result: credit charged, stock not deducted

**Fix applied:**
- `stock_2pc_commit` and `payment_2pc_commit` Lua scripts now accept item amounts as parameters. If `status != prepared` (prepare data lost), they re-apply the deduction. In 2PC, the commit decision is irrevocable — we must ensure the mutation happens.

**Files:** `lua/stock_lib.lua`, `lua/payment_lib.lua`

---

### Bug 2: NATS Transport Retries Cause Double Deduction During Failover

**Severity:** Critical — stock deducted twice, credit charged once

**Symptom:** `credit_spent=250 != stock_sold_cost=450` (9 stock units deducted but only 5 credits charged)

**Root cause:** `NatsOrchestratorTransport.send_and_wait` retried `prepare` messages up to 6 times on transient errors. During a Sentinel failover, the first attempt executed `prepare` on the old master (stock deducted, status set to `prepared`). The response timed out. The retry sent a new `prepare` to the stock service, which now connected to the new master (Sentinel failover completed). The new master had no status hash (not replicated), so the idempotency check (`if status == prepared then return 1`) failed. Stock was deducted again.

**Fix applied:**
- `prepare` and `execute` actions limited to 1 attempt (no retry) in the NATS transport. These mutations are not safely retryable across Sentinel failovers because the idempotency key lives on the old master.
- `commit`, `abort`, and `compensate` retain full retries — they are idempotent by design (check status before acting, handle missing prepare data).

**File:** `common/nats_transport.py`

---

### Bug 3: Late Redis-py Retry Executes After Orchestrator Aborts

**Severity:** Critical — stock deducted with no matching credit

**Symptom:** `credit_spent=0 != stock_sold_cost=100` (stock deducted, zero credit charged)

**Root cause:** The Redis client (`redis.asyncio`) was configured with `retry_on_error=[ConnectionError, TimeoutError]`, causing it to retry failed Lua calls indefinitely. During a network partition, the stock service's `db.fcall("stock_saga_execute", ...)` hung inside redis-py's retry loop. The NATS request timed out. The orchestrator decided to abort/compensate. Later, when the partition healed, the redis-py retry completed — executing the stock deduction after the orchestrator had already moved on.

**Fix applied:**
- Removed `retry_on_error` from Redis connection config entirely. Lua calls now fail fast on `ConnectionError`/`TimeoutError`. The orchestrator's `_verified_action` handles retries at the correct level for idempotent operations (commit/abort/compensate).

**File:** `common/config.py`

---

### Bug 4: SAGA Executor Doesn't Compensate on Timeout

**Severity:** High — stock deducted via existing TCP connection, never compensated

**Symptom:** Stock deducted during partition, no compensation

**Root cause:** When a SAGA step times out, the executor treated it as "step failed, nothing was done." But during a network partition, existing TCP connections can deliver commands before the socket detects the disconnection. The Lua `execute` script runs and deducts stock, but the response never reaches the orchestrator (NATS timeout). The orchestrator skips compensation because the step is not in `completed_steps`.

**Fix applied:**
- Timeout is now treated as ambiguous: the timed-out step is added to `completed_steps` and compensated. Compensation is idempotent — if the step never actually executed, the compensate Lua script checks the status key and does nothing.

**File:** `orchestrator/executor.py`

---

### Bug 5: Abort/Compensate Races with Late Prepare/Execute (Poison Pill)

**Severity:** High — late mutation after orchestrator decided to abort

**Symptom:** Stock deducted after abort decision

**Root cause:** After the orchestrator decides to abort, it sends abort messages to all services. But the original prepare/execute may still be in-flight (redis-py retry, or TCP buffer). The abort sets `status=aborted`, but if the prepare arrives first, it deducts stock. If the abort arrives first, the late prepare should be blocked — but the original Lua scripts only checked for `prepared` (idempotency), not for `aborted` (conflict).

**Fix applied:**
- All prepare/execute Lua scripts now check for conflicting terminal states:
  - `stock_2pc_prepare`: refuses if `status == 'aborted'` or `'committed'`
  - `stock_saga_execute`: refuses if `status == 'compensated'`
  - Same for payment equivalents
- This "poison pill" pattern ensures that once an abort/compensate decision is recorded in Redis, no late mutation can proceed.

**Files:** `lua/stock_lib.lua`, `lua/payment_lib.lua`

---

## Architectural Defenses

### Layered Partition Defense

| Layer | Mechanism | What It Prevents |
|-------|-----------|-----------------|
| 1 | **Force 2PC when circuit breakers open** | Irrevocable SAGA mutations during suspected partitions |
| 2 | **Pool invalidation on Sentinel failover** | Stale connections reaching demoted old master |
| 3 | **Poison pill in Lua** | Late prepare/execute after abort/compensate decision |
| 4 | **No NATS retry for prepare/execute** | Double-deduction across Sentinel failover |
| 5 | **No redis-py retry_on_error** | Late Lua execution after orchestrator moves on |
| 6 | **SAGA compensate-on-timeout** | Uncompensated mutations from ambiguous timeouts |
| 7 | **2PC commit re-deduction** | Lost prepare data after failover |

### Normal Operation Impact

| Defense | Latency overhead |
|---------|-----------------|
| Force 2PC | 0ms (2PC is default) |
| Pool invalidation | 0ms (background listener) |
| Poison pill | 0.01ms (one extra HGET in Lua) |
| No NATS retry | 0ms (reduces wasted retries) |
| No redis-py retry | 0ms (faster failure detection) |

---

## Test Results

| Verification rerun (2026-04-07) | Result |
|-------|--------|
| Core unit suite (`test_executor`, `test_recovery`, `test_circuit_breaker`, `test_orchestrator_core`, `test_wal_metrics`, `test_new_unit_tests`) | **85 passed** |
| Integration rerun (`test_microservices`, `test_stress`, `test_crash_recovery`) | **9 passed** |
| Sentinel failover rerun (`test_sentinel_failover.py`) | **1 passed, 1 skipped** |
| Chaos rerun (`test_network_partition_stock_db`) | **1 passed** |

The skip is currently an explicit infrastructure precondition in the failover test: it only proceeds when replication confirmation is available from the backend.

---

## Known Limitation: Sentinel TILT on Docker/Windows

When running multiple failover-heavy tests sequentially, tests that kill Redis masters can cause Docker CPU throttling on the Sentinel containers. Sentinel's timer interrupt (runs every 100ms) detects a >2s gap and enters TILT mode, refusing failover operations for 30 seconds.

**Impact:** After a chaos test kills a Redis master, Sentinel cannot promote the replica. Subsequent tests fail with "503 Service Unavailable" or "Item not found" because services can't reach a working master.

**Verification:** `docker compose logs sentinel-1 | grep tilt`

**Not a consistency bug.** The conservation invariant can still hold while availability temporarily drops. On this branch, the individual chaos scenario rerun passed on a clean stack.

### Current backend note: Dragonfly and `WAIT`

Fresh verification on this branch showed that the Dragonfly backend in the local stack responds to `WAIT` with `ERR unknown command 'WAIT'`. Because of that, this branch does **not** claim production use of `WAIT 1 5000`; the active failover defenses are the ones listed above and implemented in code today.

**Mitigation options:**
1. Run on Linux with 16+ CPU cores (eliminates Docker throttling)
2. Reduce docker-compose resource limits to fit available CPU
3. Run chaos tests individually: `pytest test/test_chaos.py::test_name -m integration`

---

## Reproduction

```bash
# Fresh reruns used for this report
docker compose up -d
python -m pytest test/test_executor.py test/test_recovery.py test/test_circuit_breaker.py test/test_orchestrator_core.py test/test_wal_metrics.py test/test_new_unit_tests.py -q
python -m pytest test/test_microservices.py test/test_stress.py test/test_crash_recovery.py -q -m integration
python -m pytest test/test_sentinel_failover.py -q -m integration
python -m pytest test/test_chaos.py::test_network_partition_stock_db -q -m integration
```
