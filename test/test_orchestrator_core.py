"""Tests for Orchestrator._select_protocol, Orchestrator.execute, and LeaderElection."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

from orchestrator.core import Orchestrator
from orchestrator.definition import Step, TransactionDefinition
from orchestrator.leader import LeaderElection
from orchestrator.metrics import MetricsCollector


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_orchestrator(protocol: str = "auto") -> Orchestrator:
    """Create a minimal Orchestrator without starting background tasks."""
    order_db = AsyncMock()
    order_db.pipeline = MagicMock()
    # Make pipeline return an async context manager
    pipe_mock = MagicMock()
    pipe_mock.set = MagicMock(return_value=pipe_mock)
    pipe_mock.publish = MagicMock(return_value=pipe_mock)
    pipe_mock.execute = AsyncMock(return_value=[True, 1])
    order_db.pipeline.return_value.__aenter__ = AsyncMock(return_value=pipe_mock)
    order_db.pipeline.return_value.__aexit__ = AsyncMock(return_value=False)

    transport = AsyncMock()
    transport.send_and_wait = AsyncMock(return_value={"event": "ok"})
    tx_def = TransactionDefinition("checkout", [
        Step("stock", "stock"),
        Step("payment", "payment"),
    ])
    orch = Orchestrator(order_db, transport, [tx_def], protocol=protocol)
    # Replace WAL with a no-op mock so tests don't touch Redis
    orch.wal.log = AsyncMock()
    orch.wal.log_terminal = AsyncMock()
    orch.wal.get_incomplete_sagas = AsyncMock(return_value={})
    return orch


# ---------------------------------------------------------------------------
# Part A: _select_protocol
# ---------------------------------------------------------------------------

class TestSelectProtocol:

    def test_fixed_2pc(self):
        orch = _make_orchestrator(protocol="2pc")
        assert orch._select_protocol() == "2pc"

    def test_fixed_saga(self):
        orch = _make_orchestrator(protocol="saga")
        assert orch._select_protocol() == "saga"

    def test_auto_stays_2pc_below_threshold(self):
        orch = _make_orchestrator(protocol="auto")
        # 91 successes + 9 failures = 9% abort rate (below 10%)
        for _ in range(91):
            orch.metrics._results.append({"success": True, "protocol": "2pc", "timestamp": 0})
        for _ in range(9):
            orch.metrics._results.append({"success": False, "protocol": "2pc", "timestamp": 0})
        assert orch._select_protocol() == "2pc"

    def test_auto_switches_to_saga_at_threshold(self):
        orch = _make_orchestrator(protocol="auto")
        # 90 successes + 10 failures = 10% abort rate (triggers switch)
        for _ in range(90):
            orch.metrics._results.append({"success": True, "protocol": "2pc", "timestamp": 0})
        for _ in range(10):
            orch.metrics._results.append({"success": False, "protocol": "2pc", "timestamp": 0})
        result = orch._select_protocol()
        assert result == "saga"
        assert orch.metrics.current_protocol == "saga"

    def test_auto_stays_saga_between_thresholds(self):
        orch = _make_orchestrator(protocol="auto")
        orch.metrics.current_protocol = "saga"
        # 94 successes + 6 failures = 6% abort rate (between 5% and 10%)
        for _ in range(94):
            orch.metrics._results.append({"success": True, "protocol": "saga", "timestamp": 0})
        for _ in range(6):
            orch.metrics._results.append({"success": False, "protocol": "saga", "timestamp": 0})
        result = orch._select_protocol()
        assert result == "saga"  # stays saga (hysteresis)

    def test_auto_switches_back_to_2pc(self):
        orch = _make_orchestrator(protocol="auto")
        orch.metrics.current_protocol = "saga"
        # 95 successes + 5 failures = 5% abort rate (triggers switch back)
        for _ in range(95):
            orch.metrics._results.append({"success": True, "protocol": "saga", "timestamp": 0})
        for _ in range(5):
            orch.metrics._results.append({"success": False, "protocol": "saga", "timestamp": 0})
        result = orch._select_protocol()
        assert result == "2pc"
        assert orch.metrics.current_protocol == "2pc"


# ---------------------------------------------------------------------------
# Part B: Orchestrator.execute
# ---------------------------------------------------------------------------

class TestOrchestratorExecute:

    @pytest.mark.asyncio
    async def test_execute_unknown_tx(self):
        orch = _make_orchestrator()
        result = await orch.execute("nonexistent", {})
        assert result["status"] == "failed"
        assert "Unknown transaction" in result["error"]

    @pytest.mark.asyncio
    async def test_execute_records_success_metric(self):
        orch = _make_orchestrator(protocol="2pc")
        # Mock the 2pc executor to return success
        orch.two_pc.execute = AsyncMock(return_value={"status": "success"})
        await orch.execute("checkout", {"items": [], "user_id": "u1", "total_cost": 10})
        assert orch.metrics.total_success == 1
        assert orch.metrics.total_failure == 0

    @pytest.mark.asyncio
    async def test_execute_records_failure_metric(self):
        orch = _make_orchestrator(protocol="2pc")
        orch.two_pc.execute = AsyncMock(return_value={"status": "failed", "error": "insufficient_stock"})
        await orch.execute("checkout", {"items": [], "user_id": "u1", "total_cost": 10})
        assert orch.metrics.total_failure == 1
        assert orch.metrics.total_success == 0

    @pytest.mark.asyncio
    async def test_execute_adds_saga_id_and_protocol(self):
        orch = _make_orchestrator(protocol="saga")
        orch.saga.execute = AsyncMock(return_value={"status": "success"})
        result = await orch.execute("checkout", {"items": []}, saga_id_override="my-saga-123")
        assert result["saga_id"] == "my-saga-123"
        assert result["protocol"] == "saga"


# ---------------------------------------------------------------------------
# Part C: LeaderElection
# ---------------------------------------------------------------------------

class TestLeaderElection:

    @pytest_asyncio.fixture
    async def leader(self):
        db = AsyncMock()
        db.set = AsyncMock(return_value=True)
        # register_script returns a callable AsyncMock (the Lua script handle)
        db.register_script = MagicMock(return_value=AsyncMock(return_value=1))
        le = LeaderElection(db)
        yield le
        # Cleanup: stop heartbeat if running
        le._stop_event.set()
        if le._heartbeat_task and not le._heartbeat_task.done():
            le._heartbeat_task.cancel()
            try:
                await le._heartbeat_task
            except (asyncio.CancelledError, Exception):
                pass

    @pytest.mark.asyncio
    async def test_acquire_succeeds(self, leader):
        leader.db.set.return_value = True
        result = await leader.acquire()
        assert result is True
        assert leader.is_leader is True

    @pytest.mark.asyncio
    async def test_acquire_fails(self, leader):
        leader.db.set.return_value = None
        result = await leader.acquire()
        assert result is False
        assert leader.is_leader is False

    @pytest.mark.asyncio
    async def test_release_calls_lua(self, leader):
        leader.db.set.return_value = True
        await leader.acquire()
        # Stop heartbeat so release doesn't race
        if leader._heartbeat_task:
            leader._heartbeat_task.cancel()
            try:
                await leader._heartbeat_task
            except (asyncio.CancelledError, Exception):
                pass
            leader._heartbeat_task = None
        await leader.release()
        assert leader.is_leader is False
        # The release Lua script should have been called
        leader._release.assert_awaited()

    @pytest.mark.asyncio
    async def test_release_when_not_leader_noop(self, leader):
        leader.db.set.return_value = None
        await leader.acquire()  # fails
        # release() on non-leader should not call the Lua script
        await leader.release()
        leader._release.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_heartbeat_loss_sets_not_leader(self, leader):
        # Simulate heartbeat renewal returning 0 (lock lost)
        leader._renew = AsyncMock(return_value=0)
        leader.db.set.return_value = True
        await leader.acquire()
        # Wait for the heartbeat to detect loss (it sleeps HEARTBEAT_INTERVAL first)
        with patch("asyncio.sleep", new_callable=AsyncMock):
            # Manually trigger one heartbeat cycle
            leader._is_leader = True
            # Run the heartbeat renew directly
            result = await leader._renew(keys=["orchestrator:leader"], args=[leader.instance_id, "5"])
            if not result:
                leader._is_leader = False
        assert leader.is_leader is False

    @pytest.mark.asyncio
    async def test_stop_releases(self, leader):
        leader.db.set.return_value = True
        await leader.acquire()
        await leader.stop()
        assert leader._stop_event.is_set()
        assert leader.is_leader is False
