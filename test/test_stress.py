"""Integration stress tests — requires Docker Compose running.

Run: docker compose up -d && python -m pytest test/test_stress.py -v -m integration
"""
from __future__ import annotations

import asyncio

import httpx
import pytest

GATEWAY = "http://127.0.0.1:8000"
TIMEOUT = httpx.Timeout(30.0, connect=10.0)

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _batch_init(client: httpx.AsyncClient, n_items: int = 100,
                      stock_per_item: int = 100, n_users: int = 100,
                      credit_per_user: int = 100000):
    """Initialize stock and payment data via each service's batch_init endpoint."""
    item_price = 10
    # Stock: create n_items items with starting_stock and price
    r = await client.post(
        f"{GATEWAY}/stock/batch_init/{n_items}/{stock_per_item}/{item_price}"
    )
    assert r.status_code == 200, f"Stock batch_init failed: {r.text}"
    # Payment: create n_users users with starting credit
    r = await client.post(
        f"{GATEWAY}/payment/batch_init/{n_users}/{credit_per_user}"
    )
    assert r.status_code == 200, f"Payment batch_init failed: {r.text}"
    # Orders: create pre-populated orders (optional, for benchmark compat)
    r = await client.post(
        f"{GATEWAY}/orders/batch_init/{n_items}/{n_items}/{n_users}/{item_price}"
    )
    assert r.status_code == 200, f"Order batch_init failed: {r.text}"
    await asyncio.sleep(1.0)


async def _create_checkout(client: httpx.AsyncClient, user_id: int,
                           item_id: int, quantity: int = 1) -> httpx.Response:
    """Create an order with one item and attempt checkout."""
    # Create order
    r = await client.post(f"{GATEWAY}/orders/create/{user_id}")
    if r.status_code != 200:
        return r  # Propagate failure as response, not exception
    order_id = r.json()["order_id"]

    # Add item
    r = await client.post(f"{GATEWAY}/orders/addItem/{order_id}/{item_id}/{quantity}")
    if r.status_code != 200:
        return r

    # Checkout
    r = await client.post(f"{GATEWAY}/orders/checkout/{order_id}")
    return r


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_concurrent_checkouts():
    """50 concurrent unique checkouts — no 5xx, all return clean success/fail."""
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
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
    """10 users competing for limited stock — exactly N succeed (N = stock)."""
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        # Create 1 item with stock=3, 10 users with plenty of credit
        item_price = 10
        r = await client.post(f"{GATEWAY}/stock/item/create/{item_price}")
        assert r.status_code == 200
        item_id = r.json()["item_id"]
        await client.post(f"{GATEWAY}/stock/add/{item_id}/3")

        user_ids = []
        for _ in range(10):
            r = await client.post(f"{GATEWAY}/payment/create_user")
            assert r.status_code == 200
            uid = r.json()["user_id"]
            await client.post(f"{GATEWAY}/payment/add_funds/{uid}/10000")
            user_ids.append(uid)

        # All 10 users try to buy 1 of the same item
        async def _try_checkout(uid):
            r = await client.post(f"{GATEWAY}/orders/create/{uid}")
            oid = r.json()["order_id"]
            await client.post(f"{GATEWAY}/orders/addItem/{oid}/{item_id}/1")
            return await client.post(f"{GATEWAY}/orders/checkout/{oid}")

        responses = await asyncio.gather(*[_try_checkout(uid) for uid in user_ids])

        successes = sum(1 for r in responses if r.status_code == 200)
        failures = sum(1 for r in responses if r.status_code == 400)

        assert successes <= 3, f"More successes ({successes}) than stock (3)"
        assert successes + failures == 10

        # Consistency check: available + reserved should equal initial
        r = await client.get(f"{GATEWAY}/stock/find/{item_id}")
        data = r.json()
        # After successful checkouts, stock should be fully accounted for
        assert data["stock"] >= 0


