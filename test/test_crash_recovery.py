"""Integration crash-recovery tests — requires Docker Compose running.

These tests kill containers mid-transaction and verify consistency is maintained.

Run: docker compose up -d && python -m pytest test/test_crash_recovery.py -v -m integration
"""
from __future__ import annotations

import asyncio
import subprocess

import httpx
import pytest

GATEWAY = "http://127.0.0.1:8000"
TIMEOUT = httpx.Timeout(60.0, connect=10.0)

pytestmark = pytest.mark.integration


def _docker_compose(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    """Run a docker compose command."""
    return subprocess.run(
        ["docker", "compose", *args],
        capture_output=True, text=True, check=check, timeout=60,
    )


def _kill_container(name: str):
    """Force-kill a running container."""
    _docker_compose("kill", name, check=False)


def _restart_container(name: str):
    """Restart a killed container."""
    _docker_compose("start", name, check=False)


async def _wait_healthy(client: httpx.AsyncClient, max_wait: float = 30.0):
    """Wait until the gateway is responsive."""
    deadline = asyncio.get_event_loop().time() + max_wait
    while asyncio.get_event_loop().time() < deadline:
        try:
            r = await client.get(f"{GATEWAY}/orders/health")
            if r.status_code == 200:
                return
        except httpx.ConnectError:
            pass
        await asyncio.sleep(1.0)
    raise TimeoutError("Gateway did not become healthy")


@pytest.mark.asyncio
async def test_order_crash_mid_checkout():
    """Kill order-service-1 during checkout, wait for recovery → consistency maintained."""
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        await _wait_healthy(client)

        # Setup: create item + user
        item_price = 100
        r = await client.post(f"{GATEWAY}/stock/item/create/{item_price}")
        item_id = r.json()["item_id"]
        initial_stock = 50
        await client.post(f"{GATEWAY}/stock/add/{item_id}/{initial_stock}")

        r = await client.post(f"{GATEWAY}/payment/create_user")
        user_id = r.json()["user_id"]
        await client.post(f"{GATEWAY}/payment/add_funds/{user_id}/100000")

        # Fire several checkouts concurrently
        async def _checkout():
            r = await client.post(f"{GATEWAY}/orders/create/{user_id}")
            oid = r.json()["order_id"]
            await client.post(f"{GATEWAY}/orders/addItem/{oid}/{item_id}/1")
            try:
                return await client.post(f"{GATEWAY}/orders/checkout/{oid}")
            except (httpx.ReadError, httpx.RemoteProtocolError, httpx.ConnectError):
                return None  # Expected — container died

        # Start 10 checkouts, kill order-service-1 after a brief delay
        tasks = [asyncio.create_task(_checkout()) for _ in range(10)]
        await asyncio.sleep(0.3)
        _kill_container("order-service-1")

        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Wait for recovery (order-service-2 takes over leadership)
        await asyncio.sleep(10.0)
        _restart_container("order-service-1")
        await asyncio.sleep(5.0)
        await _wait_healthy(client)

        # Consistency: stock >= 0, stock <= initial (no leaks or negative values)
        r = await client.get(f"{GATEWAY}/stock/find/{item_id}")
        stock_data = r.json()
        assert stock_data["stock"] >= 0, \
            f"Negative stock after crash recovery: {stock_data['stock']}"
        assert stock_data["stock"] <= initial_stock, \
            f"Stock exceeded initial after crash: {stock_data['stock']} > {initial_stock}"


@pytest.mark.asyncio
async def test_stock_crash_mid_reserve():
    """Kill stock-service during transaction → saga compensates, no leaks."""
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        await _wait_healthy(client)

        # Setup
        item_price = 50
        r = await client.post(f"{GATEWAY}/stock/item/create/{item_price}")
        item_id = r.json()["item_id"]
        initial_stock = 20
        await client.post(f"{GATEWAY}/stock/add/{item_id}/{initial_stock}")

        r = await client.post(f"{GATEWAY}/payment/create_user")
        user_id = r.json()["user_id"]
        await client.post(f"{GATEWAY}/payment/add_funds/{user_id}/50000")

        # Fire checkouts and kill stock-service mid-flight
        async def _checkout():
            r = await client.post(f"{GATEWAY}/orders/create/{user_id}")
            oid = r.json()["order_id"]
            await client.post(f"{GATEWAY}/orders/addItem/{oid}/{item_id}/1")
            try:
                return await client.post(f"{GATEWAY}/orders/checkout/{oid}")
            except (httpx.ReadError, httpx.RemoteProtocolError, httpx.ConnectError):
                return None

        tasks = [asyncio.create_task(_checkout()) for _ in range(5)]
        await asyncio.sleep(0.2)
        _kill_container("stock-service")

        await asyncio.gather(*tasks, return_exceptions=True)

        # Restart and wait for recovery
        _restart_container("stock-service")
        await asyncio.sleep(15.0)
        await _wait_healthy(client)

        # Consistency: stock >= 0, stock <= initial (no leaks or negative values)
        r = await client.get(f"{GATEWAY}/stock/find/{item_id}")
        stock_data = r.json()
        assert stock_data["stock"] >= 0, \
            f"Negative stock after crash recovery: {stock_data['stock']}"
        assert stock_data["stock"] <= initial_stock, \
            f"Stock exceeded initial after crash: {stock_data['stock']} > {initial_stock}"
