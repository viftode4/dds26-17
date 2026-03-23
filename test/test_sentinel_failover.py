"""Integration tests for Sentinel failover and HAProxy failover.

These tests require a running Docker Compose stack and kill/restart containers.

Run: docker compose up -d && python -m pytest test/test_sentinel_failover.py -v -m integration
"""
from __future__ import annotations

import asyncio
import subprocess

import httpx
import pytest

from helpers import (
    GATEWAY, TIMEOUT,
    OutcomeTracker,
    wait_until,
    create_item,
    create_user,
    create_order_with_item,
    wait_gateway_healthy,
)

pytestmark = [pytest.mark.integration, pytest.mark.slow]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _docker_compose(*args: str, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", "compose", *args],
        capture_output=True, text=True, check=check, timeout=60,
    )


async def _setup_item_and_user(client: httpx.AsyncClient,
                                stock: int = 100, credit: int = 100000):
    """Create a test item and user, returning (item_id, user_id). Retries on 503."""
    # Retry loop for transient 503s after container restarts
    for attempt in range(5):
        r = await client.post(f"{GATEWAY}/stock/item/create/10")
        if r.status_code == 200:
            break
        await asyncio.sleep(2.0)
    assert r.status_code == 200, f"stock/item/create failed: {r.status_code} {r.text}"
    item_id = r.json()["item_id"]
    await client.post(f"{GATEWAY}/stock/add/{item_id}/{stock}")

    for attempt in range(5):
        r = await client.post(f"{GATEWAY}/payment/create_user")
        if r.status_code == 200:
            break
        await asyncio.sleep(2.0)
    assert r.status_code == 200, f"payment/create_user failed: {r.status_code} {r.text}"
    user_id = r.json()["user_id"]
    await client.post(f"{GATEWAY}/payment/add_funds/{user_id}/{credit}")

    return item_id, user_id


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_sentinel_failover_stock_master():
    """Kill stock-db master, wait for Sentinel failover, verify checkouts still work.

    Fixes (task 5.1 + task 1.6):
    - Use WAIT-based replication confirmation via a setup delay + health check
      (direct WAIT command would require raw Redis access from test; instead we
       sleep slightly longer and verify item is readable on replica before killing)
    - Add r.status_code == 200 guard before accessing stock_data["stock"]
    - Use OutcomeTracker to capture all request outcomes for diagnostics
    """
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        await wait_gateway_healthy(client)

        item_id, user_id = await _setup_item_and_user(client, stock=100, credit=100000)
        initial_credit = 100000

        # Verify item is visible on replica before killing master
        # (give replication a generous window and confirm reads work)
        async def _item_readable() -> bool:
            try:
                r = await client.get(f"{GATEWAY}/stock/find/{item_id}")
                return r.status_code == 200 and "stock" in r.json()
            except Exception:
                return False

        await wait_until(_item_readable, timeout=10.0, interval=0.5,
                         msg="Item not replicated to replica within 10s")

        # Kill the stock master
        _docker_compose("kill", "stock-db")

        # Wait for Sentinel failover (down-after=5s + failover-timeout=10s)
        await asyncio.sleep(20)

        # Issue 5 checkouts — during/after failover, transient 5xx are acceptable
        tracker = OutcomeTracker()
        for _ in range(5):
            order_id = await create_order_with_item(client, user_id, item_id, 1)
            try:
                r = await client.post(f"{GATEWAY}/orders/checkout/{order_id}")
                tracker.record_response(r)
            except (httpx.ReadError, httpx.RemoteProtocolError, httpx.ConnectError) as e:
                tracker.record_exception(e)

        # Restart killed containers and wait for full recovery
        _docker_compose("start", "stock-db")
        await asyncio.sleep(10)

        # Restart app services to force connection pool refresh after topology change
        _docker_compose("restart", "stock-service", "stock-service-2",
                        "order-service-1", "order-service-2")
        await asyncio.sleep(15)

        await wait_gateway_healthy(client)

        # At least one checkout should have succeeded or failed gracefully (not all 5xx)
        assert tracker.successes + tracker.client_errors > 0, (
            f"All requests were server/connection errors — Sentinel failover did not recover.\n"
            f"Outcomes: {tracker.summary()}\n"
            f"Details: {tracker.outcomes}"
        )

        # Task 1.6: guard stock access with status check
        r = await client.get(f"{GATEWAY}/stock/find/{item_id}")
        assert r.status_code == 200, (
            f"stock/find returned {r.status_code} after failover: {r.text}"
        )
        stock_data = r.json()
        assert "stock" in stock_data, f"Unexpected response format: {stock_data}"
        assert int(stock_data["stock"]) >= 0, f"Stock went negative: {stock_data['stock']}"

        # Conservation check: credit_spent == stock_sold × price
        r = await client.get(f"{GATEWAY}/payment/find_user/{user_id}")
        assert r.status_code == 200, f"find_user failed after failover: {r.text}"
        final_credit = r.json()["credit"]
        stock_sold = 100 - int(stock_data["stock"])
        credit_spent = initial_credit - final_credit
        assert credit_spent == stock_sold * 10, (
            f"Conservation violated after failover: "
            f"credit_spent={credit_spent} != stock_sold({stock_sold}) × 10\n"
            f"Request outcomes: {tracker.summary()}"
        )


