"""Unit tests for RecoveryWorker -- crash recovery for incomplete sagas.

Pattern: create RecoveryWorker with mocked deps, call recover_incomplete_sagas(),
configure transport.send_and_wait responses to drive recovery paths.
Patch asyncio.sleep to skip delays.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from orchestrator.recovery import RecoveryWorker
from orchestrator.wal import WALEngine
from orchestrator.definition import TransactionDefinition, Step


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _noop_sleep(*args, **kwargs):
    """Instant replacement for asyncio.sleep in recovery code."""
    pass


def _wal_with_saga(saga_id: str, last_step: str, data: dict | None = None) -> AsyncMock:
    """Create a mock WAL that returns one incomplete saga."""
    wal = AsyncMock(spec=WALEngine)
    wal.log = AsyncMock()
    wal.log_terminal = AsyncMock()
    wal.get_incomplete_sagas = AsyncMock(return_value={
        saga_id: {
            "saga_id": saga_id,
            "last_step": last_step,
            "data": data or {},
        }
    })
    return wal


def _make_steps():
    return [
        Step("stock", "stock"),
        Step("payment", "payment"),
    ]


def _route_transport(transport, responses: dict[tuple[str, str], dict | list]):
    """Configure transport.send_and_wait to return responses based on (service, action)."""
    queues = {}
    for key, val in responses.items():
        if isinstance(val, list):
            queues[key] = list(val)
        else:
            queues[key] = [val]

    async def _send_and_wait(service, action, payload, timeout):
        key = (service, action)
        if key in queues and queues[key]:
            resp = queues[key].pop(0)
            if not queues[key]:
                queues[key].append(resp)
            return resp
        return {"event": "ok"}

    transport.send_and_wait = AsyncMock(side_effect=_send_and_wait)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@patch("orchestrator.recovery.asyncio.sleep", side_effect=_noop_sleep)
async def test_recover_started(mock_sleep):
    """WAL='STARTED' -> FAILED, no commands sent."""
    transport = AsyncMock()
    wal = _wal_with_saga("saga-started", "STARTED")

    worker = RecoveryWorker(wal, transport)
    await worker.recover_incomplete_sagas()

    wal.log.assert_called_with("saga-started", "FAILED")
    transport.send_and_wait.assert_not_called()


@pytest.mark.asyncio
@patch("orchestrator.recovery.asyncio.sleep", side_effect=_noop_sleep)
async def test_recover_preparing_aborts(mock_sleep):
    """WAL='PREPARING' -> abort to all services with verified retry, FAILED."""
    transport = AsyncMock()
    wal = _wal_with_saga("saga-prep", "PREPARING", {"tx_name": "checkout"})
    tx_def = TransactionDefinition("checkout", _make_steps())

    _route_transport(transport, {
        ("stock", "abort"): {"event": "aborted"},
        ("payment", "abort"): {"event": "aborted"},
    })

    worker = RecoveryWorker(wal, transport, definitions={"checkout": tx_def})
    await worker.recover_incomplete_sagas()

    # Verify abort calls were made
    abort_calls = [
        c for c in transport.send_and_wait.call_args_list
        if c.args[1] == "abort"
    ]
    # Task 1.9: tightened from >= 2 to == 2 (exactly one abort per service)
    assert len(abort_calls) == 2, f"Expected exactly 2 abort calls, got {len(abort_calls)}"
    assert wal.log.call_args_list[-1].args == ("saga-prep", "FAILED")


@pytest.mark.asyncio
@patch("orchestrator.recovery.asyncio.sleep", side_effect=_noop_sleep)
async def test_recover_committing_commits(mock_sleep):
    """WAL='COMMITTING', both respond 'committed' -> COMPLETED."""
    transport = AsyncMock()
    wal = _wal_with_saga("saga-commit", "COMMITTING", {"tx_name": "checkout"})
    tx_def = TransactionDefinition("checkout", _make_steps())

    _route_transport(transport, {
        ("stock", "commit"): {"event": "committed"},
        ("payment", "commit"): {"event": "committed"},
    })

    worker = RecoveryWorker(wal, transport, definitions={"checkout": tx_def})
    await worker.recover_incomplete_sagas()

    assert wal.log.call_args_list[-1].args == ("saga-commit", "COMPLETED")


@pytest.mark.asyncio
@patch("orchestrator.recovery.asyncio.sleep", side_effect=_noop_sleep)
async def test_recover_committing_retries(mock_sleep):
    """First 'commit_failed', then 'committed' -> retries then COMPLETED."""
    transport = AsyncMock()
    wal = _wal_with_saga("saga-retry", "COMMITTING", {"tx_name": "checkout"})
    tx_def = TransactionDefinition("checkout", _make_steps())

    _route_transport(transport, {
        ("stock", "commit"): [{"event": "commit_failed"}, {"event": "committed"}],
        ("payment", "commit"): {"event": "committed"},
    })

    worker = RecoveryWorker(wal, transport, definitions={"checkout": tx_def})
    await worker.recover_incomplete_sagas()

    final_states = [c.args[1] for c in wal.log.call_args_list]
    assert "COMPLETED" in final_states


@pytest.mark.asyncio
@patch("orchestrator.recovery.asyncio.sleep", side_effect=_noop_sleep)
async def test_recover_committing_retries_until_success(mock_sleep):
    """Commit retries indefinitely (never aborts) -- eventually succeeds."""
    transport = AsyncMock()
    wal = _wal_with_saga("saga-exh", "COMMITTING", {"tx_name": "checkout"})
    tx_def = TransactionDefinition("checkout", _make_steps())

    _route_transport(transport, {
        ("stock", "commit"): [
            {"event": "commit_failed"}, {"event": "commit_failed"},
            {"event": "commit_failed"}, {"event": "committed"},
        ],
        ("payment", "commit"): {"event": "committed"},
    })

    worker = RecoveryWorker(wal, transport, definitions={"checkout": tx_def})
    await worker.recover_incomplete_sagas()

    final_log = wal.log.call_args_list[-1].args
    assert final_log[1] == "COMPLETED", \
        "Commit must never fall back to abort -- irrevocable decision"


@pytest.mark.asyncio
@patch("orchestrator.recovery.asyncio.sleep", side_effect=_noop_sleep)
async def test_recover_aborting(mock_sleep):
    """WAL='ABORTING' -> abort to all services with verified retry, FAILED."""
    transport = AsyncMock()
    wal = _wal_with_saga("saga-abort", "ABORTING", {"tx_name": "checkout"})
    tx_def = TransactionDefinition("checkout", _make_steps())

    _route_transport(transport, {
        ("stock", "abort"): {"event": "aborted"},
        ("payment", "abort"): {"event": "aborted"},
    })

    worker = RecoveryWorker(wal, transport, definitions={"checkout": tx_def})
    await worker.recover_incomplete_sagas()

    abort_calls = [
        c for c in transport.send_and_wait.call_args_list
        if c.args[1] == "abort"
    ]
    # Task 1.9: tightened from >= 2 to == 2 (exactly one abort per service)
    assert len(abort_calls) == 2, f"Expected exactly 2 abort calls, got {len(abort_calls)}"
    assert wal.log.call_args_list[-1].args == ("saga-abort", "FAILED")


@pytest.mark.asyncio
@patch("orchestrator.recovery.asyncio.sleep", side_effect=_noop_sleep)
async def test_recover_no_definition_fallback(mock_sleep):
    """No tx_def found -> fire-and-forget fallback via transport."""
    transport = AsyncMock()
    wal = _wal_with_saga("saga-nodef", "COMMITTING", {"tx_name": "unknown_tx"})

    _route_transport(transport, {
        ("stock", "commit"): {"event": "committed"},
        ("payment", "commit"): {"event": "committed"},
    })

    worker = RecoveryWorker(wal, transport, definitions={})
    await worker.recover_incomplete_sagas()

    # Without a tx_def, recovery uses _send_action_to_all which has no steps to iterate
    # So it falls through to logging COMPLETED
    assert wal.log.call_args_list[-1].args == ("saga-nodef", "COMPLETED")


@pytest.mark.asyncio
@patch("orchestrator.recovery.asyncio.sleep", side_effect=_noop_sleep)
async def test_recover_executing_compensates(mock_sleep):
    """WAL='EXECUTING' -> compensate ALL steps (we don't know which completed), FAILED."""
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
    assert len(compensate_calls) == 2, f"Expected exactly 2 compensate calls, got {len(compensate_calls)}"
    assert wal.log.call_args_list[-1].args == ("saga-exec", "FAILED")


@pytest.mark.asyncio
@patch("orchestrator.recovery.asyncio.sleep", side_effect=_noop_sleep)
async def test_recover_compensating_retries(mock_sleep):
    """WAL='COMPENSATING' -> retry compensation for all steps -> FAILED."""
    transport = AsyncMock()
    wal = _wal_with_saga("saga-comp", "COMPENSATING", {"tx_name": "checkout"})
    tx_def = TransactionDefinition("checkout", _make_steps())

    _route_transport(transport, {
        ("stock", "compensate"): [{"event": "failed"}, {"event": "compensated"}],
        ("payment", "compensate"): {"event": "compensated"},
    })

    worker = RecoveryWorker(wal, transport, definitions={"checkout": tx_def})
    await worker.recover_incomplete_sagas()

    final_log = wal.log.call_args_list[-1].args
    assert final_log[1] == "FAILED", "COMPENSATING recovery must end in FAILED"


@pytest.mark.asyncio
@patch("orchestrator.recovery.asyncio.sleep", side_effect=_noop_sleep)
async def test_compensate_with_known_completed_steps(mock_sleep):
    """Only compensate steps listed in data['completed_steps'], not all steps."""
    transport = AsyncMock()
    # Only stock completed; payment never executed
    wal = _wal_with_saga("saga-partial", "COMPENSATING", {
        "tx_name": "checkout",
        "completed_steps": ["stock"],
    })
    tx_def = TransactionDefinition("checkout", _make_steps())

    _route_transport(transport, {
        ("stock", "compensate"): {"event": "compensated"},
    })

    worker = RecoveryWorker(wal, transport, definitions={"checkout": tx_def})
    await worker.recover_incomplete_sagas()

    compensate_calls = [
        c for c in transport.send_and_wait.call_args_list
        if c.args[1] == "compensate"
    ]
    services_compensated = {c.args[0] for c in compensate_calls}
    assert "stock" in services_compensated, "Stock should be compensated (it completed)"
    assert "payment" not in services_compensated, "Payment must NOT be compensated (it never ran)"
    assert wal.log.call_args_list[-1].args == ("saga-partial", "FAILED")


@pytest.mark.asyncio
@patch("orchestrator.recovery.asyncio.sleep", side_effect=_noop_sleep)
async def test_recovery_exception_logs_failed(mock_sleep):
    """Exception escaping _recover_saga is caught -> WAL logs FAILED, no crash.

    Uses an unknown WAL state so _recover_saga calls wal.log() directly
    (the else branch), which is made to raise. The outer handler in
    recover_incomplete_sagas catches it and logs FAILED on a second call.
    """
    transport = AsyncMock()
    # 'UNKNOWN_STATE' hits the else branch -> calls wal.log(saga_id, "FAILED") directly
    wal = _wal_with_saga("saga-exc", "UNKNOWN_STATE", {"tx_name": "checkout"})
    # First wal.log call raises (simulating e.g. Redis failure), second succeeds
    wal.log = AsyncMock(side_effect=[RuntimeError("redis down"), None])

    worker = RecoveryWorker(wal, transport, definitions={})
    # Must not raise — the outer except in recover_incomplete_sagas handles it
    await worker.recover_incomplete_sagas()

    # The exception handler retries wal.log with "FAILED"
    logged_states = [c.args[1] for c in wal.log.call_args_list]
    assert "FAILED" in logged_states, "Exception must result in WAL FAILED entry"
    transport.send_and_wait.assert_not_called()


@pytest.mark.asyncio
@patch("orchestrator.recovery.asyncio.sleep", side_effect=_noop_sleep)
async def test_recover_committing_commits_both_services(mock_sleep):
    """WAL='COMMITTING' -> commit sent to BOTH services -> COMPLETED."""
    transport = AsyncMock()
    wal = _wal_with_saga("saga-commit2", "COMMITTING", {"tx_name": "checkout"})
    tx_def = TransactionDefinition("checkout", _make_steps())

    _route_transport(transport, {
        ("stock", "commit"): {"event": "committed"},
        ("payment", "commit"): {"event": "committed"},
    })

    worker = RecoveryWorker(wal, transport, definitions={"checkout": tx_def})
    await worker.recover_incomplete_sagas()

    commit_calls = [
        c for c in transport.send_and_wait.call_args_list
        if c.args[1] == "commit"
    ]
    assert len(commit_calls) == 2, f"Expected exactly 2 commit calls, got {len(commit_calls)}"
    commit_services = {c.args[0] for c in commit_calls}
    assert commit_services == {"stock", "payment"}, (
        f"Commit not sent to both services: {commit_services}"
    )
    assert wal.log.call_args_list[-1].args == ("saga-commit2", "COMPLETED")


@pytest.mark.asyncio
@patch("orchestrator.recovery.time.time")
@patch("orchestrator.recovery.asyncio.sleep", side_effect=_noop_sleep)
async def test_abort_orphaned_sagas_by_age(mock_sleep, mock_time):
    """Sagas older than ORPHAN_SAGA_TIMEOUT (120s) are aborted."""
    transport = AsyncMock()

    now = 1000.0
    mock_time.return_value = now

    saga_id = "saga-orphan"
    wal = AsyncMock(spec=WALEngine.__class__)
    wal.log = AsyncMock()
    wal.log_terminal = AsyncMock()
    # Created 200s ago — well past the 120s threshold
    wal.get_incomplete_sagas = AsyncMock(return_value={
        saga_id: {
            "saga_id": saga_id,
            "last_step": "PREPARING",
            "created_at": now - 200,
            "data": {"tx_name": "checkout"},
        }
    })

    tx_def = TransactionDefinition("checkout", _make_steps())
    _route_transport(transport, {
        ("stock", "abort"): {"event": "aborted"},
        ("payment", "abort"): {"event": "aborted"},
    })

    worker = RecoveryWorker(wal, transport, definitions={"checkout": tx_def})
    await worker._abort_orphaned_sagas()

    abort_calls = [c for c in transport.send_and_wait.call_args_list if c.args[1] == "abort"]
    assert len(abort_calls) == 2, f"Expected 2 abort calls for orphaned saga, got {len(abort_calls)}"
    assert wal.log.call_args_list[-1].args == (saga_id, "FAILED")


@pytest.mark.asyncio
@patch("orchestrator.recovery.time.time")
@patch("orchestrator.recovery.asyncio.sleep", side_effect=_noop_sleep)
async def test_orphaned_committing_saga_completed(mock_sleep, mock_time):
    """Orphaned saga in COMMITTING state -> committed (never aborted)."""
    transport = AsyncMock()

    now = 1000.0
    mock_time.return_value = now

    saga_id = "saga-orphan-commit"
    wal = AsyncMock(spec=WALEngine.__class__)
    wal.log = AsyncMock()
    wal.log_terminal = AsyncMock()
    wal.get_incomplete_sagas = AsyncMock(return_value={
        saga_id: {
            "saga_id": saga_id,
            "last_step": "COMMITTING",
            "created_at": now - 200,
            "data": {"tx_name": "checkout"},
        }
    })

    tx_def = TransactionDefinition("checkout", _make_steps())
    _route_transport(transport, {
        ("stock", "commit"): {"event": "committed"},
        ("payment", "commit"): {"event": "committed"},
    })

    worker = RecoveryWorker(wal, transport, definitions={"checkout": tx_def})
    await worker._abort_orphaned_sagas()

    commit_calls = [c for c in transport.send_and_wait.call_args_list if c.args[1] == "commit"]
    abort_calls = [c for c in transport.send_and_wait.call_args_list if c.args[1] == "abort"]
    assert len(commit_calls) == 2, "Orphaned COMMITTING saga must be committed"
    assert len(abort_calls) == 0, "Orphaned COMMITTING saga must NEVER be aborted"
    assert wal.log.call_args_list[-1].args == (saga_id, "COMPLETED")


@pytest.mark.asyncio
async def test_reconciliation_loop_calls_callback():
    """start_reconciliation() -> _reconcile() invokes reconcile_fn."""
    transport = AsyncMock()
    wal = AsyncMock(spec=WALEngine.__class__)
    wal.log = AsyncMock()
    wal.get_incomplete_sagas = AsyncMock(return_value={})

    callback_called = False

    async def _my_reconcile():
        nonlocal callback_called
        callback_called = True

    worker = RecoveryWorker(wal, transport, reconcile_fn=_my_reconcile)
    await worker._reconcile()

    assert callback_called, "reconcile_fn must be called by _reconcile()"
