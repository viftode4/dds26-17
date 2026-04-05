"""Chaos engineering tests — app-level fault injection.

Uses the /fault/set HTTP API to inject crashes, delays, and errors at specific
saga phases, then verifies conservation after recovery.

The pattern is always:
  1. Seed data + pre-create orders
  2. Inject fault
  3. Fire checkouts (fire-and-forget, don't care about HTTP response)
  4. Restore system (restart crashed service / clear fault)
  5. Wait for recovery worker + retry loops
  6. Assert conservation

Run: docker compose -f docker-compose-small.yml up -d
     python -m pytest test/test_chaos_framework.py -v -m "integration and slow"
"""
from __future__ import annotations

import asyncio
import subprocess

import httpx
import pytest

from helpers import (
    GATEWAY, TIMEOUT, LIMITS,
    assert_conservation,
    wait_until,
    create_item, create_user,
    create_order_with_item,
    wait_gateway_healthy,
    OutcomeTracker,
)

pytestmark = [pytest.mark.integration, pytest.mark.slow]

COMPOSE_FILE = ["-f", "docker-compose-small.yml"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _docker_compose(*args: str, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", "compose", *COMPOSE_FILE, *args],
        capture_output=True, text=True, check=check, timeout=60,
    )


async def _set_fault(
    client: httpx.AsyncClient,
    service: str,
    point: str,
    action: str,
    value: int = 0,
) -> None:
    """Set a fault rule on a service via the gateway."""
    r = await client.post(
        f"{GATEWAY}/{service}/fault/set",
        json={"point": point, "action": action, "value": value},
    )
    assert r.status_code == 200, f"Failed to set fault on {service}: {r.text}"


async def _clear_fault(
    client: httpx.AsyncClient,
    service: str,
    point: str | None = None,
) -> None:
    """Clear fault rule(s) on a service via the gateway."""
    url = f"{GATEWAY}/{service}/fault/clear"
    if point:
        url += f"?point={point}"
    r = await client.post(url)
    assert r.status_code == 200, f"Failed to clear fault on {service}: {r.text}"


async def _service_healthy(client: httpx.AsyncClient, service: str) -> bool:
    """Check if a service is reachable through the gateway."""
    try:
        r = await client.get(f"{GATEWAY}/{service}/health")
        return r.status_code == 200
    except Exception:
        return False


async def _fire_checkouts(
    client: httpx.AsyncClient,
    order_ids: list[str],
    tracker: OutcomeTracker,
) -> None:
    """Fire checkout requests concurrently, recording outcomes. Never raises."""
    async def _do(order_id: str):
        try:
            r = await client.post(f"{GATEWAY}/orders/checkout/{order_id}")
            return tracker.record_response(r)
        except (httpx.ReadError, httpx.RemoteProtocolError,
                httpx.ConnectError, httpx.TimeoutException) as e:
            return tracker.record_exception(e)

    await asyncio.gather(*[_do(oid) for oid in order_ids], return_exceptions=True)


# ---------------------------------------------------------------------------
# Test 1: Crash after stock execute (saga)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_crash_after_stock_execute():
    """Stock crashes after saga execute. Recovery worker compensates.

    Stock deducts inventory, then crashes before responding. The orchestrator
    times out, and the recovery worker picks up the incomplete saga and
    compensates. Conservation must hold.
    """
    async with httpx.AsyncClient(timeout=TIMEOUT, limits=LIMITS) as client:
        await wait_gateway_healthy(client)

        item_id = await create_item(client, price=10, stock=100)
        user_id = await create_user(client, credit=1000)
        tracker = OutcomeTracker()

        # Pre-create orders before injecting fault
        order_ids = []
        for _ in range(3):
            order_ids.append(await create_order_with_item(client, user_id, item_id, 2))

        # Inject: crash after stock execute completes
        await _set_fault(client, "stock", "after_execute", "crash")

        # Fire checkouts — stock will crash, responses will be errors/timeouts
        await _fire_checkouts(client, order_ids, tracker)

        # Restore: restart stock, clear fault
        _docker_compose("start", "stock-service-1")
        await asyncio.sleep(5)
        await wait_until(
            lambda: _service_healthy(client, "stock"),
            timeout=30.0, interval=1.0,
            msg="Stock service did not restart",
        )
        await _clear_fault(client, "stock")

        # Wait for recovery worker to process incomplete sagas
        await wait_gateway_healthy(client)
        await asyncio.sleep(30)

        await assert_conservation(
            client,
            items={item_id: 100},
            users={user_id: 1000},
            price=10,
            label="crash_after_stock_execute",
        )
        print(f"crash_after_stock_execute outcomes: {tracker.summary()}")


# ---------------------------------------------------------------------------
# Test 2: Crash after payment execute (saga)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_crash_after_payment_execute():
    """Payment crashes after saga execute. Recovery worker compensates both services."""
    async with httpx.AsyncClient(timeout=TIMEOUT, limits=LIMITS) as client:
        await wait_gateway_healthy(client)

        item_id = await create_item(client, price=5, stock=50)
        user_id = await create_user(client, credit=500)
        tracker = OutcomeTracker()

        order_ids = []
        for _ in range(3):
            order_ids.append(await create_order_with_item(client, user_id, item_id, 2))

        await _set_fault(client, "payment", "after_execute", "crash")
        await _fire_checkouts(client, order_ids, tracker)

        _docker_compose("start", "payment-service-1")
        await asyncio.sleep(5)
        await wait_until(
            lambda: _service_healthy(client, "payment"),
            timeout=30.0, interval=1.0,
            msg="Payment service did not restart",
        )
        await _clear_fault(client, "payment")
        await wait_gateway_healthy(client)
        await asyncio.sleep(30)

        await assert_conservation(
            client,
            items={item_id: 50},
            users={user_id: 500},
            price=5,
            label="crash_after_payment_execute",
        )
        print(f"crash_after_payment_execute outcomes: {tracker.summary()}")


# ---------------------------------------------------------------------------
# Test 3: Transient error on stock compensate
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_transient_error_on_compensate():
    """Stock compensate returns errors temporarily. Orchestrator retries until cleared.

    We inject an error, fire checkouts that need compensation (insufficient
    credit), then clear the fault. The retry loop (or recovery worker) resolves
    the compensation and conservation holds.
    """
    async with httpx.AsyncClient(timeout=TIMEOUT, limits=LIMITS) as client:
        await wait_gateway_healthy(client)

        item_id = await create_item(client, price=10, stock=100)
        user_id = await create_user(client, credit=5)  # Insufficient for any item
        tracker = OutcomeTracker()

        order_ids = []
        for _ in range(3):
            order_ids.append(await create_order_with_item(client, user_id, item_id, 1))

        # Inject error on compensate
        await _set_fault(client, "stock", "before_compensate", "error")

        # Fire checkouts — payment will fail, compensation will be retried
        await _fire_checkouts(client, order_ids, tracker)

        # Clear the fault after checkouts have been attempted
        await _clear_fault(client, "stock")

        # Allow retry loops / recovery worker to finish compensation
        await asyncio.sleep(30)

        await assert_conservation(
            client,
            items={item_id: 100},
            users={user_id: 5},
            price=10,
            label="transient_error_on_compensate",
        )
        print(f"transient_error_on_compensate outcomes: {tracker.summary()}")


# ---------------------------------------------------------------------------
# Test 4: Delay on stock commit (2PC path)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_delay_on_commit():
    """Stock commit takes 3 seconds. 2PC should still complete successfully.

    Verified_action's retry loop should tolerate the delay and eventually
    receive the "committed" response.
    """
    async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0), limits=LIMITS) as client:
        await wait_gateway_healthy(client)

        item_id = await create_item(client, price=10, stock=100)
        user_id = await create_user(client, credit=1000)
        tracker = OutcomeTracker()

        order_ids = []
        for _ in range(3):
            order_ids.append(await create_order_with_item(client, user_id, item_id, 1))

        # 3 second delay on commit
        await _set_fault(client, "stock", "before_commit", "delay", value=3000)

        await _fire_checkouts(client, order_ids, tracker)

        await _clear_fault(client, "stock")
        await asyncio.sleep(15)

        await assert_conservation(
            client,
            items={item_id: 100},
            users={user_id: 1000},
            price=10,
            label="delay_on_commit",
        )
        print(f"delay_on_commit outcomes: {tracker.summary()}")


