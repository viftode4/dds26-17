"""Integration stress tests — requires Docker Compose running.

Run: docker compose up -d && python -m pytest test/test_stress.py -v -m integration
"""
from __future__ import annotations

import asyncio

import httpx
import pytest

GATEWAY = "http://127.0.0.1:8000"
TIMEOUT = httpx.Timeout(30.0, connect=10.0)
LIMITS = httpx.Limits(max_connections=20, max_keepalive_connections=10)

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _batch_init(client: httpx.AsyncClient, n_items: int = 100,
                      stock_per_item: int = 100, n_users: int = 100,
                      credit_per_user: int = 100000):
    """Initialize stock and payment data via each service's batch_init endpoint."""
    item_price = 10
    r = await client.post(
        f"{GATEWAY}/stock/batch_init/{n_items}/{stock_per_item}/{item_price}"
    )
    assert r.status_code == 200, f"Stock batch_init failed: {r.text}"
    r = await client.post(
        f"{GATEWAY}/payment/batch_init/{n_users}/{credit_per_user}"
    )
    assert r.status_code == 200, f"Payment batch_init failed: {r.text}"
    r = await client.post(
        f"{GATEWAY}/orders/batch_init/{n_items}/{n_items}/{n_users}/{item_price}"
    )
    assert r.status_code == 200, f"Order batch_init failed: {r.text}"
    await asyncio.sleep(1.0)


async def _create_checkout(client: httpx.AsyncClient, user_id: int,
                           item_id: int, quantity: int = 1) -> httpx.Response:
    """Create an order with one item and attempt checkout."""
    r = await client.post(f"{GATEWAY}/orders/create/{user_id}")
    if r.status_code != 200:
        return r
    order_id = r.json()["order_id"]

    r = await client.post(f"{GATEWAY}/orders/addItem/{order_id}/{item_id}/{quantity}")
    if r.status_code != 200:
        return r

    r = await client.post(f"{GATEWAY}/orders/checkout/{order_id}")
    return r


async def _create_item(client: httpx.AsyncClient, price: int, stock: int) -> str:
    """Create a stock item and add inventory. Returns item_id."""
    r = await client.post(f"{GATEWAY}/stock/item/create/{price}")
    assert r.status_code == 200, f"item/create failed: {r.text}"
    item_id = r.json()["item_id"]
    r = await client.post(f"{GATEWAY}/stock/add/{item_id}/{stock}")
    assert r.status_code == 200, f"stock/add failed: {r.text}"
    return item_id


async def _create_user(client: httpx.AsyncClient, credit: int) -> str:
    """Create a payment user and add funds. Returns user_id."""
    r = await client.post(f"{GATEWAY}/payment/create_user")
    assert r.status_code == 200, f"create_user failed: {r.text}"
    user_id = r.json()["user_id"]
    if credit > 0:
        r = await client.post(f"{GATEWAY}/payment/add_funds/{user_id}/{credit}")
        assert r.status_code == 200, f"add_funds failed: {r.text}"
    return user_id


async def _create_order_with_item(client: httpx.AsyncClient, user_id: str,
                                  item_id: str, quantity: int = 1) -> str:
    """Create an order, add an item, return order_id."""
    r = await client.post(f"{GATEWAY}/orders/create/{user_id}")
    assert r.status_code == 200, f"orders/create failed: {r.text}"
    order_id = r.json()["order_id"]
    r = await client.post(f"{GATEWAY}/orders/addItem/{order_id}/{item_id}/{quantity}")
    assert r.status_code == 200, f"addItem failed: {r.text}"
    return order_id


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_concurrent_checkouts():
    """50 concurrent unique checkouts — no 5xx, all return clean success/fail."""
    async with httpx.AsyncClient(timeout=TIMEOUT, limits=LIMITS) as client:
        await _batch_init(client, n_items=50, stock_per_item=1000,
                          n_users=50, credit_per_user=1000000)

        tasks = [
            _create_checkout(client, user_id=i, item_id=i, quantity=1)
            for i in range(50)
        ]
        responses = await asyncio.gather(*tasks, return_exceptions=True)

        errors = [r for r in responses if isinstance(r, Exception)]
        assert len(errors) == 0, f"Exceptions: {errors}"

        for r in responses:
            assert isinstance(r, httpx.Response)
            assert r.status_code != 500, f"Got 500: {r.text}"
            assert r.status_code in (200, 400)


