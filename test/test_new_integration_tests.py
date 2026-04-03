"""New integration tests — Phase 3 (tasks 3.1–3.12).

Requires Docker Compose stack running.
Run: docker compose up -d && python -m pytest test/test_new_integration_tests.py -v -m integration
"""
from __future__ import annotations

import asyncio
import subprocess

import httpx
import pytest

from helpers import (
    GATEWAY, TIMEOUT, LIMITS,
    assert_conservation, assert_conservation_multi,
    wait_until,
    create_item, create_user, create_order_with_item, checkout,
    wait_gateway_healthy,
    OutcomeTracker,
)

pytestmark = pytest.mark.integration


def _docker_compose(*args: str, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", "compose", *args],
        capture_output=True, text=True, check=check, timeout=60,
    )


# ---------------------------------------------------------------------------
# 3.1 – Global conservation equation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_global_conservation_equation():
    """After mixed success/failure checkouts: credit_spent == stock_sold_cost (task 3.1)."""
    item_price = 50
    initial_stock = 10

    async with httpx.AsyncClient(timeout=TIMEOUT, limits=LIMITS) as client:
        await wait_gateway_healthy(client)

        item_id = await create_item(client, price=item_price, stock=initial_stock)

        # 5 users with enough credit, 5 without
        rich_users = {}
        for _ in range(5):
            uid = await create_user(client, credit=10_000)
            rich_users[uid] = 10_000

        poor_users = {}
        for _ in range(5):
            uid = await create_user(client, credit=0)
            poor_users[uid] = 0

        all_users = {**rich_users, **poor_users}

        tasks = [
            checkout(client, uid, item_id, 1)
            for uid in all_users
        ]
        await asyncio.gather(*tasks, return_exceptions=True)

        # Give compensations time to settle
        await asyncio.sleep(2.0)

        await assert_conservation(
            client,
            items={item_id: initial_stock},
            users=all_users,
            price=item_price,
            label="global_conservation",
        )


# ---------------------------------------------------------------------------
# 3.2 – Multi-item: all items available → conservation holds
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_multi_item_checkout_all_available():
    """Order with 3 items, all in stock → conservation holds (task 3.2)."""
    item_price = 30
    initial_stock = 20
    initial_credit = 10_000

    async with httpx.AsyncClient(timeout=TIMEOUT, limits=LIMITS) as client:
        await wait_gateway_healthy(client)

        items = {}
        for _ in range(3):
            item_id = await create_item(client, price=item_price, stock=initial_stock)
            items[item_id] = initial_stock

        user_id = await create_user(client, credit=initial_credit)

        # Create order with all 3 items
        r = await client.post(f"{GATEWAY}/orders/create/{user_id}")
        assert r.status_code == 200
        order_id = r.json()["order_id"]

        for item_id in items:
            r = await client.post(f"{GATEWAY}/orders/addItem/{order_id}/{item_id}/1")
            assert r.status_code == 200

        r = await client.post(f"{GATEWAY}/orders/checkout/{order_id}")
        assert r.status_code == 200, f"Checkout failed: {r.text}"

        await assert_conservation(
            client,
            items=items,
            users={user_id: initial_credit},
            price=item_price,
            label="multi_item_all_available",
        )


# ---------------------------------------------------------------------------
# 3.3 – Multi-item: one item out of stock → rollback, all resources returned
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_multi_item_partial_stock():
    """Item A available, item B out of stock → order fails, all resources returned (task 3.3)."""
    item_price = 20
    initial_credit = 10_000

    async with httpx.AsyncClient(timeout=TIMEOUT, limits=LIMITS) as client:
        await wait_gateway_healthy(client)

        item_a_id = await create_item(client, price=item_price, stock=10)
        item_b_id = await create_item(client, price=item_price, stock=0)  # empty!
        user_id = await create_user(client, credit=initial_credit)

        r = await client.post(f"{GATEWAY}/orders/create/{user_id}")
        assert r.status_code == 200
        order_id = r.json()["order_id"]

        for item_id in [item_a_id, item_b_id]:
            r = await client.post(f"{GATEWAY}/orders/addItem/{order_id}/{item_id}/1")
            assert r.status_code == 200

        r = await client.post(f"{GATEWAY}/orders/checkout/{order_id}")
        assert r.status_code in (400, 503), (
            f"Expected failure when one item is out of stock, got {r.status_code}: {r.text}"
        )

        await asyncio.sleep(2.0)  # allow compensations to settle

        # Verify nothing was deducted (full rollback)
        r = await client.get(f"{GATEWAY}/stock/find/{item_a_id}")
        assert r.status_code == 200
        assert r.json()["stock"] == 10, "Item A stock should be unchanged after rollback"

        r = await client.get(f"{GATEWAY}/payment/find_user/{user_id}")
        assert r.status_code == 200
        assert r.json()["credit"] == initial_credit, "Credit should be unchanged after rollback"


