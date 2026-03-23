"""Integration stress tests — requires Docker Compose running.

Run: docker compose up -d && python -m pytest test/test_stress.py -v -m integration
"""
from __future__ import annotations

import asyncio

import httpx
import pytest

from helpers import (
    GATEWAY, TIMEOUT, LIMITS,
    assert_conservation,
    wait_until,
    create_item,
    create_user,
    create_order_with_item,
)

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


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_concurrent_checkouts():
    """50 concurrent unique checkouts — no 5xx, all return clean success/fail.

    Task 1.1: also verify total_stock_deducted == count(200_responses).
    """
    async with httpx.AsyncClient(timeout=TIMEOUT, limits=LIMITS) as client:
        item_price = 10
        initial_stock = 1000
        # Create 50 separate items (one per user) so no contention
        item_ids = []
        user_ids = []
        for _ in range(50):
            item_id = await create_item(client, price=item_price, stock=initial_stock)
            item_ids.append(item_id)
            user_id = await create_user(client, credit=1_000_000)
            user_ids.append(user_id)

        tasks = [
            create_order_with_item(client, user_ids[i], item_ids[i], 1)
            for i in range(50)
        ]
        order_ids = await asyncio.gather(*tasks)

        checkout_tasks = [
            client.post(f"{GATEWAY}/orders/checkout/{oid}")
            for oid in order_ids
        ]
        responses = await asyncio.gather(*checkout_tasks, return_exceptions=True)

        errors = [r for r in responses if isinstance(r, Exception)]
        assert len(errors) == 0, f"Exceptions: {errors}"

        successes = 0
        for r in responses:
            assert isinstance(r, httpx.Response)
            assert r.status_code != 500, f"Got 500: {r.text}"
            assert r.status_code in (200, 400)
            if r.status_code == 200:
                successes += 1

        # Task 1.1: verify total stock deducted equals number of successful checkouts
        total_deducted = 0
        for item_id in item_ids:
            r = await client.get(f"{GATEWAY}/stock/find/{item_id}")
            assert r.status_code == 200
            total_deducted += initial_stock - r.json()["stock"]

        assert total_deducted == successes, (
            f"Conservation gap: {total_deducted} units deducted but only {successes} succeeded"
        )


@pytest.mark.asyncio
async def test_checkout_contention():
    """10 users competing for limited stock — exactly N succeed (N <= stock).

    Task 1.2: exact conservation check (stock == 3 - successes) + credit side.
    """
    item_price = 10
    initial_stock = 3
    initial_credit = 10000

    async with httpx.AsyncClient(timeout=TIMEOUT, limits=LIMITS) as client:
        item_id = await create_item(client, price=item_price, stock=initial_stock)

        user_ids = []
        for _ in range(10):
            uid = await create_user(client, credit=initial_credit)
            user_ids.append(uid)

        async def _try_checkout(uid):
            order_id = await create_order_with_item(client, uid, item_id, 1)
            return await client.post(f"{GATEWAY}/orders/checkout/{order_id}")

        responses = await asyncio.gather(*[_try_checkout(uid) for uid in user_ids])

        successes = sum(1 for r in responses if r.status_code == 200)
        failures = sum(1 for r in responses if r.status_code == 400)

        assert successes <= initial_stock, (
            f"Over-sold: {successes} successes but only {initial_stock} in stock"
        )
        assert successes + failures == 10, (
            f"Lost requests: {successes} + {failures} != 10"
        )

        # Task 1.2a: exact stock conservation (not just >= 0)
        r = await client.get(f"{GATEWAY}/stock/find/{item_id}")
        assert r.status_code == 200, f"stock/find failed: {r.text}"
        final_stock = r.json()["stock"]
        assert final_stock == initial_stock - successes, (
            f"Stock not exact: {final_stock} != {initial_stock} - {successes}"
        )

        # Task 1.2b: credit-side verification
        for uid in user_ids:
            r = await client.get(f"{GATEWAY}/payment/find_user/{uid}")
            assert r.status_code == 200
            assert r.json()["credit"] >= 0, f"Negative credit for user {uid}"

        total_credit_spent = sum(
            initial_credit - (
                (await client.get(f"{GATEWAY}/payment/find_user/{uid}")).json()["credit"]
            )
            for uid in user_ids
        )
        assert total_credit_spent == successes * item_price, (
            f"Credit conservation: spent={total_credit_spent} "
            f"expected={successes * item_price}"
        )