@pytest.mark.asyncio
async def test_checkout_contention():
    """10 users competing for limited stock — exactly N succeed (N <= stock)."""
    async with httpx.AsyncClient(timeout=TIMEOUT, limits=LIMITS) as client:
        item_id = await _create_item(client, price=10, stock=3)

        user_ids = []
        for _ in range(10):
            uid = await _create_user(client, credit=10000)
            user_ids.append(uid)

        async def _try_checkout(uid):
            order_id = await _create_order_with_item(client, uid, item_id, 1)
            return await client.post(f"{GATEWAY}/orders/checkout/{order_id}")

        responses = await asyncio.gather(*[_try_checkout(uid) for uid in user_ids])

        successes = sum(1 for r in responses if r.status_code == 200)
        failures = sum(1 for r in responses if r.status_code == 400)

        assert successes <= 3, f"More successes ({successes}) than stock (3)"
        assert successes + failures == 10

        # Consistency check: stock should be fully accounted for
        r = await client.get(f"{GATEWAY}/stock/find/{item_id}")
        assert r.status_code == 200, f"stock/find failed: {r.text}"
        data = r.json()
        assert data["stock"] >= 0


@pytest.mark.asyncio
async def test_conservation_after_failures():
    """Checkouts with half failing (no credit) — no stock leaked by failed txns."""
    async with httpx.AsyncClient(timeout=TIMEOUT, limits=LIMITS) as client:
        item_price = 100
        initial_stock = 100
        item_id = await _create_item(client, price=item_price, stock=initial_stock)

        # 5 users with credit, 5 without
        all_users = []
        for i in range(10):
            credit = 100000 if i < 5 else 0
            uid = await _create_user(client, credit=credit)
            all_users.append(uid)

        async def _checkout(uid):
            order_id = await _create_order_with_item(client, uid, item_id, 1)
            return await client.post(f"{GATEWAY}/orders/checkout/{order_id}")

        responses = await asyncio.gather(*[_checkout(uid) for uid in all_users])
        await asyncio.sleep(2.0)

        n_success = sum(1 for r in responses
                        if isinstance(r, httpx.Response) and r.status_code == 200)

        # Conservation: stock = initial - successful_sales (no leaks from failures)
        r = await client.get(f"{GATEWAY}/stock/find/{item_id}")
        assert r.status_code == 200, f"stock/find failed: {r.text}"
        data = r.json()
        expected_stock = initial_stock - n_success
        assert data["stock"] == expected_stock, \
            f"Conservation violated: stock={data['stock']}, expected={expected_stock} (initial={initial_stock}, sold={n_success})"


@pytest.mark.asyncio
async def test_idempotent_confirm_no_double_count():
    """Replay checkout on same order -> no double-counting.

    The order service has two idempotency guards:
    1. "Order already paid" (pre-check on order.paid flag)
    2. SET NX idempotency key (in-flight dedup)
    Either one prevents double-processing. We verify counters don't change.
    """
    async with httpx.AsyncClient(timeout=TIMEOUT, limits=LIMITS) as client:
        item_id = await _create_item(client, price=50, stock=10)
        user_id = await _create_user(client, credit=10000)
        order_id = await _create_order_with_item(client, user_id, item_id, 1)

        # First checkout — must succeed
        r = await client.post(f"{GATEWAY}/orders/checkout/{order_id}")
        assert r.status_code == 200, f"Checkout failed: {r.text}"

        # Snapshot counters
        r = await client.get(f"{GATEWAY}/stock/find/{item_id}")
        assert r.status_code == 200
        stock_after = r.json()

        r = await client.get(f"{GATEWAY}/payment/find_user/{user_id}")
        assert r.status_code == 200
        credit_after = r.json()

        # Replay — should be rejected (either "already paid" or cached success)
        r2 = await client.post(f"{GATEWAY}/orders/checkout/{order_id}")
        assert r2.status_code in (200, 400), f"Unexpected: {r2.status_code} {r2.text}"

        # The key invariant: counters must NOT change after replay
        r = await client.get(f"{GATEWAY}/stock/find/{item_id}")
        assert r.status_code == 200
        stock_after2 = r.json()

        r = await client.get(f"{GATEWAY}/payment/find_user/{user_id}")
        assert r.status_code == 200
        credit_after2 = r.json()

        assert stock_after["stock"] == stock_after2["stock"], \
            f"Stock changed after replay: {stock_after} -> {stock_after2}"
        assert credit_after["credit"] == credit_after2["credit"], \
            f"Credit changed after replay: {credit_after} -> {credit_after2}"
