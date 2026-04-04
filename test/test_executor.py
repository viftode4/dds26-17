"""Unit tests for TwoPCExecutor and SagaExecutor.

Architecture:
- 2PC: parallel prepare (lock, no mutations) -> commit (apply) or abort (release locks)
- Saga: sequential execute (direct mutation) -> compensate in reverse on failure
- WAL writes awaited before side effects (PREPARING, EXECUTING, COMMITTING, COMPENSATING)
- Transport.send_and_wait replaces Redis Streams for inter-service communication
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, call

import pytest

from orchestrator.executor import (
    TwoPCExecutor,
    SagaExecutor,
    CircuitBreaker,
    STEP_TIMEOUT,
)
from orchestrator.definition import TransactionDefinition, Step
from orchestrator.wal import WALEngine
from orchestrator.metrics import MetricsCollector


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_executor(cls, mock_transport, mock_wal, circuit_breakers, metrics):
    return cls(
        transport=mock_transport,
        wal=mock_wal,
        circuit_breakers=circuit_breakers,
        metrics=metrics,
    )


def _route_transport(mock_transport, responses: dict[tuple[str, str], dict | list]):
    """Configure mock_transport.send_and_wait to return responses based on (service, action).

    If the value is a list, responses are returned in order (pop from front).
    If the value is a dict, it's returned every time.
    """
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
                # Refill with last response for infinite retry scenarios
                queues[key].append(resp)
            return resp
        return {"event": "ok"}

    mock_transport.send_and_wait = AsyncMock(side_effect=_send_and_wait)


# ---------------------------------------------------------------------------
# 2PC Tests
# ---------------------------------------------------------------------------

class TestTwoPCExecutor:

    @pytest.mark.asyncio
    async def test_2pc_happy_path(self, mock_transport, mock_wal,
                                   circuit_breakers, metrics, checkout_tx):
        """Both services prepare -> commit -> success."""
        _route_transport(mock_transport, {
            ("stock", "prepare"): {"event": "prepared"},
            ("payment", "prepare"): {"event": "prepared"},
            ("stock", "commit"): {"event": "committed"},
            ("payment", "commit"): {"event": "committed"},
        })
        executor = _make_executor(TwoPCExecutor, mock_transport, mock_wal,
                                  circuit_breakers, metrics)
        saga_id = "2pc-happy"
        ctx = {"items": "[]", "user_id": "u1", "total_cost": "100", "tx_name": "checkout"}

        result = await executor.execute(checkout_tx, saga_id, ctx)

        assert result["status"] == "success"
        # WAL progression: PREPARING -> COMMITTING -> COMPLETED
        wal_steps = [c.args[1] for c in mock_wal.log.call_args_list if c.args[0] == saga_id]
        assert wal_steps == ["PREPARING", "COMMITTING"]
        mock_wal.log_terminal.assert_called_with(saga_id, "COMPLETED")
        # Task 1.8: verify prepare and commit were actually sent to BOTH services
        prepare_calls = [
            c for c in mock_transport.send_and_wait.call_args_list
            if c.args[1] == "prepare"
        ]
        commit_calls = [
            c for c in mock_transport.send_and_wait.call_args_list
            if c.args[1] == "commit"
        ]
        assert len(prepare_calls) == 2, f"Expected 2 prepare calls, got {len(prepare_calls)}"
        assert len(commit_calls) == 2, f"Expected 2 commit calls, got {len(commit_calls)}"
        prepare_services = {c.args[0] for c in prepare_calls}
        assert prepare_services == {"stock", "payment"}, (
            f"Prepare not sent to both services: {prepare_services}"
        )

    @pytest.mark.asyncio
    async def test_2pc_prepare_fails(self, mock_transport, mock_wal,
                                      circuit_breakers, metrics, checkout_tx):
        """Stock returns 'failed' during prepare -> abort broadcast, FAILED."""
        _route_transport(mock_transport, {
            ("stock", "prepare"): {"event": "failed", "reason": "insufficient_stock"},
            ("payment", "prepare"): {"event": "prepared"},
            ("stock", "abort"): {"event": "aborted"},
            ("payment", "abort"): {"event": "aborted"},
        })
        executor = _make_executor(TwoPCExecutor, mock_transport, mock_wal,
                                  circuit_breakers, metrics)
        saga_id = "2pc-fail"
        ctx = {"items": "[]", "user_id": "u1", "total_cost": "100", "tx_name": "checkout"}

        result = await executor.execute(checkout_tx, saga_id, ctx)

        assert result["status"] == "failed"
        assert "insufficient_stock" in result.get("error", "")
        wal_steps = [c.args[1] for c in mock_wal.log.call_args_list if c.args[0] == saga_id]
        assert wal_steps == ["PREPARING", "ABORTING"]
        mock_wal.log_terminal.assert_called_with(saga_id, "FAILED")

    @pytest.mark.asyncio
    async def test_2pc_verified_commits(self, mock_transport, mock_wal,
                                          circuit_breakers, metrics, checkout_tx):
        """Prepares succeed -> commits sent and verified -> success."""
        _route_transport(mock_transport, {
            ("stock", "prepare"): {"event": "prepared"},
            ("payment", "prepare"): {"event": "prepared"},
            ("stock", "commit"): {"event": "committed"},
            ("payment", "commit"): {"event": "committed"},
        })
        executor = _make_executor(TwoPCExecutor, mock_transport, mock_wal,
                                  circuit_breakers, metrics)
        saga_id = "2pc-verified"
        ctx = {"items": "[]", "user_id": "u1", "total_cost": "100", "tx_name": "checkout"}

        result = await executor.execute(checkout_tx, saga_id, ctx)

        assert result["status"] == "success"
        # Verify commit calls were made to both services
        commit_calls = [
            c for c in mock_transport.send_and_wait.call_args_list
            if c.args[1] == "commit"
        ]
        assert len(commit_calls) == 2
        wal_steps = [c.args[1] for c in mock_wal.log.call_args_list if c.args[0] == saga_id]
        assert wal_steps == ["PREPARING", "COMMITTING"]
        mock_wal.log_terminal.assert_called_with(saga_id, "COMPLETED")

    @pytest.mark.asyncio
    async def test_2pc_commit_wal_keeps_tx_name_when_context_omits_it(
        self, mock_transport, mock_wal, circuit_breakers, metrics, checkout_tx
    ):
        """Recovery metadata must keep tx_name even if runtime context doesn't."""
        _route_transport(mock_transport, {
            ("stock", "prepare"): {"event": "prepared"},
            ("payment", "prepare"): {"event": "prepared"},
            ("stock", "commit"): {"event": "committed"},
            ("payment", "commit"): {"event": "committed"},
        })
        executor = _make_executor(TwoPCExecutor, mock_transport, mock_wal,
                                  circuit_breakers, metrics)

        result = await executor.execute(
            checkout_tx,
            "2pc-commit-txname",
            {"items": "[]", "user_id": "u1", "total_cost": "100"},
            tx_name="checkout",
        )

        assert result["status"] == "success"
        commit_call = next(
            c for c in mock_wal.log.call_args_list
            if c.args[0] == "2pc-commit-txname" and c.args[1] == "COMMITTING"
        )
        assert commit_call.args[2]["tx_name"] == "checkout"

    @pytest.mark.asyncio
    async def test_2pc_abort_wal_keeps_tx_name_when_context_omits_it(
        self, mock_transport, mock_wal, circuit_breakers, metrics, checkout_tx
    ):
        """Abort recovery also needs tx_name in the WAL payload."""
        _route_transport(mock_transport, {
            ("stock", "prepare"): {"event": "failed", "reason": "insufficient_stock"},
            ("payment", "prepare"): {"event": "prepared"},
            ("stock", "abort"): {"event": "aborted"},
            ("payment", "abort"): {"event": "aborted"},
        })
        executor = _make_executor(TwoPCExecutor, mock_transport, mock_wal,
                                  circuit_breakers, metrics)

        result = await executor.execute(
            checkout_tx,
            "2pc-abort-txname",
            {"items": "[]", "user_id": "u1", "total_cost": "100"},
            tx_name="checkout",
        )

        assert result["status"] == "failed"
        abort_call = next(
            c for c in mock_wal.log.call_args_list
            if c.args[0] == "2pc-abort-txname" and c.args[1] == "ABORTING"
        )
        assert abort_call.args[2]["tx_name"] == "checkout"

    @pytest.mark.asyncio
    async def test_circuit_breaker_fast_fail(self, mock_transport, mock_wal,
                                              circuit_breakers, metrics, checkout_tx):
        """Circuit breaker open -> WAL FAILED immediately, no commands sent."""
        executor = _make_executor(TwoPCExecutor, mock_transport, mock_wal,
                                  circuit_breakers, metrics)
        saga_id = "2pc-cb"
        ctx = {"items": "[]", "user_id": "u1", "total_cost": "100", "tx_name": "checkout"}

        # Trip the circuit breaker for stock
        for _ in range(10):
            circuit_breakers["stock"].record_failure()
        assert circuit_breakers["stock"].is_open()

        result = await executor.execute(checkout_tx, saga_id, ctx)

        assert result["status"] == "failed"
        assert "unavailable" in result["error"]
        mock_transport.send_and_wait.assert_not_called()
        mock_wal.log_terminal.assert_called_with(saga_id, "FAILED")


