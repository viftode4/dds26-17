"""New unit tests for Phase 2 improvements (tasks 2A–2E).

Covers paths not tested by the original test suite:
  2A: RecoveryWorker — EXECUTING, COMPENSATING, reconciliation, orphan handling, error path
  2B: CircuitBreaker — half-open → re-open on failure
  2C: WALEngine     — concurrent writes, pipeline partial failure, high-volume
  2D: TwoPCExecutor / SagaExecutor — commit retry, compensation retry, timeout, both-fail
  2E: NatsOrchestratorTransport — timeout, handler errors
"""
from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from orchestrator.definition import Step, TransactionDefinition
from orchestrator.executor import CircuitBreaker, SagaExecutor, TwoPCExecutor
from orchestrator.metrics import MetricsCollector
from orchestrator.recovery import RecoveryWorker
from orchestrator.wal import WALEngine


# ---------------------------------------------------------------------------
# Shared helpers (copied from test_recovery.py style)
# ---------------------------------------------------------------------------

async def _noop_sleep(*args, **kwargs):
    pass


def _wal_with_saga(saga_id: str, last_step: str,
                   data: dict | None = None) -> AsyncMock:
    wal = AsyncMock(spec=WALEngine)
    wal.log = AsyncMock()
    wal.log_terminal = AsyncMock()
    wal.get_incomplete_sagas = AsyncMock(return_value={
        saga_id: {
            "saga_id": saga_id,
            "last_step": last_step,
            "data": data or {},
            "created_at": time.time(),
        }
    })
    return wal


def _make_steps():
    return [Step("stock", "stock"), Step("payment", "payment")]


def _route_transport(transport, responses: dict[tuple[str, str], dict | list]):
    queues: dict = {}
    for key, val in responses.items():
        queues[key] = list(val) if isinstance(val, list) else [val]

    async def _send_and_wait(service, action, payload, timeout):
        key = (service, action)
        if key in queues and queues[key]:
            resp = queues[key].pop(0)
            if not queues[key]:
                queues[key].append(resp)
            return resp
        return {"event": "ok"}

    transport.send_and_wait = AsyncMock(side_effect=_send_and_wait)


def _make_executor(cls, mock_transport, mock_wal, circuit_breakers, metrics):
    return cls(transport=mock_transport, wal=mock_wal,
               circuit_breakers=circuit_breakers, metrics=metrics)


# ---------------------------------------------------------------------------
# 2A.1 – RecoveryWorker: WAL=EXECUTING → compensate all
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@patch("orchestrator.recovery.asyncio.sleep", side_effect=_noop_sleep)
async def test_recover_executing_compensates(mock_sleep):
    """WAL='EXECUTING' → compensate all services (task 2A.1)."""
    transport = AsyncMock()
    wal = _wal_with_saga("saga-exec", "EXECUTING", {"tx_name": "checkout"})
    tx_def = TransactionDefinition("checkout", _make_steps())

    _route_transport(transport, {
        ("stock", "compensate"): {"event": "compensated"},
        ("payment", "compensate"): {"event": "compensated"},
    })

    worker = RecoveryWorker(wal, transport, definitions={"checkout": tx_def})
    await worker.recover_incomplete_sagas()

    compensate_calls = [
        c for c in transport.send_and_wait.call_args_list
        if c.args[1] == "compensate"
    ]
    assert len(compensate_calls) >= 1, "No compensate calls made for EXECUTING state"
    assert wal.log.call_args_list[-1].args == ("saga-exec", "FAILED")


# ---------------------------------------------------------------------------
# 2A.2 – RecoveryWorker: WAL=COMPENSATING → retry compensation loop
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@patch("orchestrator.recovery.asyncio.sleep", side_effect=_noop_sleep)
async def test_recover_compensating_retries(mock_sleep):
    """WAL='COMPENSATING' → compensate all (idempotent retry) → FAILED (task 2A.2)."""
    transport = AsyncMock()
    wal = _wal_with_saga("saga-comp", "COMPENSATING", {"tx_name": "checkout"})
    tx_def = TransactionDefinition("checkout", _make_steps())

    # First compensate call fails, second succeeds
    _route_transport(transport, {
        ("stock", "compensate"): [
            {"event": "failed"},
            {"event": "compensated"},
        ],
        ("payment", "compensate"): {"event": "compensated"},
    })

    worker = RecoveryWorker(wal, transport, definitions={"checkout": tx_def})
    await worker.recover_incomplete_sagas()

    final_states = [c.args[1] for c in wal.log.call_args_list]
    assert "FAILED" in final_states