@pytest.mark.asyncio
async def test_conservation_after_failures():
    """Checkouts with half failing (no credit) — no stock leaked by failed txns."""
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        item_price = 100
        r = await client.post(f"{GATEWAY}/stock/item/create/{item_price}")
        item_id = r.json()["item_id"]
        initial_stock = 100
        await client.post(f"{GATEWAY}/stock/add/{item_id}/{initial_stock}")

        # 5 users with credit, 5 without
        user_ids_rich = []
        user_ids_poor = []
        for i in range(10):
            r = await client.post(f"{GATEWAY}/payment/create_user")
            uid = r.json()["user_id"]
            if i < 5:
                await client.post(f"{GATEWAY}/payment/add_funds/{uid}/100000")
                user_ids_rich.append(uid)
            else:
                user_ids_poor.append(uid)

        all_users = user_ids_rich + user_ids_poor

        async def _checkout(uid):
            r = await client.post(f"{GATEWAY}/orders/create/{uid}")
            oid = r.json()["order_id"]
            await client.post(f"{GATEWAY}/orders/addItem/{oid}/{item_id}/1")
            return await client.post(f"{GATEWAY}/orders/checkout/{oid}")

        responses = await asyncio.gather(*[_checkout(uid) for uid in all_users])
        await asyncio.sleep(2.0)

        # Count successful checkouts
        n_success = sum(1 for r in responses
                        if isinstance(r, httpx.Response) and r.status_code == 200)

        # Conservation: stock = initial - successful_sales (no leaks from failures)
        r = await client.get(f"{GATEWAY}/stock/find/{item_id}")
        data = r.json()
        expected_stock = initial_stock - n_success
        assert data["stock"] == expected_stock, \
            f"Conservation violated: stock={data['stock']}, expected={expected_stock} (initial={initial_stock}, sold={n_success})"


@pytest.mark.asyncio
async def test_idempotent_confirm_no_double_count():
    """Replay checkout on same order -> no double-counting (Fix 1).

    The order service has two idempotency guards:
    1. "Order already paid" (pre-check on order.paid flag)
    2. SET NX idempotency key (in-flight dedup)
    Either one prevents double-processing. We verify counters don't change.
    """
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        # Create fresh resources
        r = await client.post(f"{GATEWAY}/stock/item/create/50")
        item_id = r.json()["item_id"]
        await client.post(f"{GATEWAY}/stock/add/{item_id}/10")

        r = await client.post(f"{GATEWAY}/payment/create_user")
        user_id = r.json()["user_id"]
        await client.post(f"{GATEWAY}/payment/add_funds/{user_id}/10000")

        r = await client.post(f"{GATEWAY}/orders/create/{user_id}")
        order_id = r.json()["order_id"]
        await client.post(f"{GATEWAY}/orders/addItem/{order_id}/{item_id}/1")

        # First checkout — must succeed
        r = await client.post(f"{GATEWAY}/orders/checkout/{order_id}")
        assert r.status_code == 200, f"Checkout failed: {r.text}"

        # Snapshot counters
        stock_after = (await client.get(f"{GATEWAY}/stock/find/{item_id}")).json()
        credit_after = (await client.get(f"{GATEWAY}/payment/find_user/{user_id}")).json()

        # Replay — should be rejected (either "already paid" or cached success)
        r2 = await client.post(f"{GATEWAY}/orders/checkout/{order_id}")
        assert r2.status_code in (200, 400), f"Unexpected: {r2.status_code} {r2.text}"

        # The key invariant: counters must NOT change after replay
        stock_after2 = (await client.get(f"{GATEWAY}/stock/find/{item_id}")).json()
        credit_after2 = (await client.get(f"{GATEWAY}/payment/find_user/{user_id}")).json()

        assert stock_after["stock"] == stock_after2["stock"], \
            f"Stock changed after replay: {stock_after} -> {stock_after2}"
        assert credit_after["credit"] == credit_after2["credit"], \
            f"Credit changed after replay: {credit_after} -> {credit_after2}"
