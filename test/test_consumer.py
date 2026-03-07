"""Tests for common.consumer (consumer_loop, dlq_sweep)."""
from __future__ import annotations

import asyncio
import os
from unittest.mock import AsyncMock

import pytest

from common.consumer import _process_and_ack, consumer_loop, _sweep_dlq

class TestProcessAndAck:

    @pytest.mark.asyncio
    async def test_handler_exception_does_not_ack(self):
        """If handler raises, the message is NOT XACK'd (stays in PEL for DLQ)."""
        db = AsyncMock()
        sem = asyncio.Semaphore(10)
        await sem.acquire()  # pre-acquire to simulate the loop doing it

        handler = AsyncMock(side_effect=RuntimeError("boom"))
        await _process_and_ack(
            "1-0", {"action": "prepare"},
            handler, db, "stock-commands", "stock-workers", sem,
        )
        db.xack.assert_not_awaited()
        # Semaphore should be released even on error
        assert sem._value == 10


class TestConsumerLoop:

    @pytest.mark.asyncio
    async def test_cancelled_error_cancels_pending(self):
        """CancelledError during xreadgroup cancels all in-flight tasks."""
        db = AsyncMock()
        call_count = 0

        async def fake_xreadgroup(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return [("stream", [("1-0", {"action": "test"})])]
            raise asyncio.CancelledError()

        db.xreadgroup = AsyncMock(side_effect=fake_xreadgroup)
        db.xack = AsyncMock()
        handler = AsyncMock()

        with pytest.raises(asyncio.CancelledError):
            await consumer_loop(db, "stream", "group", "consumer", handler, max_inflight=10)

    @pytest.mark.asyncio
    async def test_semaphore_limits_inflight(self):
        """Semaphore caps concurrent in-flight tasks."""
        db = AsyncMock()
        concurrent = 0
        max_concurrent = 0

        async def slow_handler(fields):
            nonlocal concurrent, max_concurrent
            concurrent += 1
            max_concurrent = max(max_concurrent, concurrent)
            await asyncio.sleep(0.05)
            concurrent -= 1

        messages_delivered = 0

        async def fake_xreadgroup(*args, **kwargs):
            nonlocal messages_delivered
            if messages_delivered == 0:
                messages_delivered = 5
                return [("s", [(f"{i}-0", {"i": str(i)}) for i in range(5)])]
            # After delivering, wait a bit then cancel
            await asyncio.sleep(0.2)
            raise asyncio.CancelledError()

        db.xreadgroup = AsyncMock(side_effect=fake_xreadgroup)
        db.xack = AsyncMock()

        with pytest.raises(asyncio.CancelledError):
            await consumer_loop(db, "s", "g", "c", slow_handler, max_inflight=2)

        assert max_concurrent <= 2


class TestDLQSweep:

    @pytest.mark.asyncio
    async def test_dlq_sweep_moves_overretried(self):
        """Messages exceeding max_retries are moved to DLQ."""
        db = AsyncMock()
        db.xpending_range = AsyncMock(return_value=[
            {"message_id": "1-0", "times_delivered": 6},
        ])
        db.xrange = AsyncMock(return_value=[("1-0", {"action": "prepare", "saga_id": "s1"})])
        db.xadd = AsyncMock()
        db.xack = AsyncMock()

        await _sweep_dlq(db, "stock-commands", "stock-workers", "dead-letter:stock", max_retries=5)

        db.xadd.assert_awaited_once()
        dlq_fields = db.xadd.call_args[0][1]
        assert dlq_fields["reason"] == "max_retries_exceeded"
        assert dlq_fields["original_stream"] == "stock-commands"
        db.xack.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_dlq_sweep_boundary_not_moved(self):
        """Messages at exactly max_retries are NOT moved to DLQ (> not >=)."""
        db = AsyncMock()
        db.xpending_range = AsyncMock(return_value=[
            {"message_id": "1-0", "times_delivered": 5},
        ])
        db.xrange = AsyncMock(return_value=[])
        db.xadd = AsyncMock()
        db.xack = AsyncMock()

        await _sweep_dlq(db, "stock-commands", "stock-workers", "dead-letter:stock", max_retries=5)

        db.xadd.assert_not_awaited()
        db.xack.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_dlq_sweep_message_gone_still_acks(self):
        """If the message was deleted from the stream, we still XACK it."""
        db = AsyncMock()
        db.xpending_range = AsyncMock(return_value=[
            {"message_id": "1-0", "times_delivered": 10},
        ])
        # Message no longer in stream
        db.xrange = AsyncMock(return_value=[])
        db.xadd = AsyncMock()
        db.xack = AsyncMock()

        await _sweep_dlq(db, "stock-commands", "stock-workers", "dead-letter:stock", max_retries=5)

        # No xadd (message body not available), but xack still called
        db.xadd.assert_not_awaited()
        db.xack.assert_awaited_once_with("stock-commands", "stock-workers", "1-0")


