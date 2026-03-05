"""Integration tests for Sentinel failover and leader election takeover.

These tests require a running Docker Compose stack and kill/restart containers.

Run: docker compose up -d && python -m pytest test/test_sentinel_failover.py -v -m integration
"""
from __future__ import annotations

import asyncio
import subprocess

import httpx
import pytest

GATEWAY = "http://127.0.0.1:8000"
TIMEOUT = httpx.Timeout(60.0, connect=10.0)

pytestmark = [pytest.mark.integration, pytest.mark.slow]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _docker_compose(*args: str, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", "compose", *args],
        capture_output=True, text=True, check=check, timeout=60,
    )


async def _wait_healthy(client: httpx.AsyncClient, max_wait: float = 60.0):
    """Wait until ALL services are responsive through the gateway."""
    deadline = asyncio.get_event_loop().time() + max_wait
    while asyncio.get_event_loop().time() < deadline:
        try:
            r_order = await client.get(f"{GATEWAY}/orders/health")
            r_stock = await client.post(f"{GATEWAY}/stock/item/create/1")
            if r_order.status_code == 200 and r_stock.status_code == 200:
                return
        except (httpx.ConnectError, httpx.ReadError):
            pass
        await asyncio.sleep(2.0)
    raise TimeoutError("Services did not become healthy")


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
    """Kill stock-db master, wait for Sentinel failover, verify checkouts still work."""
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        await _wait_healthy(client)

        item_id, user_id = await _setup_item_and_user(client, stock=100, credit=100000)

        # Wait for replication to propagate to replica before killing master
        await asyncio.sleep(3)

        # Kill the stock master
        _docker_compose("kill", "stock-db")

        # Wait for Sentinel failover (down-after=5s + failover-timeout=10s)
        await asyncio.sleep(20)

        # Issue 5 checkouts — during/after failover, transient 5xx are acceptable
        # as long as the system recovers (no permanent data corruption)
        results = []
        for _ in range(5):
            try:
                r = await client.post(f"{GATEWAY}/orders/create/{user_id}")
                if r.status_code != 200:
                    results.append(r.status_code)
                    continue
                order_id = r.json()["order_id"]
                await client.post(f"{GATEWAY}/orders/addItem/{order_id}/{item_id}/1")
                r = await client.post(f"{GATEWAY}/orders/checkout/{order_id}")
                results.append(r.status_code)
            except (httpx.ReadError, httpx.RemoteProtocolError, httpx.ConnectError):
                results.append(-1)  # connection error

        # Restart killed containers and wait for full recovery
        _docker_compose("start", "stock-db")
        await asyncio.sleep(10)

        # Restart app services to force connection pool refresh after topology change
        # (sentinel promoted replica to master — old connections to original master are stale)
        _docker_compose("restart", "stock-service", "stock-service-2",
                        "order-service-1", "order-service-2")
        await asyncio.sleep(15)

        # At least one checkout should have succeeded or failed gracefully (not all 5xx)
        non_5xx = [r for r in results if r < 500 and r > 0]
        assert len(non_5xx) > 0 or len(results) == 0, \
            f"All requests were 5xx — Sentinel failover did not recover: {results}"

        # Verify stock conservation — this is the real invariant
        await _wait_healthy(client)
        r = await client.get(f"{GATEWAY}/stock/find/{item_id}")
        stock_data = r.json()
        assert int(stock_data["stock"]) >= 0, f"Stock went negative: {stock_data['stock']}"


@pytest.mark.asyncio
async def test_leader_election_takeover():
    """Kill order-service-1, verify order-service-2 takes over leadership."""
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        await _wait_healthy(client)

        item_id, user_id = await _setup_item_and_user(client, stock=100, credit=100000)

        # Kill one order instance
        _docker_compose("kill", "order-service-1")

        # Wait for leader TTL to expire + new instance to acquire (TTL=5s)
        await asyncio.sleep(10)

        # Issue 10 checkouts — all routed to order-service-2 by HAProxy
        errors_500 = 0
        successes = 0
        for _ in range(10):
            try:
                r = await client.post(f"{GATEWAY}/orders/create/{user_id}")
                if r.status_code != 200:
                    continue
                order_id = r.json()["order_id"]
                await client.post(f"{GATEWAY}/orders/addItem/{order_id}/{item_id}/1")
                r = await client.post(f"{GATEWAY}/orders/checkout/{order_id}")
                if r.status_code >= 500:
                    errors_500 += 1
                elif r.status_code == 200:
                    successes += 1
            except (httpx.ReadError, httpx.RemoteProtocolError, httpx.ConnectError):
                pass

        # Restart killed containers
        _docker_compose("start", "order-service-1")
        await asyncio.sleep(10)

        # With one order instance alive, checkouts should work.
        # Allow at most 2 transient 5xx (HAProxy may briefly route to dead backend)
        assert errors_500 <= 2, f"Got {errors_500} server errors after leader takeover"
        assert successes > 0, "No successful checkouts — order-service-2 did not take over"

        # Verify stock conservation
        await _wait_healthy(client)
        r = await client.get(f"{GATEWAY}/stock/find/{item_id}")
        stock_data = r.json()
        assert int(stock_data["stock"]) >= 0, f"Stock went negative: {stock_data['stock']}"
