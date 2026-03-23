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