@pytest.mark.asyncio
async def test_conservation_after_failures():
    """Checkouts with half failing (no credit) — no stock leaked by failed txns.

    Task 1.3: also verify credit side of the conservation equation.
    """
    item_price = 100
    initial_stock = 100
    initial_credit_rich = 100000

    async with httpx.AsyncClient(timeout=TIMEOUT, limits=LIMITS) as client:
        item_id = await create_item(client, price=item_price, stock=initial_stock)

        # 5 users with credit, 5 without
        rich_users = []
        poor_users = []
        for i in range(10):
            if i < 5:
                uid = await create_user(client, credit=initial_credit_rich)
                rich_users.append(uid)
            else:
                uid = await create_user(client, credit=0)
                poor_users.append(uid)

        all_users = rich_users + poor_users

        async def _checkout(uid):
            order_id = await create_order_with_item(client, uid, item_id, 1)
            return await client.post(f"{GATEWAY}/orders/checkout/{order_id}")

        responses = await asyncio.gather(*[_checkout(uid) for uid in all_users])

        # Wait for any async compensations to settle
        await asyncio.sleep(2.0)

        n_success = sum(
            1 for r in responses
            if isinstance(r, httpx.Response) and r.status_code == 200
        )

        # Stock side: exact conservation
        r = await client.get(f"{GATEWAY}/stock/find/{item_id}")
        assert r.status_code == 200, f"stock/find failed: {r.text}"
        final_stock = r.json()["stock"]
        expected_stock = initial_stock - n_success
        assert final_stock == expected_stock, (
            f"Stock conservation violated: stock={final_stock}, "
            f"expected={expected_stock} (initial={initial_stock}, sold={n_success})"
        )

        # Task 1.3: credit side — only rich users can have spent money
        total_credit_spent = 0
        for uid in rich_users:
            r = await client.get(f"{GATEWAY}/payment/find_user/{uid}")
            assert r.status_code == 200
            data = r.json()
            spent = initial_credit_rich - data["credit"]
            assert spent >= 0, f"User {uid} credit increased unexpectedly"
            total_credit_spent += spent

        for uid in poor_users:
            r = await client.get(f"{GATEWAY}/payment/find_user/{uid}")
            assert r.status_code == 200
            assert r.json()["credit"] == 0, (
                f"Poor user {uid} was charged despite having no credit"
            )

        assert total_credit_spent == n_success * item_price, (
            f"Credit conservation violated: "
            f"credit_spent={total_credit_spent}, "
            f"expected={n_success * item_price}"
        )


@pytest.mark.asyncio
async def test_idempotent_confirm_no_double_count():
    """Replay checkout on same order -> no double-counting.

    The order service has two idempotency guards:
    1. "Order already paid" (pre-check on order.paid flag)
    2. SET NX idempotency key (in-flight dedup)
    Either one prevents double-processing. We verify counters don't change.
    """
    async with httpx.AsyncClient(timeout=TIMEOUT, limits=LIMITS) as client:
        item_id = await create_item(client, price=50, stock=10)
        user_id = await create_user(client, credit=10000)
        order_id = await create_order_with_item(client, user_id, item_id, 1)

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

        assert stock_after["stock"] == stock_after2["stock"], (
            f"Stock changed after replay: {stock_after} -> {stock_after2}"
        )
        assert credit_after["credit"] == credit_after2["credit"], (
            f"Credit changed after replay: {credit_after} -> {credit_after2}"
        )
