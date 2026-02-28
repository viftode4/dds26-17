"""Tests for WALEngine, LatencyHistogram, and MetricsCollector."""
from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from orchestrator.wal import WALEngine
from orchestrator.metrics import LatencyHistogram, MetricsCollector


# ---------------------------------------------------------------------------
# Part A: WALEngine
# ---------------------------------------------------------------------------

class TestWALEngine:

    @pytest_asyncio.fixture
    async def wal(self):
        db = AsyncMock()
        db.xadd = AsyncMock(return_value="1-0")
        db.xrange = AsyncMock(return_value=[])
        return WALEngine(db)

    @pytest.mark.asyncio
    async def test_log_without_data(self, wal):
        await wal.log("saga-1", "PREPARING")
        wal.db.xadd.assert_awaited_once()
        fields = wal.db.xadd.call_args[0][1]
        assert fields["saga_id"] == "saga-1"
        assert fields["step"] == "PREPARING"
        assert "data" not in fields

    @pytest.mark.asyncio
    async def test_log_with_data(self, wal):
        payload = {"items": [("item1", 2)], "user_id": "u1"}
        await wal.log("saga-2", "STARTED", data=payload)
        fields = wal.db.xadd.call_args[0][1]
        assert "data" in fields
        parsed = json.loads(fields["data"])
        assert parsed["user_id"] == "u1"

    @pytest.mark.asyncio
    async def test_get_incomplete_filters_terminal(self, wal):
        wal.db.xrange.return_value = [
            ("1-0", {"saga_id": "s1", "step": "PREPARING"}),
            ("2-0", {"saga_id": "s1", "step": "COMPLETED"}),
            ("3-0", {"saga_id": "s2", "step": "FAILED"}),
            ("4-0", {"saga_id": "s3", "step": "ABANDONED"}),
            ("5-0", {"saga_id": "s4", "step": "COMMITTING"}),
        ]
        result = await wal.get_incomplete_sagas()
        assert "s1" not in result  # reached COMPLETED
        assert "s2" not in result  # reached FAILED
        assert "s3" not in result  # reached ABANDONED
        assert "s4" in result
        assert result["s4"]["last_step"] == "COMMITTING"

    @pytest.mark.asyncio
    async def test_get_incomplete_last_entry_wins(self, wal):
        wal.db.xrange.return_value = [
            ("1-0", {"saga_id": "s1", "step": "STARTED"}),
            ("2-0", {"saga_id": "s1", "step": "PREPARING"}),
            ("3-0", {"saga_id": "s1", "step": "COMMITTING"}),
        ]
        result = await wal.get_incomplete_sagas()
        assert result["s1"]["last_step"] == "COMMITTING"

    @pytest.mark.asyncio
    async def test_get_incomplete_malformed_json(self, wal):
        wal.db.xrange.return_value = [
            ("1-0", {"saga_id": "s1", "step": "PREPARING", "data": "not{json"}),
        ]
        result = await wal.get_incomplete_sagas()
        assert result["s1"]["data"] == {}  # malformed JSON → empty dict


# ---------------------------------------------------------------------------
# Part B: LatencyHistogram
# ---------------------------------------------------------------------------

class TestLatencyHistogram:

    def test_empty_histogram_returns_5000(self):
        h = LatencyHistogram()
        assert h.get_percentile(99) == 5000.0

    def test_observe_correct_bucket(self):
        h = LatencyHistogram()
        h.observe(0.025)  # 25ms → 25 <= 25 → 25ms bucket
        assert h._counts[25] == 1
        assert h._counts[50] == 0

    def test_observe_overflow(self):
        h = LatencyHistogram()
        h.observe(15.0)  # 15000ms > 10000ms → overflow
        assert h._overflow == 1
        # get_percentile should return the last bucket boundary
        assert h.get_percentile(99) == 10000.0

    def test_prometheus_lines_format(self):
        h = LatencyHistogram()
        h.observe(0.025)  # 25ms
        lines = h.prometheus_lines("tx_latency", labels='service="order"')
        # Should have len(BUCKETS_MS) + 3 lines (+Inf, _sum, _count)
        assert len(lines) == len(LatencyHistogram.BUCKETS_MS) + 3
        # Check a bucket line format
        assert any('le="0.025"' in line for line in lines)
        # Check +Inf line
        assert any('+Inf' in line for line in lines)
        # Check _sum and _count lines
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
        # Fill with 10 failures → 100% abort rate
        for _ in range(10):
            m.record(success=False, protocol="2pc")
        assert m.sliding_abort_rate() == 1.0
        # Add 10 successes → evicts all failures
        for _ in range(10):
            m.record(success=True, protocol="2pc")
        assert m.sliding_abort_rate() == 0.0

    def test_record_zero_duration_skips_histogram(self):
        m = MetricsCollector()
        m.record(success=True, protocol="2pc", duration=0.0)
        assert m.latency_2pc._total == 0

    def test_get_percentile_delegates_by_protocol(self):
        m = MetricsCollector()
        # Record some 2pc latency
        m.record(success=True, protocol="2pc", duration=0.050)
        # Record some saga latency
        m.record(success=True, protocol="saga", duration=0.500)
        # Default protocol is 2pc
        m.current_protocol = "2pc"
        p99_2pc = m.get_percentile(99)
        m.current_protocol = "saga"
        p99_saga = m.get_percentile(99)
        # 50ms observation → 50ms bucket; 500ms observation → 500ms bucket
        assert p99_2pc == 50.0
        assert p99_saga == 500.0