@pytest.mark.asyncio
async def test_haproxy_failover_order_service():
    """Kill order-service-1, verify order-service-2 takes over via HAProxy.

    Renamed from test_leader_election_takeover — checkouts don't depend on
    leader election (active-active architecture). This tests HAProxy failover.

    Fixes (task 5.2 + task 1.7):
    - Use OutcomeTracker instead of silent pass/continue (3 silent paths fixed)
    - Wait for HAProxy to detect the dead backend before issuing checkouts
    - Retry-with-backoff logic for the transition window
    - Meaningful diagnostic output on failure
    """
    initial_stock = 100
    initial_credit = 100000
    item_price = 10

    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        await wait_gateway_healthy(client)

        item_id, user_id = await _setup_item_and_user(
            client, stock=initial_stock, credit=initial_credit,
        )

        # Kill one order instance
        _docker_compose("kill", "order-service-1")

        # Wait for HAProxy to mark the dead backend down (check interval ~2s, 3 probes)
        # and for order-service-2 to be the sole recipient of traffic
        await asyncio.sleep(10)

        # Issue 10 checkouts using OutcomeTracker — no silent swallowing
        tracker = OutcomeTracker()
        for _ in range(10):
            try:
                r = await client.post(f"{GATEWAY}/orders/create/{user_id}")
                if r.status_code != 200:
                    tracker.record_response(r)
                    continue
                order_id = r.json()["order_id"]
                await client.post(f"{GATEWAY}/orders/addItem/{order_id}/{item_id}/1")
                r = await client.post(f"{GATEWAY}/orders/checkout/{order_id}")
                tracker.record_response(r)
            except (httpx.ReadError, httpx.RemoteProtocolError, httpx.ConnectError) as e:
                tracker.record_exception(e)

        # Restart killed container
        _docker_compose("start", "order-service-1")
        await asyncio.sleep(10)

        await wait_gateway_healthy(client)

        # With one order instance alive, most checkouts should work.
        # Allow at most 2 transient server errors (HAProxy may briefly route to dead backend).
        assert tracker.server_errors <= 2, (
            f"Got {tracker.server_errors} server errors after HAProxy failover.\n"
            f"Summary: {tracker.summary()}\n"
            f"Details: {tracker.outcomes}"
        )
        assert tracker.successes > 0, (
            f"No successful checkouts — order-service-2 did not handle traffic.\n"
            f"Summary: {tracker.summary()}\n"
            f"Details: {tracker.outcomes}"
        )

        # Conservation check
        r = await client.get(f"{GATEWAY}/stock/find/{item_id}")
        assert r.status_code == 200
        final_stock = r.json()["stock"]
        assert int(final_stock) >= 0, f"Stock went negative: {final_stock}"

        r = await client.get(f"{GATEWAY}/payment/find_user/{user_id}")
        assert r.status_code == 200
        final_credit = r.json()["credit"]
        stock_sold = initial_stock - int(final_stock)
        credit_spent = initial_credit - final_credit
        assert credit_spent == stock_sold * item_price, (
            f"Conservation violated after HAProxy failover: "
            f"credit_spent={credit_spent} != stock_sold({stock_sold}) × {item_price}"
        )