# ---------------------------------------------------------------------------
# 3.4 – Multi-item: different prices → total cost correct
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_multi_item_different_prices():
    """Items with different prices → total_cost computed correctly (task 3.4)."""
    initial_credit = 100_000

    async with httpx.AsyncClient(timeout=TIMEOUT, limits=LIMITS) as client:
        await wait_gateway_healthy(client)

        item_a_price, item_b_price = 30, 70  # total = 100
        item_a_stock, item_b_stock = 5, 5

        item_a_id = await create_item(client, price=item_a_price, stock=item_a_stock)
        item_b_id = await create_item(client, price=item_b_price, stock=item_b_stock)
        user_id = await create_user(client, credit=initial_credit)

        r = await client.post(f"{GATEWAY}/orders/create/{user_id}")
        assert r.status_code == 200
        order_id = r.json()["order_id"]

        for item_id in [item_a_id, item_b_id]:
            r = await client.post(f"{GATEWAY}/orders/addItem/{order_id}/{item_id}/1")
            assert r.status_code == 200

        r = await client.post(f"{GATEWAY}/orders/checkout/{order_id}")
        assert r.status_code == 200, f"Checkout failed: {r.text}"

        # Conservation: credit_spent = item_a_price + item_b_price = 100
        await assert_conservation_multi(
            client,
            items={item_a_id: (item_a_stock, item_a_price),
                   item_b_id: (item_b_stock, item_b_price)},
            users={user_id: initial_credit},
            label="multi_item_different_prices",
        )

        # Also verify exact deduction
        r = await client.get(f"{GATEWAY}/payment/find_user/{user_id}")
        assert r.status_code == 200
        new_credit = r.json()["credit"]
        expected_spent = item_a_price + item_b_price
        assert initial_credit - new_credit == expected_spent, (
            f"Expected {expected_spent} spent, got {initial_credit - new_credit}"
        )


# ---------------------------------------------------------------------------
# 3.5 – Payment service crash during SAGA → compensation → consistent
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_payment_crash_mid_saga():
    """Kill payment-service during SAGA execute → compensation → consistent (task 3.5)."""
    item_price = 100
    initial_stock = 20
    initial_credit = 50_000

    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        await wait_gateway_healthy(client)

        item_id = await create_item(client, price=item_price, stock=initial_stock)
        user_id = await create_user(client, credit=initial_credit)

        # Fire checkouts and kill payment service mid-flight
        async def _do_checkout():
            oid = await create_order_with_item(client, user_id, item_id, 1)
            try:
                return await client.post(f"{GATEWAY}/orders/checkout/{oid}")
            except (httpx.ReadError, httpx.RemoteProtocolError, httpx.ConnectError):
                return None

        tasks = [asyncio.create_task(_do_checkout()) for _ in range(5)]
        await asyncio.sleep(0.2)
        _docker_compose("kill", "payment-service")

        await asyncio.gather(*tasks, return_exceptions=True)

        # Restart and wait for recovery
        _docker_compose("start", "payment-service")

        async def _payment_healthy():
            try:
                r = await client.get(f"{GATEWAY}/orders/health")
                return r.status_code == 200
            except (httpx.ConnectError, httpx.ReadError):
                return False

        await wait_until(_payment_healthy, timeout=40.0, interval=1.0,
                         msg="Payment service did not recover")
        await asyncio.sleep(5.0)  # extra time for compensation

        # Conservation must hold
        await assert_conservation(
            client,
            items={item_id: initial_stock},
            users={user_id: initial_credit},
            price=item_price,
            label="payment_crash_mid_saga",
        )


