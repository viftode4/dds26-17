"""Integration tests for Redis Cluster failover and HAProxy failover.

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
async def test_cluster_failover_stock_master():
    """Kill stock-cluster-1 master, wait for cluster failover, verify checkouts still work.

    Redis Cluster automatically promotes the replica for the affected shard
    once cluster-node-timeout (5s) + election time (~5s) elapses. The
    RedisCluster client discovers the new topology via MOVED/ASK responses
    without requiring service restarts.
    """
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        await wait_gateway_healthy(client)
        # Extra settle: allow any recovery worker activity from prior tests to
        # finish before we create the user whose credit we'll audit.
        await asyncio.sleep(10)

        item_id, user_id = await _setup_item_and_user(client, stock=100, credit=100000)
        initial_credit = 100000

        # Brief pause to allow replication to the replica before killing the master.
        await asyncio.sleep(2.0)

        # Kill one stock cluster master
        _docker_compose("kill", "stock-cluster-1")

        # Wait for cluster election (cluster-node-timeout=5s + election overhead)
        await asyncio.sleep(15)

        # Restart stock-cluster-1 so the promoted master gets a replica.
        # With min-replicas-to-write=1 configured on original masters, the
        # promoted replica (started without that setting) accepts writes immediately.
        _docker_compose("start", "stock-cluster-1")
        await asyncio.sleep(5)

        # Wait for stock service to be reachable through HAProxy after failover.
        async def _stock_healthy():
            try:
                r = await client.get(f"{GATEWAY}/stock/health")
                return r.status_code == 200
            except Exception:
                return False

        await wait_until(_stock_healthy, timeout=45.0, interval=2.0,
                         msg="stock service not healthy through gateway after failover")

        # Issue 5 checkouts — after failover recovery
        tracker = OutcomeTracker()
        for _ in range(5):
            order_id = await create_order_with_item(client, user_id, item_id, 1)
            try:
                r = await client.post(f"{GATEWAY}/orders/checkout/{order_id}")
                tracker.record_response(r)
            except (httpx.ReadError, httpx.RemoteProtocolError, httpx.ConnectError) as e:
                tracker.record_exception(e)

        await wait_gateway_healthy(client)

        # At least one checkout should have succeeded or failed gracefully (not all 5xx)
        assert tracker.successes + tracker.client_errors > 0, (
            f"All requests were server/connection errors — cluster failover did not recover.\n"
            f"Outcomes: {tracker.summary()}\n"
            f"Details: {tracker.outcomes}"
        )

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

    Tests HAProxy leastconn failover — checkouts don't depend on leader
    election (active-active architecture).
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

        # HAProxy uses `check inter 5s` with default `fall 3` — needs at least
        # 3 × 5s = 15s to mark the dead backend down. Wait 20s to be safe,
        # then poll until order-service-2 is actually accepting requests.
        await asyncio.sleep(20)

        async def _order_service_ready() -> bool:
            try:
                r = await client.get(f"{GATEWAY}/orders/health")
                return r.status_code == 200
            except Exception:
                return False

        await wait_until(_order_service_ready, timeout=30.0, interval=1.0,
                         msg="order-service-2 not healthy after 30s")

        # Issue 10 checkouts using OutcomeTracker — no silent swallowing.
        tracker = OutcomeTracker()
        for _ in range(10):
            order_id = None
            for _attempt in range(3):
                try:
                    r = await client.post(f"{GATEWAY}/orders/create/{user_id}")
                    if r.status_code == 200:
                        order_id = r.json()["order_id"]
                        break
                    tracker.record_response(r)
                    break
                except (httpx.ReadError, httpx.RemoteProtocolError, httpx.ConnectError):
                    await asyncio.sleep(1.0)

            if order_id is None:
                tracker.record_exception(Exception("orders/create failed after retries"))
                continue

            r_add = await client.post(f"{GATEWAY}/orders/addItem/{order_id}/{item_id}/1")
            if r_add.status_code != 200:
                tracker.record_response(r_add)
                continue

            try:
                r = await client.post(f"{GATEWAY}/orders/checkout/{order_id}")
                tracker.record_response(r)
            except (httpx.ReadError, httpx.RemoteProtocolError, httpx.ConnectError) as e:
                tracker.record_exception(e)

        # Restart killed container
        _docker_compose("start", "order-service-1")
        await asyncio.sleep(10)

        await wait_gateway_healthy(client)

        # With one order instance alive and HAProxy fully failed over, all
        # checkouts should succeed or fail with clean business-logic errors.
        assert tracker.server_errors <= 1, (
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