# ---------------------------------------------------------------------------
# Saga Tests
# ---------------------------------------------------------------------------

class TestSagaExecutor:

    @pytest.mark.asyncio
    async def test_saga_happy_path(self, mock_transport, mock_wal,
                                    circuit_breakers, metrics, checkout_tx):
        """Sequential execute: stock then payment -> COMPLETED (no confirm phase)."""
        _route_transport(mock_transport, {
            ("stock", "execute"): {"event": "executed"},
            ("payment", "execute"): {"event": "executed"},
        })
        executor = _make_executor(SagaExecutor, mock_transport, mock_wal,
                                  circuit_breakers, metrics)
        saga_id = "saga-happy"
        ctx = {"items": "[]", "user_id": "u1", "total_cost": "100", "tx_name": "checkout"}

        result = await executor.execute(checkout_tx, saga_id, ctx)

        assert result["status"] == "success"
        # WAL: EXECUTING -> COMPLETED (no confirm phase!)
        wal_steps = [c.args[1] for c in mock_wal.log.call_args_list if c.args[0] == saga_id]
        assert wal_steps == ["EXECUTING"]
        mock_wal.log_terminal.assert_called_with(saga_id, "COMPLETED")
        # No confirm calls
        confirm_calls = [
            c for c in mock_transport.send_and_wait.call_args_list
            if c.args[1] in ("commit", "confirm")
        ]
        assert len(confirm_calls) == 0

    @pytest.mark.asyncio
    async def test_saga_early_abort_on_first_failure(self, mock_transport, mock_wal,
                                                      circuit_breakers, metrics,
                                                      checkout_tx):
        """Stock fails -> payment never tried (early abort), no compensation needed, FAILED."""
        _route_transport(mock_transport, {
            ("stock", "execute"): {"event": "failed", "reason": "insufficient_stock"},
        })
        executor = _make_executor(SagaExecutor, mock_transport, mock_wal,
                                  circuit_breakers, metrics)
        saga_id = "saga-early"
        ctx = {"items": "[]", "user_id": "u1", "total_cost": "100", "tx_name": "checkout"}

        result = await executor.execute(checkout_tx, saga_id, ctx)

        assert result["status"] == "failed"
        assert "insufficient_stock" in result["error"]
        # Payment should NOT have received an execute (early abort)
        payment_executes = [
            c for c in mock_transport.send_and_wait.call_args_list
            if c.args[0] == "payment" and c.args[1] == "execute"
        ]
        assert len(payment_executes) == 0
        wal_steps = [c.args[1] for c in mock_wal.log.call_args_list if c.args[0] == saga_id]
        # No completed steps -> COMPENSATING logged but nothing to compensate
        assert wal_steps == ["EXECUTING", "COMPENSATING"]
        mock_wal.log_terminal.assert_called_with(saga_id, "FAILED")

    @pytest.mark.asyncio
    async def test_saga_second_step_fails(self, mock_transport, mock_wal,
                                           circuit_breakers, metrics, checkout_tx):
        """Stock executes, payment fails -> compensate stock in reverse, FAILED."""
        _route_transport(mock_transport, {
            ("stock", "execute"): {"event": "executed"},
            ("payment", "execute"): {"event": "failed", "reason": "insufficient_credit"},
            ("stock", "compensate"): {"event": "compensated"},
        })
        executor = _make_executor(SagaExecutor, mock_transport, mock_wal,
                                  circuit_breakers, metrics)
        saga_id = "saga-fail2"
        ctx = {"items": "[]", "user_id": "u1", "total_cost": "100", "tx_name": "checkout"}

        result = await executor.execute(checkout_tx, saga_id, ctx)

        assert result["status"] == "failed"
        assert "insufficient_credit" in result["error"]
        # Stock should have been compensated
        compensate_calls = [
            c for c in mock_transport.send_and_wait.call_args_list
            if c.args[0] == "stock" and c.args[1] == "compensate"
        ]
        assert len(compensate_calls) >= 1
        # Task 1.10: payment must NOT have been asked to compensate (it never executed)
        payment_compensate_calls = [
            c for c in mock_transport.send_and_wait.call_args_list
            if c.args[0] == "payment" and c.args[1] == "compensate"
        ]
        assert len(payment_compensate_calls) == 0, (
            "Payment should NOT be compensated — it never executed"
        )
        wal_steps = [c.args[1] for c in mock_wal.log.call_args_list if c.args[0] == saga_id]
        assert wal_steps == ["EXECUTING", "COMPENSATING"]
        mock_wal.log_terminal.assert_called_with(saga_id, "FAILED")

    @pytest.mark.asyncio
    async def test_saga_no_confirm_phase(self, mock_transport, mock_wal,
                                           circuit_breakers, metrics, checkout_tx):
        """Executes succeed -> COMPLETED immediately, no confirm/commit phase."""
        _route_transport(mock_transport, {
            ("stock", "execute"): {"event": "executed"},
            ("payment", "execute"): {"event": "executed"},
        })
        executor = _make_executor(SagaExecutor, mock_transport, mock_wal,
                                  circuit_breakers, metrics)
        saga_id = "saga-no-confirm"
        ctx = {"items": "[]", "user_id": "u1", "total_cost": "100", "tx_name": "checkout"}

        result = await executor.execute(checkout_tx, saga_id, ctx)

        assert result["status"] == "success"
        wal_steps = [c.args[1] for c in mock_wal.log.call_args_list if c.args[0] == saga_id]
        # Key assertion: no CONFIRMING state — saga goes straight to COMPLETED
        assert "CONFIRMING" not in wal_steps
        assert wal_steps == ["EXECUTING"]
        mock_wal.log_terminal.assert_called_with(saga_id, "COMPLETED")