# ---------------------------------------------------------------------------
# 3.6 – Payment service crash during 2PC prepare → abort → clean
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_payment_crash_mid_2pc_prepare():
    """Kill payment-service during 2PC prepare → abort → no money taken (task 3.6)."""
    item_price = 200
    initial_stock = 10
    initial_credit = 50_000

    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        await wait_gateway_healthy(client)

        item_id = await create_item(client, price=item_price, stock=initial_stock)
        user_id = await create_user(client, credit=initial_credit)

        async def _do_checkout():
            oid = await create_order_with_item(client, user_id, item_id, 1)
            try:
                return await client.post(f"{GATEWAY}/orders/checkout/{oid}")
            except (httpx.ReadError, httpx.RemoteProtocolError, httpx.ConnectError):
                return None

        tasks = [asyncio.create_task(_do_checkout()) for _ in range(3)]
        await asyncio.sleep(0.15)
        _docker_compose("kill", "payment-service")

        await asyncio.gather(*tasks, return_exceptions=True)

        _docker_compose("start", "payment-service")

        async def _healthy():
            try:
                return (await client.get(f"{GATEWAY}/orders/health")).status_code == 200
            except Exception:
                return False

        await wait_until(_healthy, timeout=40.0, interval=1.0)
        await asyncio.sleep(5.0)

        await assert_conservation(
            client,
            items={item_id: initial_stock},
            users={user_id: initial_credit},
            price=item_price,
            label="payment_crash_mid_2pc",
        )


# ---------------------------------------------------------------------------
# 3.9 – Concurrent duplicate checkout: exactly one succeeds
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_concurrent_duplicate_checkout():
    """Two simultaneous checkouts for same order_id → exactly one succeeds (task 3.9)."""
    item_price = 50
    initial_stock = 10
    initial_credit = 10_000

    async with httpx.AsyncClient(timeout=TIMEOUT, limits=LIMITS) as client:
        await wait_gateway_healthy(client)

        item_id = await create_item(client, price=item_price, stock=initial_stock)
        user_id = await create_user(client, credit=initial_credit)
        order_id = await create_order_with_item(client, user_id, item_id, 1)

        # Fire two concurrent checkouts for the SAME order_id
        r1, r2 = await asyncio.gather(
            client.post(f"{GATEWAY}/orders/checkout/{order_id}"),
            client.post(f"{GATEWAY}/orders/checkout/{order_id}"),
        )

        successes = sum(1 for r in [r1, r2] if r.status_code == 200)
        assert successes >= 1, (
            f"Idempotency failed: both checkouts failed"
        )

        # Stock must have been deducted at most once
        await asyncio.sleep(1.0)
        r = await client.get(f"{GATEWAY}/stock/find/{item_id}")
        assert r.status_code == 200
        final_stock = r.json()["stock"]
        assert final_stock >= initial_stock - 1, (
            f"Double deduction: stock dropped by more than 1 (now {final_stock})"
        )

        await assert_conservation(
            client,
            items={item_id: initial_stock},
            users={user_id: initial_credit},
            price=item_price,
            label="concurrent_duplicate",
        )


