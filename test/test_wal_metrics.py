"""Tests for WALEngine, LatencyHistogram, and MetricsCollector."""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, call

import pytest
import pytest_asyncio

from orchestrator.wal import WALEngine
from orchestrator.metrics import LatencyHistogram, MetricsCollector


# ---------------------------------------------------------------------------
# Part A: WALEngine
# ---------------------------------------------------------------------------

def _make_wal_db():
    """Create a mock Redis that supports the pipeline pattern used by WALEngine."""
    db = AsyncMock()

    # Track pipeline operations
    pipe = AsyncMock()
    pipe.xadd = MagicMock()
    pipe.sadd = MagicMock()
    pipe.srem = MagicMock()
    pipe.hset = MagicMock()
    pipe.delete = MagicMock()
    pipe.execute = AsyncMock(return_value=[])

    db.pipeline = MagicMock(return_value=pipe)
    pipe.__aenter__ = AsyncMock(return_value=pipe)
    pipe.__aexit__ = AsyncMock(return_value=False)

    # For get_incomplete_sagas
    db.sscan = AsyncMock(return_value=(0, []))
    db.hgetall = AsyncMock(return_value={})
    db.srem = AsyncMock()
    db.delete = AsyncMock()
    db.sismember = AsyncMock(return_value=False)

    return db, pipe


class TestWALEngine:

    @pytest.mark.asyncio
    async def test_log_without_data(self):
        db, pipe = _make_wal_db()
        wal = WALEngine(db)

        await wal.log("saga-1", "PREPARING")

        # Pipeline should add to stream + active_sagas set + state hash
        pipe.xadd.assert_called_once()
        fields = pipe.xadd.call_args[0][1]
        assert fields["saga_id"] == "saga-1"
        assert fields["step"] == "PREPARING"
        assert "data" not in fields
        pipe.sadd.assert_called_once_with("active_sagas", "saga-1")
        pipe.hset.assert_called_once()

    @pytest.mark.asyncio
    async def test_log_with_data(self):
        db, pipe = _make_wal_db()
        wal = WALEngine(db)

        payload = {"items": [("item1", 2)], "user_id": "u1"}
        await wal.log("saga-2", "STARTED", data=payload)

        fields = pipe.xadd.call_args[0][1]
        assert "data" in fields
        parsed = json.loads(fields["data"])
        assert parsed["user_id"] == "u1"

    @pytest.mark.asyncio
    async def test_log_terminal_removes_from_active(self):
        db, pipe = _make_wal_db()
        wal = WALEngine(db)

        await wal.log("saga-done", "COMPLETED")

        # Terminal states should remove from active set + delete state hash
        pipe.srem.assert_called_once_with("active_sagas", "saga-done")
        pipe.delete.assert_called_once_with("saga_state:saga-done")
        # Should NOT add to active set
        pipe.sadd.assert_not_called()

    @pytest.mark.asyncio
    async def test_log_failed_is_terminal(self):
        db, pipe = _make_wal_db()
        wal = WALEngine(db)

        await wal.log("saga-fail", "FAILED")

        pipe.srem.assert_called_once_with("active_sagas", "saga-fail")
        pipe.delete.assert_called_once_with("saga_state:saga-fail")

    @pytest.mark.asyncio
    async def test_get_incomplete_returns_active_sagas(self):
        db, pipe = _make_wal_db()
        wal = WALEngine(db)

        db.sscan.return_value = (0, ["s4"])
        db.hgetall.return_value = {
            "saga_id": "s4",
            "step": "COMMITTING",
            "timestamp": "1700000000.0",
        }

        result = await wal.get_incomplete_sagas()
        assert "s4" in result
        assert result["s4"]["last_step"] == "COMMITTING"
        assert result["s4"]["created_at"] == 1700000000.0

    @pytest.mark.asyncio
    async def test_get_incomplete_cleans_stale_entries(self):
        db, pipe = _make_wal_db()
        wal = WALEngine(db)

        # Saga in active set but no state hash (stale)
        db.sscan.return_value = (0, ["stale-saga"])
        db.hgetall.return_value = {}

        result = await wal.get_incomplete_sagas()
        assert len(result) == 0
        db.srem.assert_called_with("active_sagas", "stale-saga")

    @pytest.mark.asyncio
    async def test_get_incomplete_cleans_terminal_in_set(self):
        db, pipe = _make_wal_db()
        wal = WALEngine(db)

        # Saga in active set but already terminal (race condition cleanup)
        db.sscan.return_value = (0, ["done-saga"])
        db.hgetall.return_value = {
            "saga_id": "done-saga",
            "step": "COMPLETED",
            "timestamp": "1700000000.0",
        }

        result = await wal.get_incomplete_sagas()
        assert len(result) == 0
        db.srem.assert_called_with("active_sagas", "done-saga")

    @pytest.mark.asyncio
    async def test_get_incomplete_malformed_json(self):
        db, pipe = _make_wal_db()
        wal = WALEngine(db)

        db.sscan.return_value = (0, ["s1"])
        db.hgetall.return_value = {
            "saga_id": "s1",
            "step": "PREPARING",
            "timestamp": "1700000000.0",
            "data": "not{json",
        }

        result = await wal.get_incomplete_sagas()
        assert result["s1"]["data"] == {}