# ---------------------------------------------------------------------------
# Test 5: Crash after stock prepare (2PC abort path)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_crash_after_prepare():
    """Stock crashes after prepare. 2PC aborts and releases locks via recovery."""
    async with httpx.AsyncClient(timeout=TIMEOUT, limits=LIMITS) as client:
        await wait_gateway_healthy(client)

        item_id = await create_item(client, price=10, stock=100)
        user_id = await create_user(client, credit=1000)
        tracker = OutcomeTracker()

        order_ids = []
        for _ in range(3):
            order_ids.append(await create_order_with_item(client, user_id, item_id, 2))

        await _set_fault(client, "stock", "after_prepare", "crash")
        await _fire_checkouts(client, order_ids, tracker)

        _docker_compose("start", "stock-service-1")
        await asyncio.sleep(5)
        await wait_until(
            lambda: _service_healthy(client, "stock"),
            timeout=30.0, interval=1.0,
            msg="Stock service did not restart",
        )
        await _clear_fault(client, "stock")
        await wait_gateway_healthy(client)
        await asyncio.sleep(30)

        await assert_conservation(
            client,
            items={item_id: 100},
            users={user_id: 1000},
            price=10,
            label="crash_after_prepare",
        )
        print(f"crash_after_prepare outcomes: {tracker.summary()}")


# ---------------------------------------------------------------------------
# Test 6: Crash during payment compensate (recovery worker resumes)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_crash_during_compensate():
    """Payment crashes while compensating. Recovery worker finishes it."""
    async with httpx.AsyncClient(timeout=TIMEOUT, limits=LIMITS) as client:
        await wait_gateway_healthy(client)

        item_id = await create_item(client, price=10, stock=100)
        user_id = await create_user(client, credit=5)  # Insufficient
        tracker = OutcomeTracker()

        order_ids = []
        for _ in range(3):
            order_ids.append(await create_order_with_item(client, user_id, item_id, 1))

        await _set_fault(client, "payment", "before_compensate", "crash")
        await _fire_checkouts(client, order_ids, tracker)

        await asyncio.sleep(3)
        _docker_compose("start", "payment-service-1")
        await asyncio.sleep(5)
        await wait_until(
            lambda: _service_healthy(client, "payment"),
            timeout=30.0, interval=1.0,
            msg="Payment service did not restart",
        )
        await _clear_fault(client, "payment")
        await wait_gateway_healthy(client)
        await asyncio.sleep(30)

        await assert_conservation(
            client,
            items={item_id: 100},
            users={user_id: 5},
            price=10,
            label="crash_during_compensate",
        )
        print(f"crash_during_compensate outcomes: {tracker.summary()}")