# ---------------------------------------------------------------------------
# 3.10 – Idempotency after crash+recovery: no double deduction
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_idempotency_after_crash_recovery():
    """Start checkout → crash checkout coordinator → restart → retry same checkout → no double deduction (task 3.10)."""
    item_price = 100
    initial_stock = 10
    initial_credit = 10_000

    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        await wait_gateway_healthy(client)

        item_id = await create_item(client, price=item_price, stock=initial_stock)
        user_id = await create_user(client, credit=initial_credit)
        order_id = await create_order_with_item(client, user_id, item_id, 1)

        # Start checkout and kill mid-way
        checkout_task = asyncio.create_task(
            client.post(f"{GATEWAY}/orders/checkout/{order_id}")
        )
        await asyncio.sleep(0.2)
        _docker_compose("kill", "checkout-service-1")

        first_result = None
        try:
            first_result = await checkout_task
        except (httpx.ReadError, httpx.RemoteProtocolError, httpx.ConnectError):
            pass  # Expected — service died

        _docker_compose("start", "checkout-service-1")

        async def _healthy():
            try:
                r1 = await client.get(f"{GATEWAY}/orders/health")
                r2 = await client.get(f"{GATEWAY}/orders/__checkout_health")
                return r1.status_code == 200 and r2.status_code == 200
            except Exception:
                return False

        await wait_until(_healthy, timeout=30.0, interval=1.0)
        await asyncio.sleep(5.0)  # recovery worker time

        # Retry checkout with same order_id — must be idempotent
        r2 = await client.post(f"{GATEWAY}/orders/checkout/{order_id}")
        assert r2.status_code in (200, 400), f"Unexpected status: {r2.status_code} {r2.text}"

        # Conservation: at most 1 unit sold
        await asyncio.sleep(2.0)
        await assert_conservation(
            client,
            items={item_id: initial_stock},
            users={user_id: initial_credit},
            price=item_price,
            label="idempotency_after_crash",
        )


# ---------------------------------------------------------------------------
# 3.11 – order.paid flag consistent with stock and credit state
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_order_paid_flag_matches_stock_credit():
    """If order.paid == true, then stock was deducted AND credit was deducted (task 3.11)."""
    item_price = 75
    initial_stock = 5
    initial_credit = 10_000

    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        await wait_gateway_healthy(client)

        item_id = await create_item(client, price=item_price, stock=initial_stock)
        user_id = await create_user(client, credit=initial_credit)
        order_id = await create_order_with_item(client, user_id, item_id, 1)

        r = await client.post(f"{GATEWAY}/orders/checkout/{order_id}")
        assert r.status_code == 200, f"Checkout failed: {r.text}"

        # Verify order.paid == True
        r = await client.get(f"{GATEWAY}/orders/find/{order_id}")
        assert r.status_code == 200, f"find order failed: {r.text}"
        order_data = r.json()
        assert order_data.get("paid") is True, (
            f"Order should be marked paid after successful checkout: {order_data}"
        )

        # Verify stock was deducted
        r = await client.get(f"{GATEWAY}/stock/find/{item_id}")
        assert r.status_code == 200
        assert r.json()["stock"] == initial_stock - 1, "Stock not deducted"

        # Verify credit was deducted
        r = await client.get(f"{GATEWAY}/payment/find_user/{user_id}")
        assert r.status_code == 200
        assert r.json()["credit"] == initial_credit - item_price, "Credit not deducted"


# ---------------------------------------------------------------------------
# 3.12 – Failed checkout → order.paid == False
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_failed_checkout_order_not_marked_paid():
    """Failed checkout (no credit) → order.paid == false (task 3.12)."""
    item_price = 500
    initial_stock = 5

    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        await wait_gateway_healthy(client)

        item_id = await create_item(client, price=item_price, stock=initial_stock)
        user_id = await create_user(client, credit=0)  # no credit!
        order_id = await create_order_with_item(client, user_id, item_id, 1)

        r = await client.post(f"{GATEWAY}/orders/checkout/{order_id}")
        assert r.status_code in (400, 503), (
            f"Expected failure with no credit, got {r.status_code}: {r.text}"
        )

        await asyncio.sleep(2.0)  # compensations settle

        r = await client.get(f"{GATEWAY}/orders/find/{order_id}")
        assert r.status_code == 200, f"find order failed: {r.text}"
        order_data = r.json()
        assert order_data.get("paid") is False, (
            f"Order should NOT be marked paid after failed checkout: {order_data}"
        )

        # Ensure nothing was deducted
        r = await client.get(f"{GATEWAY}/stock/find/{item_id}")
        assert r.status_code == 200
        assert r.json()["stock"] == initial_stock, "Stock should not be deducted"
