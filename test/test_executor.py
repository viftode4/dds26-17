"""Unit tests for TwoPCExecutor and SagaExecutor.

Architecture:
- 2PC: parallel prepare (lock, no mutations) → commit (apply) or abort (release locks)
- Saga: sequential execute (direct mutation) → compensate in reverse on failure
- WAL writes awaited before side effects (PREPARING, EXECUTING, COMMITTING, COMPENSATING)
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

    def action_calls(self, action: str, service: str | None = None) -> list[tuple[str, str, dict]]:
        return [
            (svc, stream, fields) for svc, stream, fields in self.call_log
            if fields.get("action") == action and (service is None or svc == service)
        ]


# ---------------------------------------------------------------------------
# 2PC Tests
# ---------------------------------------------------------------------------

class TestTwoPCExecutor:

    @pytest.mark.asyncio
    async def test_2pc_happy_path(self, service_dbs, outbox_reader, mock_wal,
                                   circuit_breakers, metrics, checkout_tx):
        """Both services prepare → commit → success."""
        executor = _make_executor(TwoPCExecutor, service_dbs, outbox_reader,
                                  mock_wal, circuit_breakers, metrics)
        saga_id = "2pc-happy"
        ctx = {"items": "[]", "user_id": "u1", "total_cost": "100", "tx_name": "checkout"}

        seq = _ResponseSequencer(outbox_reader, service_dbs, saga_id, {
            "stock": [{"event": "prepared"}, {"event": "committed"}],
            "payment": [{"event": "prepared"}, {"event": "committed"}],
        })

        result = await executor.execute(checkout_tx, saga_id, ctx)

        assert result["status"] == "success"
        # WAL progression: PREPARING → COMMITTING → COMPLETED
        wal_steps = [call.args[1] for call in mock_wal.log.call_args_list
                     if call.args[0] == saga_id]
        assert wal_steps == ["PREPARING", "COMMITTING", "COMPLETED"]
        # Commits were sent and verified
        assert len(seq.action_calls("commit")) == 2

    @pytest.mark.asyncio
    async def test_2pc_prepare_fails(self, service_dbs, outbox_reader, mock_wal,
                                      circuit_breakers, metrics, checkout_tx):
        """Stock returns 'failed' during prepare → abort broadcast, FAILED."""
        executor = _make_executor(TwoPCExecutor, service_dbs, outbox_reader,
                                  mock_wal, circuit_breakers, metrics)
        saga_id = "2pc-fail"
        ctx = {"items": "[]", "user_id": "u1", "total_cost": "100", "tx_name": "checkout"}

        _ResponseSequencer(outbox_reader, service_dbs, saga_id, {
            "stock": [{"event": "failed", "reason": "insufficient_stock"},
                      {"event": "aborted"}],
            "payment": [{"event": "prepared"}, {"event": "aborted"}],
        })

        result = await executor.execute(checkout_tx, saga_id, ctx)

        assert result["status"] == "failed"
        assert "insufficient_stock" in result.get("error", "")
        wal_steps = [call.args[1] for call in mock_wal.log.call_args_list
                     if call.args[0] == saga_id]
        assert wal_steps == ["PREPARING", "ABORTING", "FAILED"]

    @pytest.mark.asyncio
    async def test_2pc_verified_commits(self, service_dbs, outbox_reader,
                                          mock_wal, circuit_breakers, metrics,
                                          checkout_tx):
        """Prepares succeed → commits sent and verified → success."""
        executor = _make_executor(TwoPCExecutor, service_dbs, outbox_reader,
                                  mock_wal, circuit_breakers, metrics)
        saga_id = "2pc-verified"
        ctx = {"items": "[]", "user_id": "u1", "total_cost": "100", "tx_name": "checkout"}

        seq = _ResponseSequencer(outbox_reader, service_dbs, saga_id, {
            "stock": [{"event": "prepared"}, {"event": "committed"}],
            "payment": [{"event": "prepared"}, {"event": "committed"}],
        })

        result = await executor.execute(checkout_tx, saga_id, ctx)

        assert result["status"] == "success"
        commit_calls = seq.action_calls("commit")
        assert len(commit_calls) == 2
        wal_steps = [call.args[1] for call in mock_wal.log.call_args_list
                     if call.args[0] == saga_id]
        assert wal_steps == ["PREPARING", "COMMITTING", "COMPLETED"]

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
        service_dbs["stock"].xadd.assert_not_called()
        service_dbs["payment"].xadd.assert_not_called()
        mock_wal.log.assert_called_with(saga_id, "FAILED")


# ---------------------------------------------------------------------------
# Saga Tests
# ---------------------------------------------------------------------------

class TestSagaExecutor:

    @pytest.mark.asyncio
    async def test_saga_happy_path(self, service_dbs, outbox_reader, mock_wal,
                                    circuit_breakers, metrics, checkout_tx):
        """Sequential execute: stock then payment → COMPLETED (no confirm phase)."""
        executor = _make_executor(SagaExecutor, service_dbs, outbox_reader,
                                  mock_wal, circuit_breakers, metrics)
        saga_id = "saga-happy"
        ctx = {"items": "[]", "user_id": "u1", "total_cost": "100", "tx_name": "checkout"}

        seq = _ResponseSequencer(outbox_reader, service_dbs, saga_id, {
            "stock": [{"event": "executed"}],
            "payment": [{"event": "executed"}],
        })

        result = await executor.execute(checkout_tx, saga_id, ctx)

        assert result["status"] == "success"
        # WAL: EXECUTING → COMPLETED (no confirm phase!)
        wal_steps = [call.args[1] for call in mock_wal.log.call_args_list
                     if call.args[0] == saga_id]
        assert wal_steps == ["EXECUTING", "COMPLETED"]
        # No confirm calls — saga mutations are immediately applied
        assert len(seq.action_calls("commit")) == 0
        assert len(seq.action_calls("confirm")) == 0

    @pytest.mark.asyncio
    async def test_saga_early_abort_on_first_failure(self, service_dbs, outbox_reader,
                                                      mock_wal, circuit_breakers, metrics,
                                                      checkout_tx):
        """Stock fails → payment never tried (early abort), no compensation needed, FAILED."""
        executor = _make_executor(SagaExecutor, service_dbs, outbox_reader,
                                  mock_wal, circuit_breakers, metrics)
        saga_id = "saga-early"
        ctx = {"items": "[]", "user_id": "u1", "total_cost": "100", "tx_name": "checkout"}

        seq = _ResponseSequencer(outbox_reader, service_dbs, saga_id, {
            "stock": [{"event": "failed", "reason": "insufficient_stock"}],
            "payment": [],
        })

        result = await executor.execute(checkout_tx, saga_id, ctx)

        assert result["status"] == "failed"
        assert "insufficient_stock" in result["error"]
        # Payment should NOT have received an execute (early abort)
        payment_executes = seq.action_calls("execute", "payment")
        assert len(payment_executes) == 0
        wal_steps = [call.args[1] for call in mock_wal.log.call_args_list
                     if call.args[0] == saga_id]
        # No completed steps → COMPENSATING logged but nothing to compensate
        assert wal_steps == ["EXECUTING", "COMPENSATING", "FAILED"]

    @pytest.mark.asyncio
    async def test_saga_second_step_fails(self, service_dbs, outbox_reader, mock_wal,
                                           circuit_breakers, metrics, checkout_tx):
        """Stock executes, payment fails → compensate stock in reverse, FAILED."""
        executor = _make_executor(SagaExecutor, service_dbs, outbox_reader,
                                  mock_wal, circuit_breakers, metrics)
        saga_id = "saga-fail2"
        ctx = {"items": "[]", "user_id": "u1", "total_cost": "100", "tx_name": "checkout"}

        seq = _ResponseSequencer(outbox_reader, service_dbs, saga_id, {
            "stock": [{"event": "executed"}, {"event": "compensated"}],
            "payment": [{"event": "failed", "reason": "insufficient_credit"}],
        })

        result = await executor.execute(checkout_tx, saga_id, ctx)

        assert result["status"] == "failed"
        assert "insufficient_credit" in result["error"]
        # Stock should have been compensated
        assert len(seq.action_calls("compensate", "stock")) == 1
        wal_steps = [call.args[1] for call in mock_wal.log.call_args_list
                     if call.args[0] == saga_id]
        assert wal_steps == ["EXECUTING", "COMPENSATING", "FAILED"]

    @pytest.mark.asyncio
    async def test_saga_no_confirm_phase(self, service_dbs, outbox_reader,
                                           mock_wal, circuit_breakers, metrics,
                                           checkout_tx):
        """Executes succeed → COMPLETED immediately, no confirm/commit phase."""
        executor = _make_executor(SagaExecutor, service_dbs, outbox_reader,
                                  mock_wal, circuit_breakers, metrics)
        saga_id = "saga-no-confirm"
        ctx = {"items": "[]", "user_id": "u1", "total_cost": "100", "tx_name": "checkout"}

        _ResponseSequencer(outbox_reader, service_dbs, saga_id, {
            "stock": [{"event": "executed"}],
            "payment": [{"event": "executed"}],
        })

        result = await executor.execute(checkout_tx, saga_id, ctx)

        assert result["status"] == "success"
        wal_steps = [call.args[1] for call in mock_wal.log.call_args_list
                     if call.args[0] == saga_id]
        # Key assertion: no CONFIRMING state — saga goes straight to COMPLETED
        assert "CONFIRMING" not in wal_steps
        assert wal_steps == ["EXECUTING", "COMPLETED"]
