"""Unit tests for TwoPCExecutor and SagaExecutor.

Pattern: launch executor.execute() in an asyncio.Task, then resolve
OutboxReader futures from the test to simulate service responses.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from orchestrator.executor import (
    TwoPCExecutor,
    SagaExecutor,
    OutboxReader,
    CircuitBreaker,
    STEP_TIMEOUT,
    CONFIRM_MAX_RETRIES,
)
from orchestrator.definition import TransactionDefinition, Step
from orchestrator.wal import WALEngine
from orchestrator.metrics import MetricsCollector


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_executor(cls, service_dbs, outbox_reader, mock_wal, circuit_breakers, metrics):
    return cls(
        service_dbs=service_dbs,
        outbox_reader=outbox_reader,
        wal=mock_wal,
        circuit_breakers=circuit_breakers,
        metrics=metrics,
    )


def _resolve_after(outbox_reader: OutboxReader, saga_id: str, service: str,
                    event_data: dict, delay: float = 0.01):
    """Schedule a future resolution after a tiny delay (simulates service response)."""
    async def _go():
        await asyncio.sleep(delay)
        outbox_reader.resolve(saga_id, service, event_data)
    asyncio.get_event_loop().create_task(_go())


class _ResponseSequencer:
    """Drives outbox resolution for multi-phase protocols.

    Watches mock xadd calls to detect when commands are sent, then resolves
    the corresponding outbox futures with pre-configured responses.
    Tracks all xadd calls in ``call_log`` for assertions.
    """
    def __init__(self, outbox_reader: OutboxReader, service_dbs: dict,
                 saga_id: str, responses: dict[str, list[dict]]):
        self.outbox_reader = outbox_reader
        self.saga_id = saga_id
        self._queues: dict[str, list[dict]] = {svc: list(resps) for svc, resps in responses.items()}
        # call_log: [(service, stream, fields), ...]
        self.call_log: list[tuple[str, str, dict]] = []

        for svc, db in service_dbs.items():
            original_xadd = db.xadd
            db.xadd = self._make_xadd_hook(svc, original_xadd)

    def _make_xadd_hook(self, service: str, original_xadd):
        async def _hooked_xadd(stream, fields, *args, **kwargs):
            self.call_log.append((service, stream, dict(fields)))
            result = await original_xadd(stream, fields, *args, **kwargs)
            if service in self._queues and self._queues[service]:
                resp = self._queues[service].pop(0)
                if resp is not None:
                    await asyncio.sleep(0.005)
                    self.outbox_reader.resolve(self.saga_id, service, resp)
            return result
        return _hooked_xadd

    def cancel_calls(self, service: str | None = None) -> list[tuple[str, str, dict]]:
        """Return all xadd calls that sent a 'cancel' action."""
        return [
            (svc, stream, fields) for svc, stream, fields in self.call_log
            if fields.get("action") == "cancel" and (service is None or svc == service)
        ]


# ---------------------------------------------------------------------------
# 2PC Tests
# ---------------------------------------------------------------------------

class TestTwoPCExecutor:

    @pytest.mark.asyncio
    async def test_2pc_happy_path(self, service_dbs, outbox_reader, mock_wal,
                                   circuit_breakers, metrics, checkout_tx):
        """Both services reserve → presumed commit (confirms fire-and-forget)."""
        executor = _make_executor(TwoPCExecutor, service_dbs, outbox_reader,
                                  mock_wal, circuit_breakers, metrics)
        saga_id = "2pc-happy"
        ctx = {"items": "[]", "user_id": "u1", "total_cost": "100", "tx_name": "checkout"}

        seq = _ResponseSequencer(outbox_reader, service_dbs, saga_id, {
            "stock": [{"event": "reserved"}],
            "payment": [{"event": "reserved"}],
        })

        result = await executor.execute(checkout_tx, saga_id, ctx)

        assert result["status"] == "success"
        # WAL progression: PREPARING → COMMITTING (no COMPLETED — recovery completes it)
        wal_steps = [call.args[1] for call in mock_wal.log.call_args_list
                     if call.args[0] == saga_id]
        assert wal_steps == ["PREPARING", "COMMITTING"]
        # Verify confirms were sent (fire-and-forget)
        confirm_calls = [(s, st, f) for s, st, f in seq.call_log if f.get("action") == "confirm"]
        assert len(confirm_calls) >= 2, "Confirm must be sent to both services"

    @pytest.mark.asyncio
    async def test_2pc_reserve_fails(self, service_dbs, outbox_reader, mock_wal,
                                      circuit_breakers, metrics, checkout_tx):
        """Stock returns 'failed' during prepare → cancel broadcast, FAILED."""
        executor = _make_executor(TwoPCExecutor, service_dbs, outbox_reader,
                                  mock_wal, circuit_breakers, metrics)
        saga_id = "2pc-fail"
        ctx = {"items": "[]", "user_id": "u1", "total_cost": "100", "tx_name": "checkout"}

        _ResponseSequencer(outbox_reader, service_dbs, saga_id, {
            "stock": [{"event": "failed", "reason": "insufficient_stock"},
                      {"event": "cancelled"}],
            "payment": [{"event": "reserved"}, {"event": "cancelled"}],
        })

        result = await executor.execute(checkout_tx, saga_id, ctx)

        assert result["status"] == "failed"
        assert "insufficient_stock" in result.get("error", "")
        wal_steps = [call.args[1] for call in mock_wal.log.call_args_list
                     if call.args[0] == saga_id]
        assert wal_steps == ["PREPARING", "ABORTING", "FAILED"]

    @pytest.mark.asyncio
    async def test_2pc_presumed_commit_sends_confirms(self, service_dbs, outbox_reader,
                                                       mock_wal, circuit_breakers, metrics,
                                                       checkout_tx):
        """Presumed commit: reserves succeed → confirms sent fire-and-forget → success."""
        executor = _make_executor(TwoPCExecutor, service_dbs, outbox_reader,
                                  mock_wal, circuit_breakers, metrics)
        saga_id = "2pc-presumed"
        ctx = {"items": "[]", "user_id": "u1", "total_cost": "100", "tx_name": "checkout"}

        seq = _ResponseSequencer(outbox_reader, service_dbs, saga_id, {
            "stock": [{"event": "reserved"}],
            "payment": [{"event": "reserved"}],
        })

        result = await executor.execute(checkout_tx, saga_id, ctx)

        assert result["status"] == "success"
        # Confirms sent to both services (fire-and-forget)
        confirm_calls = [(s, st, f) for s, st, f in seq.call_log if f.get("action") == "confirm"]
        assert len(confirm_calls) == 2
        # WAL stays COMMITTING — recovery worker completes it
        wal_steps = [call.args[1] for call in mock_wal.log.call_args_list
                     if call.args[0] == saga_id]
        assert wal_steps == ["PREPARING", "COMMITTING"]

    @pytest.mark.asyncio
    async def test_circuit_breaker_fast_fail(self, service_dbs, outbox_reader, mock_wal,
                                              circuit_breakers, metrics, checkout_tx):
        """Circuit breaker open → WAL FAILED immediately, no commands sent."""
        executor = _make_executor(TwoPCExecutor, service_dbs, outbox_reader,
                                  mock_wal, circuit_breakers, metrics)
        saga_id = "2pc-cb"
        ctx = {"items": "[]", "user_id": "u1", "total_cost": "100", "tx_name": "checkout"}

        # Trip the circuit breaker for stock
        for _ in range(10):
            circuit_breakers["stock"].record_failure()
        assert circuit_breakers["stock"].is_open()

        result = await executor.execute(checkout_tx, saga_id, ctx)

        assert result["status"] == "failed"
        assert "unavailable" in result["error"]
        # No commands should have been sent
        service_dbs["stock"].xadd.assert_not_called()
        service_dbs["payment"].xadd.assert_not_called()
        # WAL should show FAILED
        mock_wal.log.assert_called_with(saga_id, "FAILED")


# ---------------------------------------------------------------------------
# Saga Tests
# ---------------------------------------------------------------------------

class TestSagaExecutor:

    @pytest.mark.asyncio
    async def test_saga_happy_path(self, service_dbs, outbox_reader, mock_wal,
                                    circuit_breakers, metrics, checkout_tx):
        """Both services reserve → presumed commit (confirms fire-and-forget)."""
        executor = _make_executor(SagaExecutor, service_dbs, outbox_reader,
                                  mock_wal, circuit_breakers, metrics)
        saga_id = "saga-happy"
        ctx = {"items": "[]", "user_id": "u1", "total_cost": "100", "tx_name": "checkout"}

        seq = _ResponseSequencer(outbox_reader, service_dbs, saga_id, {
            "stock": [{"event": "reserved"}],
            "payment": [{"event": "reserved"}],
        })

        result = await executor.execute(checkout_tx, saga_id, ctx)

        assert result["status"] == "success"
        # WAL: TRYING → CONFIRMING (no COMPLETED — recovery completes it)
        wal_steps = [call.args[1] for call in mock_wal.log.call_args_list
                     if call.args[0] == saga_id]
        assert wal_steps == ["TRYING", "CONFIRMING"]
        # Confirms sent to both services (fire-and-forget)
        confirm_calls = [(s, st, f) for s, st, f in seq.call_log if f.get("action") == "confirm"]
        assert len(confirm_calls) == 2

    @pytest.mark.asyncio
    async def test_saga_reserve_fails(self, service_dbs, outbox_reader, mock_wal,
                                       circuit_breakers, metrics, checkout_tx):
        """Payment returns 'failed' → compensate all, FAILED."""
        executor = _make_executor(SagaExecutor, service_dbs, outbox_reader,
                                  mock_wal, circuit_breakers, metrics)
        saga_id = "saga-fail"
        ctx = {"items": "[]", "user_id": "u1", "total_cost": "100", "tx_name": "checkout"}

        _ResponseSequencer(outbox_reader, service_dbs, saga_id, {
            "stock": [{"event": "reserved"}, {"event": "cancelled"}],
            "payment": [{"event": "failed", "reason": "insufficient_credit"},
                        {"event": "cancelled"}],
        })

        result = await executor.execute(checkout_tx, saga_id, ctx)

        assert result["status"] == "failed"
        wal_steps = [call.args[1] for call in mock_wal.log.call_args_list
                     if call.args[0] == saga_id]
        assert wal_steps == ["TRYING", "COMPENSATING", "FAILED"]

    @pytest.mark.asyncio
    async def test_saga_presumed_commit_sends_confirms(self, service_dbs, outbox_reader,
                                                        mock_wal, circuit_breakers, metrics,
                                                        checkout_tx):
        """Presumed commit: reserves succeed → confirms sent fire-and-forget → success."""
        executor = _make_executor(SagaExecutor, service_dbs, outbox_reader,
                                  mock_wal, circuit_breakers, metrics)
        saga_id = "saga-presumed"
        ctx = {"items": "[]", "user_id": "u1", "total_cost": "100", "tx_name": "checkout"}

        seq = _ResponseSequencer(outbox_reader, service_dbs, saga_id, {
            "stock": [{"event": "reserved"}],
            "payment": [{"event": "reserved"}],
        })

        result = await executor.execute(checkout_tx, saga_id, ctx)

        assert result["status"] == "success"
        # Confirms sent to both services (fire-and-forget)
        confirm_calls = [(s, st, f) for s, st, f in seq.call_log if f.get("action") == "confirm"]
        assert len(confirm_calls) == 2
        # WAL stays CONFIRMING — recovery worker handles completion
        wal_steps = [call.args[1] for call in mock_wal.log.call_args_list
                     if call.args[0] == saga_id]
        assert wal_steps == ["TRYING", "CONFIRMING"]