# ---------------------------------------------------------------------------
# 2A.3 – RecoveryWorker: selective compensation (only completed_steps)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@patch("orchestrator.recovery.asyncio.sleep", side_effect=_noop_sleep)
async def test_compensate_with_known_completed_steps(mock_sleep):
    """Only steps in completed_steps are compensated, not all steps (task 2A.3)."""
    transport = AsyncMock()
    # Stock executed, payment didn't: only stock should be compensated
    wal = _wal_with_saga("saga-sel", "COMPENSATING", {
        "tx_name": "checkout",
        "completed_steps": ["stock"],  # only stock completed
    })
    tx_def = TransactionDefinition("checkout", _make_steps())

    _route_transport(transport, {
        ("stock", "compensate"): {"event": "compensated"},
    })

    worker = RecoveryWorker(wal, transport, definitions={"checkout": tx_def})
    await worker.recover_incomplete_sagas()

    compensate_calls = transport.send_and_wait.call_args_list
    services_compensated = {c.args[0] for c in compensate_calls if c.args[1] == "compensate"}
    assert "stock" in services_compensated, "Stock should be compensated"
    assert "payment" not in services_compensated, (
        "Payment should NOT be compensated — it never executed"
    )


# ---------------------------------------------------------------------------
# 2A.4 – RecoveryWorker: exception during _recover_saga → catches and logs FAILED
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@patch("orchestrator.recovery.asyncio.sleep", side_effect=_noop_sleep)
async def test_recovery_exception_logs_failed(mock_sleep):
    """Exception during _recover_saga is caught and logged as FAILED (task 2A.4)."""
    transport = AsyncMock()
    wal = _wal_with_saga("saga-err", "COMMITTING", {"tx_name": "checkout"})
    tx_def = TransactionDefinition("checkout", _make_steps())

    # Transport always raises
    transport.send_and_wait = AsyncMock(side_effect=RuntimeError("NATS exploded"))

    worker = RecoveryWorker(wal, transport, definitions={"checkout": tx_def})

    # Limit retries inside _commit_all to prevent infinite loop
    original_send = worker._send_action_to_all

    call_count = 0
    async def _send_once_then_raise(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count > 2:
            raise RuntimeError("too many retries in test")
        await original_send(*args, **kwargs)

    with patch.object(RecoveryWorker, "_commit_all", AsyncMock(side_effect=RuntimeError("test abort"))):
         await worker.recover_incomplete_sagas()

    # The outer try/except in recover_incomplete_sagas catches the exception
    # and logs FAILED
    logged_states = [c.args[1] for c in wal.log.call_args_list]
    assert "FAILED" in logged_states, (
        f"Recovery exception should result in FAILED state, got: {logged_states}"
    )


# ---------------------------------------------------------------------------
# 2A.5 – RecoveryWorker: reconciliation loop invokes reconcile_fn
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_reconciliation_loop_calls_callback():
    """start_reconciliation() fires and invokes reconcile_fn (task 2A.5)."""
    transport = AsyncMock()
    wal = AsyncMock(spec=WALEngine)
    wal.get_incomplete_sagas = AsyncMock(return_value={})

    called = asyncio.Event()

    async def _my_reconcile():
        called.set()

    worker = RecoveryWorker(wal, transport, reconcile_fn=_my_reconcile)

    # Patch RECONCILIATION_INTERVAL to minimal and use real sleep
    with patch("orchestrator.recovery.RECONCILIATION_INTERVAL", 0.01):
        await worker.start_reconciliation()
        # Give the loop time to run its callback
        await asyncio.sleep(0.05)
        await worker.stop()

    assert called.is_set(), "reconcile_fn was never called"


# ---------------------------------------------------------------------------
# 2A.6 – RecoveryWorker: orphaned sagas aborted after timeout
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@patch("orchestrator.recovery.asyncio.sleep", side_effect=_noop_sleep)
async def test_abort_orphaned_sagas_by_age(mock_sleep):
    """Sagas older than ORPHAN_SAGA_TIMEOUT are aborted (task 2A.6)."""
    from orchestrator.recovery import ORPHAN_SAGA_TIMEOUT

    transport = AsyncMock()
    wal = AsyncMock(spec=WALEngine)

    old_age = time.time() - (ORPHAN_SAGA_TIMEOUT + 60)  # definitely orphaned
    wal.get_incomplete_sagas = AsyncMock(return_value={
        "saga-orphan": {
            "saga_id": "saga-orphan",
            "last_step": "PREPARING",
            "data": {"tx_name": "checkout"},
            "created_at": old_age,
        }
    })
    wal.log = AsyncMock()

    tx_def = TransactionDefinition("checkout", _make_steps())
    _route_transport(transport, {
        ("stock", "abort"): {"event": "aborted"},
        ("payment", "abort"): {"event": "aborted"},
    })

    worker = RecoveryWorker(wal, transport, definitions={"checkout": tx_def})
    await worker._abort_orphaned_sagas()

    abort_calls = [
        c for c in transport.send_and_wait.call_args_list
        if c.args[1] == "abort"
    ]
    assert len(abort_calls) == 2, f"Expected 2 abort calls, got {len(abort_calls)}"
    assert wal.log.call_args_list[-1].args == ("saga-orphan", "FAILED")


# ---------------------------------------------------------------------------
# 2A.7 – RecoveryWorker: orphaned COMMITTING saga is committed, not aborted
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@patch("orchestrator.recovery.asyncio.sleep", side_effect=_noop_sleep)
async def test_orphaned_committing_saga_completed(mock_sleep):
    """Orphaned COMMITTING saga → commit (not abort) → COMPLETED (task 2A.7)."""
    from orchestrator.recovery import ORPHAN_SAGA_TIMEOUT

    transport = AsyncMock()
    wal = AsyncMock(spec=WALEngine)

    old_age = time.time() - (ORPHAN_SAGA_TIMEOUT + 60)
    wal.get_incomplete_sagas = AsyncMock(return_value={
        "saga-comm-orphan": {
            "saga_id": "saga-comm-orphan",
            "last_step": "COMMITTING",
            "data": {"tx_name": "checkout"},
            "created_at": old_age,
        }
    })
    wal.log = AsyncMock()

    tx_def = TransactionDefinition("checkout", _make_steps())
    _route_transport(transport, {
        ("stock", "commit"): {"event": "committed"},
        ("payment", "commit"): {"event": "committed"},
    })

    worker = RecoveryWorker(wal, transport, definitions={"checkout": tx_def})
    await worker._abort_orphaned_sagas()

    commit_calls = [
        c for c in transport.send_and_wait.call_args_list
        if c.args[1] == "commit"
    ]
    abort_calls = [
        c for c in transport.send_and_wait.call_args_list
        if c.args[1] == "abort"
    ]
    assert len(commit_calls) == 2, "Should commit, not abort, a COMMITTING orphan"
    assert len(abort_calls) == 0, "Should NOT abort a COMMITTING orphan"
    assert wal.log.call_args_list[-1].args == ("saga-comm-orphan", "COMPLETED")


# ---------------------------------------------------------------------------
# 2B.1 – CircuitBreaker: half-open failure re-opens the circuit
# ---------------------------------------------------------------------------

def test_half_open_failure_reopens():
    """Failure in half-open state → re-opens circuit (task 2B.1)."""
    cb = CircuitBreaker(failure_threshold=2, recovery_timeout=30.0)
    # Trip the circuit
    cb.record_failure()
    cb.record_failure()
    assert cb._state == "open"
    # Manually move to half-open
    cb._state = "half-open"
    cb._probe_in_flight = False
    # A further failure should re-open
    cb.record_failure()
    assert cb._state == "open", (
        "Failure in half-open state should re-open the circuit"
    )
    assert cb.is_open() is True


# ---------------------------------------------------------------------------
# 2C.1 – WALEngine: concurrent writes don't corrupt state
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_concurrent_wal_writes():
    """Two sagas writing to the WAL simultaneously produce no cross-contamination (task 2C.1)."""
    from test_wal_metrics import _make_wal_db  # reuse helper
    db, pipe = _make_wal_db()

    # The original _make_wal_db returns a MagicMock for pipe.xadd.
    # To track concurrent arguments easily, let's keep a local list
    xadd_calls = []
    def record_xadd(*args, **kwargs):
        xadd_calls.append((args, kwargs))
        return pipe

    pipe.xadd.side_effect = record_xadd
    # Ensure pipe method chaining works by returning self
    for method in ["sadd", "hset", "srem", "delete"]:
        getattr(pipe, method).return_value = pipe

    wal = WALEngine(db)

    # Run two concurrent log operations
    await asyncio.gather(
        wal.log("saga-a", "PREPARING", {"tx_name": "checkout"}),
        wal.log("saga-b", "EXECUTING", {"tx_name": "checkout"}),
    )

    # Both should have written to the stream
    assert len(xadd_calls) == 2, (
        f"Expected 2 xadd calls (one per saga), got {len(xadd_calls)}"
    )
    # Verify saga IDs appear in the calls (not mixed up)
    # The signature in wal.py: pipe.xadd(self.STREAM_KEY, entry, maxlen=self.MAX_LEN, approximate=True)
    # Extract 'entry' which is the second argument (index 1) of the args tuple
    call_saga_ids = {args[1]["saga_id"] for args, kwargs in xadd_calls}
    assert "saga-a" in call_saga_ids
    assert "saga-b" in call_saga_ids


# ---------------------------------------------------------------------------
# 2C.2 – WALEngine: pipeline partial failure leaves no inconsistent state
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_wal_pipeline_partial_failure():
    """If pipeline.execute() raises, WALEngine propagates the error cleanly (task 2C.2)."""
    from test_wal_metrics import _make_wal_db
    db, pipe = _make_wal_db()
    pipe.execute = AsyncMock(side_effect=RuntimeError("Redis pipeline failed"))
    wal = WALEngine(db)

    with pytest.raises(RuntimeError, match="Redis pipeline failed"):
        await wal.log("saga-fail", "PREPARING")


# ---------------------------------------------------------------------------
# 2C.3 – WALEngine: high-volume get_incomplete_sagas correctness
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_wal_get_incomplete_with_high_volume():
    """1000 active sagas returned correctly from get_incomplete_sagas (task 2C.3)."""
    from test_wal_metrics import _make_wal_db
    db, pipe = _make_wal_db()
    wal = WALEngine(db)

    n = 1000
    saga_ids = [f"saga-{i}" for i in range(n)]

    db.sscan = AsyncMock(return_value=(0, saga_ids))

    async def _hgetall(key):
        saga_id = key.replace("{order-wal}:state:", "")
        return {
            "saga_id": saga_id,
            "step": "PREPARING",
            "timestamp": "1700000000.0",
        }

    db.hgetall = AsyncMock(side_effect=_hgetall)

    result = await wal.get_incomplete_sagas()
    assert len(result) == n, f"Expected {n} sagas, got {len(result)}"
    assert all(sid in result for sid in saga_ids[:5]), "First 5 sagas missing"


# ---------------------------------------------------------------------------
# 2D.1 – TwoPCExecutor: commit retry (commit_failed then committed) → COMPLETED
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_2pc_commit_with_retry(mock_transport, mock_wal, circuit_breakers, metrics, checkout_tx):
    """First commit returns commit_failed, second committed → COMPLETED (task 2D.1)."""
    _route_transport(mock_transport, {
        ("stock", "prepare"): {"event": "prepared"},
        ("payment", "prepare"): {"event": "prepared"},
        ("stock", "commit"): [{"event": "commit_failed"}, {"event": "committed"}],
        ("payment", "commit"): {"event": "committed"},
    })
    executor = _make_executor(TwoPCExecutor, mock_transport, mock_wal,
                              circuit_breakers, metrics)
    with patch("orchestrator.executor.asyncio.sleep", side_effect=_noop_sleep):
        result = await executor.execute(checkout_tx, "2pc-commit-retry",
                                        {"items": "[]", "user_id": "u1", "total_cost": "100",
                                         "tx_name": "checkout"})

    assert result["status"] == "success", f"Expected success, got: {result}"
    mock_wal.log_terminal.assert_called_with("2pc-commit-retry", "COMPLETED")


# ---------------------------------------------------------------------------
# 2D.2 – SagaExecutor: compensation failure retries, succeeds → FAILED
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_saga_compensation_failure_retries(mock_transport, mock_wal,
                                                  circuit_breakers, metrics, checkout_tx):
    """Compensation of stock fails first, retries, succeeds → overall FAILED (task 2D.2)."""
    _route_transport(mock_transport, {
        ("stock", "execute"): {"event": "executed"},
        ("payment", "execute"): {"event": "failed", "reason": "insufficient_credit"},
        # First compensate attempt fails, second succeeds
        ("stock", "compensate"): [{"event": "failed"}, {"event": "compensated"}],
    })
    executor = _make_executor(SagaExecutor, mock_transport, mock_wal,
                              circuit_breakers, metrics)
    with patch("orchestrator.executor.asyncio.sleep", side_effect=_noop_sleep):
        result = await executor.execute(checkout_tx, "saga-comp-retry",
                                        {"items": "[]", "user_id": "u1", "total_cost": "100",
                                         "tx_name": "checkout"})

    assert result["status"] == "failed"
    # Compensation must have been retried (at least 2 calls)
    comp_calls = [c for c in mock_transport.send_and_wait.call_args_list
                  if c.args[1] == "compensate"]
    assert len(comp_calls) >= 2, f"Expected retry, got {len(comp_calls)} compensate call(s)"
    mock_wal.log_terminal.assert_called_with("saga-comp-retry", "FAILED")


# ---------------------------------------------------------------------------
# 2D.3 – SagaExecutor: step timeout → treat as failure → abort/compensate
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_saga_step_timeout(mock_transport, mock_wal,
                                  circuit_breakers, metrics, checkout_tx):
    """Stock execute times out → treated as failure, compensated, status=failed (task 2D.3)."""
    # _try_step catches TimeoutError and returns {event:failed, reason:timeout}
    # Timeout is ambiguous so the executor adds it to completed_steps and
    # compensates.  Mock must return "compensated" for the compensate action.
    async def _timeout_then_ok(service, action, payload, timeout):
        if service == "stock" and action == "execute":
            raise asyncio.TimeoutError()
        if action == "compensate":
            return {"event": "compensated"}
        return {"event": "ok"}

    mock_transport.send_and_wait = AsyncMock(side_effect=_timeout_then_ok)
    executor = _make_executor(SagaExecutor, mock_transport, mock_wal,
                              circuit_breakers, metrics)

    result = await executor.execute(checkout_tx, "saga-timeout",
                                    {"items": "[]", "user_id": "u1", "total_cost": "100",
                                     "tx_name": "checkout"})

    assert result["status"] == "failed"
    assert "timeout" in result.get("error", ""), (
        f"Expected timeout error, got: {result}"
    )


# ---------------------------------------------------------------------------
# 2D.4 – TwoPCExecutor: transport exception during prepare → abort triggered
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_transport_exception_during_prepare(mock_transport, mock_wal,
                                                    circuit_breakers, metrics, checkout_tx):
    """Transport raises during prepare → treat as prepare failure → abort (task 2D.4)."""
    async def _explode(service, action, payload, timeout):
        if action == "prepare":
            raise ConnectionError("NATS gone")
        return {"event": "aborted"}

    mock_transport.send_and_wait = AsyncMock(side_effect=_explode)
    executor = _make_executor(TwoPCExecutor, mock_transport, mock_wal,
                              circuit_breakers, metrics)
    with patch("orchestrator.executor.asyncio.sleep", side_effect=_noop_sleep):
        result = await executor.execute(checkout_tx, "2pc-explode",
                                        {"items": "[]", "user_id": "u1", "total_cost": "100",
                                         "tx_name": "checkout"})

    assert result["status"] == "failed"
    wal_steps = [c.args[1] for c in mock_wal.log.call_args_list
                 if c.args[0] == "2pc-explode"]
    assert "ABORTING" in wal_steps, f"Expected ABORTING in WAL, got: {wal_steps}"


# ---------------------------------------------------------------------------
# 2D.5 – TwoPCExecutor: both services fail prepare simultaneously → clean abort
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_both_services_fail_prepare(mock_transport, mock_wal,
                                           circuit_breakers, metrics, checkout_tx):
    """Both stock AND payment fail prepare → ABORTING → FAILED (task 2D.5)."""
    _route_transport(mock_transport, {
        ("stock", "prepare"): {"event": "failed", "reason": "insufficient_stock"},
        ("payment", "prepare"): {"event": "failed", "reason": "insufficient_credit"},
        ("stock", "abort"): {"event": "aborted"},
        ("payment", "abort"): {"event": "aborted"},
    })
    executor = _make_executor(TwoPCExecutor, mock_transport, mock_wal,
                              circuit_breakers, metrics)
    with patch("orchestrator.executor.asyncio.sleep", side_effect=_noop_sleep):
        result = await executor.execute(checkout_tx, "2pc-both-fail",
                                        {"items": "[]", "user_id": "u1", "total_cost": "100",
                                         "tx_name": "checkout"})

    assert result["status"] == "failed"
    mock_wal.log_terminal.assert_called_with("2pc-both-fail", "FAILED")
    abort_calls = [c for c in mock_transport.send_and_wait.call_args_list
                   if c.args[1] == "abort"]
    assert len(abort_calls) == 2, f"Expected 2 abort calls, got {len(abort_calls)}"


# ---------------------------------------------------------------------------
# 2E.1 – NatsOrchestratorTransport: timeout → returns error dict, no crash
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_nats_timeout_returns_error():
    """NATS request timeout → returns {event:failed, reason:timeout} (task 2E.1)."""
    import sys
    sys.modules["nats"] = MagicMock()
    sys.modules["nats.aio.client"] = MagicMock()

    from common.nats_transport import NatsOrchestratorTransport, NatsTransport

    nats_base = AsyncMock(spec=NatsTransport)
    nats_base.request = AsyncMock(side_effect=asyncio.TimeoutError())

    transport = NatsOrchestratorTransport(nats_base)

    with patch("orchestrator.recovery.asyncio.sleep", side_effect=_noop_sleep):
        with patch("common.nats_transport.asyncio.sleep", side_effect=_noop_sleep):
            result = await transport.send_and_wait("stock", "prepare", {}, timeout=1.0)

    assert result.get("event") == "failed", f"Expected failed, got: {result}"
    assert "timeout" in result.get("reason", "").lower(), (
        f"Expected timeout in reason, got: {result}"
    )


# ---------------------------------------------------------------------------
# 2E.2 – NatsOrchestratorTransport: non-transient error returns immediately
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_nats_non_transient_error_returns_immediately():
    """Non-connection errors (e.g. bad payload) return immediately (task 2E.2 variant)."""
    import sys
    sys.modules["nats"] = MagicMock()
    sys.modules["nats.aio.client"] = MagicMock()

    from common.nats_transport import NatsOrchestratorTransport, NatsTransport

    nats_base = AsyncMock(spec=NatsTransport)
    nats_base.request = AsyncMock(
        side_effect=ValueError("invalid message format")
    )

    transport = NatsOrchestratorTransport(nats_base)
    result = await transport.send_and_wait("stock", "prepare", {}, timeout=5.0)

    assert result.get("event") == "failed"
    # Should NOT retry non-transient errors
    assert nats_base.request.call_count == 1, (
        f"Non-transient error should not be retried, called {nats_base.request.call_count} times"
    )


# ---------------------------------------------------------------------------
# 2E.3 – NatsTransport: error callback doesn't crash the transport
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_nats_error_callback_is_safe():
    """NatsTransport._error_cb logs without crashing (task 2E.3)."""
    import sys
    sys.modules["nats"] = MagicMock()
    sys.modules["nats.aio.client"] = MagicMock()

    from common.nats_transport import NatsTransport

    transport = NatsTransport("nats://localhost:4222")
    # Should not raise
    await transport._error_cb(Exception("test error"))
    await transport._disconnected_cb()
    await transport._reconnected_cb()
