"""Unit tests for RecoveryWorker — crash recovery for incomplete sagas.

Pattern: create RecoveryWorker with mocked deps, call recover_incomplete_sagas(),
resolve outbox futures to drive the confirm path. Patch asyncio.sleep to skip delays.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from orchestrator.recovery import RecoveryWorker
from orchestrator.executor import OutboxReader
from orchestrator.wal import WALEngine
from orchestrator.definition import TransactionDefinition, Step


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _noop_sleep(*args, **kwargs):
    """Instant replacement for asyncio.sleep in recovery code."""
    pass


def _make_mock_redis() -> AsyncMock:
    db = AsyncMock()
    db.xadd = AsyncMock(return_value="1-0")
    return db


def _wal_with_saga(saga_id: str, last_step: str, data: dict | None = None) -> AsyncMock:
    """Create a mock WAL that returns one incomplete saga."""
    wal = AsyncMock(spec=WALEngine)
    wal.log = AsyncMock()
    wal.get_incomplete_sagas = AsyncMock(return_value={
        saga_id: {
            "saga_id": saga_id,
            "last_step": last_step,
            "data": data or {},
            "msg_id": "1000000000000-0",
        }
    })
    return wal


def _make_steps():
    return [
        Step("stock", "stock"),
        Step("payment", "payment"),
    ]


class _XaddResponseHook:
    """Hook into xadd to resolve outbox futures when confirm commands are sent.
    Tracks all calls in ``call_log`` for assertions.
    """

    def __init__(self, outbox_reader: OutboxReader, saga_id: str,
                 responses: dict[str, list[dict]], service_dbs: dict):
        self.outbox_reader = outbox_reader
        self.saga_id = saga_id
        self._queues = {svc: list(resps) for svc, resps in responses.items()}
        self.call_log: list[tuple[str, str, dict]] = []

        for svc, db in service_dbs.items():
            original_xadd = db.xadd
            db.xadd = self._make_hook(svc, original_xadd)

    def _make_hook(self, service, original_xadd):
        hook = self

        async def _hooked(stream, fields, *args, **kwargs):
            hook.call_log.append((service, stream, dict(fields)))
            result = await original_xadd(stream, fields, *args, **kwargs)
            if fields.get("action") == "commit" and service in hook._queues and hook._queues[service]:
                resp = hook._queues[service].pop(0)
                # Schedule resolution as a background task so the caller can
                # create_waiter() before we resolve it.
                async def _resolve_later(svc=service, r=resp):
                    await asyncio.sleep(0.02)
                    hook.outbox_reader.resolve(hook.saga_id, svc, r)
                asyncio.get_event_loop().create_task(_resolve_later())
            return result
        return _hooked

    def abort_calls(self, service: str | None = None) -> list[tuple[str, str, dict]]:
        return [
            (svc, stream, fields) for svc, stream, fields in self.call_log
            if fields.get("action") == "abort" and (service is None or svc == service)
        ]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@patch("orchestrator.recovery.asyncio.sleep", side_effect=_noop_sleep)
async def test_recover_started(mock_sleep):
    """WAL='STARTED' -> FAILED, no commands sent."""
    service_dbs = {"stock": _make_mock_redis(), "payment": _make_mock_redis()}
    wal = _wal_with_saga("saga-started", "STARTED")
    outbox = OutboxReader()

    worker = RecoveryWorker(wal, service_dbs, outbox)
    await worker.recover_incomplete_sagas()

    wal.log.assert_called_with("saga-started", "FAILED")
    service_dbs["stock"].xadd.assert_not_called()
    service_dbs["payment"].xadd.assert_not_called()


@pytest.mark.asyncio
@patch("orchestrator.recovery.asyncio.sleep", side_effect=_noop_sleep)
async def test_recover_preparing_aborts(mock_sleep):
    """WAL='PREPARING' -> abort to all services, FAILED."""
    service_dbs = {"stock": _make_mock_redis(), "payment": _make_mock_redis()}
    wal = _wal_with_saga("saga-prep", "PREPARING", {"tx_name": "checkout"})
    outbox = OutboxReader()
    tx_def = TransactionDefinition("checkout", _make_steps())

    worker = RecoveryWorker(wal, service_dbs, outbox, definitions={"checkout": tx_def})
    await worker.recover_incomplete_sagas()

    stock_calls = service_dbs["stock"].xadd.call_args_list
    assert any(c.args[1].get("action") == "abort" for c in stock_calls)
    payment_calls = service_dbs["payment"].xadd.call_args_list
    assert any(c.args[1].get("action") == "abort" for c in payment_calls)
    assert wal.log.call_args_list[-1].args == ("saga-prep", "FAILED")


@pytest.mark.asyncio
@patch("orchestrator.recovery.asyncio.sleep", side_effect=_noop_sleep)
async def test_recover_committing_commits(mock_sleep):
    """WAL='COMMITTING', both respond 'committed' -> COMPLETED."""
    service_dbs = {"stock": _make_mock_redis(), "payment": _make_mock_redis()}
    wal = _wal_with_saga("saga-commit", "COMMITTING", {"tx_name": "checkout"})
    outbox = OutboxReader()
    tx_def = TransactionDefinition("checkout", _make_steps())

    _XaddResponseHook(outbox, "saga-commit", {
        "stock": [{"event": "committed"}],
        "payment": [{"event": "committed"}],
    }, service_dbs)

    worker = RecoveryWorker(wal, service_dbs, outbox, definitions={"checkout": tx_def})
    await worker.recover_incomplete_sagas()

    assert wal.log.call_args_list[-1].args == ("saga-commit", "COMPLETED")


@pytest.mark.asyncio
@patch("orchestrator.recovery.asyncio.sleep", side_effect=_noop_sleep)
async def test_recover_committing_retries(mock_sleep):
    """First 'commit_failed', then 'committed' -> retries then COMPLETED."""
    service_dbs = {"stock": _make_mock_redis(), "payment": _make_mock_redis()}
    wal = _wal_with_saga("saga-retry", "COMMITTING", {"tx_name": "checkout"})
    outbox = OutboxReader()
    tx_def = TransactionDefinition("checkout", _make_steps())

    _XaddResponseHook(outbox, "saga-retry", {
        "stock": [{"event": "commit_failed"}, {"event": "committed"}],
        "payment": [{"event": "committed"}, {"event": "committed"}],
    }, service_dbs)

    worker = RecoveryWorker(wal, service_dbs, outbox, definitions={"checkout": tx_def})
    await worker.recover_incomplete_sagas()

    final_states = [c.args[1] for c in wal.log.call_args_list]
    assert "COMPLETED" in final_states


@pytest.mark.asyncio
@patch("orchestrator.recovery.asyncio.sleep", side_effect=_noop_sleep)
async def test_recover_committing_retries_until_success(mock_sleep):
    """Commit retries indefinitely (never aborts) — eventually succeeds."""
    service_dbs = {"stock": _make_mock_redis(), "payment": _make_mock_redis()}
    wal = _wal_with_saga("saga-exh", "COMMITTING", {"tx_name": "checkout"})
    outbox = OutboxReader()
    tx_def = TransactionDefinition("checkout", _make_steps())

    # Stock fails 3 times then succeeds; payment succeeds immediately
    _XaddResponseHook(outbox, "saga-exh", {
        "stock": [{"event": "commit_failed"}, {"event": "commit_failed"},
                  {"event": "commit_failed"}, {"event": "committed"}],
        "payment": [{"event": "committed"}, {"event": "committed"},
                    {"event": "committed"}, {"event": "committed"}],
    }, service_dbs)

    worker = RecoveryWorker(wal, service_dbs, outbox, definitions={"checkout": tx_def})
    await worker.recover_incomplete_sagas()

    final_log = wal.log.call_args_list[-1].args
    assert final_log[1] == "COMPLETED", \
        "Commit must never fall back to abort — irrevocable decision"


@pytest.mark.asyncio
@patch("orchestrator.recovery.asyncio.sleep", side_effect=_noop_sleep)
async def test_recover_aborting(mock_sleep):
    """WAL='ABORTING' -> abort to all services, FAILED."""
    service_dbs = {"stock": _make_mock_redis(), "payment": _make_mock_redis()}
    wal = _wal_with_saga("saga-abort", "ABORTING", {"tx_name": "checkout"})
    outbox = OutboxReader()
    tx_def = TransactionDefinition("checkout", _make_steps())

    worker = RecoveryWorker(wal, service_dbs, outbox, definitions={"checkout": tx_def})
    await worker.recover_incomplete_sagas()

    stock_calls = service_dbs["stock"].xadd.call_args_list
    assert any(c.args[1].get("action") == "abort" for c in stock_calls)
    assert wal.log.call_args_list[-1].args == ("saga-abort", "FAILED")


@pytest.mark.asyncio
@patch("orchestrator.recovery.asyncio.sleep", side_effect=_noop_sleep)
async def test_recover_no_definition_fallback(mock_sleep):
    """No tx_def found -> fire-and-forget fallback (bare commands to all services)."""
    service_dbs = {"stock": _make_mock_redis(), "payment": _make_mock_redis()}
    wal = _wal_with_saga("saga-nodef", "COMMITTING", {"tx_name": "unknown_tx"})
    outbox = OutboxReader()

    worker = RecoveryWorker(wal, service_dbs, outbox, definitions={})
    await worker.recover_incomplete_sagas()

    stock_calls = service_dbs["stock"].xadd.call_args_list
    assert any(c.args[1].get("action") == "commit" for c in stock_calls)
    payment_calls = service_dbs["payment"].xadd.call_args_list
    assert any(c.args[1].get("action") == "commit" for c in payment_calls)
    assert wal.log.call_args_list[-1].args == ("saga-nodef", "COMPLETED")
