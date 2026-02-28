"""Tests for OutboxReader — future creation, resolution, and cancellation."""
from __future__ import annotations

import asyncio

import pytest

from orchestrator.executor import OutboxReader


@pytest.mark.asyncio
async def test_create_and_resolve():
    """Create a waiter, resolve it, and verify the future has the correct value."""
    reader = OutboxReader()
    fut = reader.create_waiter("saga-1", "stock")
    assert not fut.done()

    reader.resolve("saga-1", "stock", {"event": "reserved"})
    result = await asyncio.wait_for(fut, timeout=1.0)
    assert result == {"event": "reserved"}


@pytest.mark.asyncio
async def test_resolve_unknown_noop():
    """Resolving a non-existent key should not raise."""
    reader = OutboxReader()
    # Should silently do nothing
    reader.resolve("nonexistent", "stock", {"event": "reserved"})


@pytest.mark.asyncio
async def test_cancel_all():
    """cancel_all should cancel all pending futures for a given saga."""
    reader = OutboxReader()
    fut_stock = reader.create_waiter("saga-2", "stock")
    fut_payment = reader.create_waiter("saga-2", "payment")
    # A different saga's future should NOT be cancelled
    fut_other = reader.create_waiter("saga-3", "stock")

    reader.cancel_all("saga-2")

    assert fut_stock.cancelled()
    assert fut_payment.cancelled()
    assert not fut_other.done()