# ---------------------------------------------------------------------------
# Part B: LatencyHistogram
# ---------------------------------------------------------------------------

class TestLatencyHistogram:

    def test_empty_histogram_returns_5000(self):
        h = LatencyHistogram()
        assert h.get_percentile(99) == 5000.0

    def test_observe_correct_bucket(self):
        h = LatencyHistogram()
        h.observe(0.025)  # 25ms -> 25 <= 25 -> 25ms bucket
        assert h._counts[25] == 1
        assert h._counts[50] == 0

    def test_observe_overflow(self):
        h = LatencyHistogram()
        h.observe(15.0)  # 15000ms > 10000ms -> overflow
        assert h._overflow == 1
        assert h.get_percentile(99) == 10000.0

    def test_prometheus_lines_format(self):
        h = LatencyHistogram()
        h.observe(0.025)  # 25ms
        lines = h.prometheus_lines("tx_latency", labels='service="order"')
        assert len(lines) == len(LatencyHistogram.BUCKETS_MS) + 3
        assert any('le="0.025"' in line for line in lines)
        assert any('+Inf' in line for line in lines)
        assert any('_sum' in line for line in lines)
        assert any('_count' in line for line in lines)


# ---------------------------------------------------------------------------
# Part C: MetricsCollector
# ---------------------------------------------------------------------------

class TestMetricsCollector:

    def test_sliding_abort_rate_empty(self):
        m = MetricsCollector()
        assert m.sliding_abort_rate() == 0.0

    def test_sliding_abort_rate_mixed(self):
        m = MetricsCollector()
        for _ in range(7):
            m.record(success=True, protocol="2pc")
        for _ in range(3):
            m.record(success=False, protocol="2pc")
        assert abs(m.sliding_abort_rate() - 0.3) < 1e-9

    def test_sliding_abort_rate_window_eviction(self):
        m = MetricsCollector(window_size=10)
        for _ in range(10):
            m.record(success=False, protocol="2pc")
        assert m.sliding_abort_rate() == 1.0
        for _ in range(10):
            m.record(success=True, protocol="2pc")
        assert m.sliding_abort_rate() == 0.0

    def test_record_zero_duration_skips_histogram(self):
        m = MetricsCollector()
        m.record(success=True, protocol="2pc", duration=0.0)
        assert m.latency_2pc._total == 0

    def test_get_percentile_delegates_by_protocol(self):
        m = MetricsCollector()
        m.record(success=True, protocol="2pc", duration=0.050)
        m.record(success=True, protocol="saga", duration=0.500)
        m.current_protocol = "2pc"
        p99_2pc = m.get_percentile(99)
        m.current_protocol = "saga"
        p99_saga = m.get_percentile(99)
        assert p99_2pc == 50.0
        assert p99_saga == 500.0
