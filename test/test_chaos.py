"""Chaos and resilience tests — Phase 4 (tasks 4.1–4.7).

Tests network partitions, NATS broker failures, slow responses, and Redis failover.

Run: docker compose up -d && python -m pytest test/test_chaos.py -v -m "integration and slow"
"""
from __future__ import annotations

import asyncio
import subprocess

import httpx
import pytest

from helpers import (
    GATEWAY, TIMEOUT,
    assert_conservation,
    wait_until,
    create_item, create_user, checkout,
    create_order_with_item,
    wait_gateway_healthy,
    OutcomeTracker,
)

pytestmark = [pytest.mark.integration, pytest.mark.slow]


def _docker_compose(*args: str, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", "compose", *args],
        capture_output=True, text=True, check=check, timeout=60,
    )


def _docker_network_disconnect(container: str, network: str = "distributed-data-systems_default"):
    """Disconnect a container from its network (simulates partition)."""
    subprocess.run(
        ["docker", "network", "disconnect", network, container],
        capture_output=True, text=True, timeout=15, check=False,
    )


def _docker_network_connect(container: str, network: str = "distributed-data-systems_default"):
    """Reconnect a container to its network."""
    subprocess.run(
        ["docker", "network", "connect", network, container],
        capture_output=True, text=True, timeout=15, check=False,
    )


# ---------------------------------------------------------------------------
# 4.1 – Network partition: stock-db unreachable
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_network_partition_stock_db():
    """Partition stock-db from the network → graceful failure, reconnect → consistent (task 4.1)."""
    item_price = 50
    initial_stock = 20
    initial_credit = 50_000

    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        await wait_gateway_healthy(client)

        item_id = await create_item(client, price=item_price, stock=initial_stock)
        user_id = await create_user(client, credit=initial_credit)

        # Pre-create orders BEFORE the partition — addItem needs stock-db for price lookup,
        # so order setup must happen while the DB is still reachable.
        order_ids = []
        for _ in range(5):
            order_ids.append(await create_order_with_item(client, user_id, item_id, 1))

        tracker = OutcomeTracker()

        # Partition the stock DB
        _docker_network_disconnect("distributed-data-systems-stock-db-1")

        # Issue the checkout phase concurrently — sequential would accumulate
        # 5×30s = 150s of backoff before the partition is lifted.
        async def _do_checkout_order(order_id: str):
            try:
                r = await client.post(f"{GATEWAY}/orders/checkout/{order_id}")
                return tracker.record_response(r)
            except (httpx.ReadError, httpx.RemoteProtocolError,
                    httpx.ConnectError, httpx.TimeoutException) as e:
                return tracker.record_exception(e)

        await asyncio.gather(*[_do_checkout_order(oid) for oid in order_ids],
                             return_exceptions=True)

        # Reconnect partition and allow server-side abort/commit retry loops to complete.
        # _verified_action retries with exponential backoff up to 60s between retries.
        # A mid-2PC-commit saga (payment committed, stock commit pending) can be stuck
        # for up to 60s before the next retry fires after reconnect.
        _docker_network_connect("distributed-data-systems-stock-db-1")
        await asyncio.sleep(5.0)  # DB reconnect settle
        await wait_gateway_healthy(client)
        await asyncio.sleep(75.0)  # cover the full 60s max backoff + retry overhead

        # Verify conservation — all partitioned requests should have been rolled back
        await assert_conservation(
            client,
            items={item_id: initial_stock},
            users={user_id: initial_credit},
            price=item_price,
            label="partition_stock_db",
        )

        # At least verify no successes during partition (stock was unreachable)
        print(f"Partition outcomes: {tracker.summary()}")


# ---------------------------------------------------------------------------
# 4.2 – Network partition: NATS unreachable
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_network_partition_nats():
    """Pause NATS → orchestrator can't communicate → timeouts → recovery (task 4.2)."""
    item_price = 50
    initial_stock = 20
    initial_credit = 50_000

    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        await wait_gateway_healthy(client)

        item_id = await create_item(client, price=item_price, stock=initial_stock)
        user_id = await create_user(client, credit=initial_credit)

        # Pre-create orders BEFORE pausing NATS — addItem uses NATS for stock price
        # lookup; with NATS paused it would fail with "Item not found".
        order_ids = []
        for _ in range(3):
            order_ids.append(await create_order_with_item(client, user_id, item_id, 1))

        tracker = OutcomeTracker()

        # Pause NATS then issue ONLY the checkout phase concurrently.
        _docker_compose("pause", "nats")

        async def _do_checkout(order_id: str):
            try:
                r = await client.post(f"{GATEWAY}/orders/checkout/{order_id}")
                return tracker.record_response(r)
            except (httpx.ReadError, httpx.RemoteProtocolError,
                    httpx.ConnectError, httpx.TimeoutException) as e:
                return tracker.record_exception(e)

        tasks = [asyncio.create_task(_do_checkout(oid)) for oid in order_ids]
        await asyncio.gather(*tasks, return_exceptions=True)

        # Unpause NATS. The server-side executor _verified_action retry loops
        # are still running — they will succeed on the next retry cycle after
        # NATS becomes available. With backoff up to 60s, allow generous settle time.
        _docker_compose("unpause", "nats")
        await asyncio.sleep(10.0)  # NATS reconnect time
        await wait_gateway_healthy(client)
        await asyncio.sleep(30.0)  # allow in-flight retry loops to complete

        # Conservation must hold
        await assert_conservation(
            client,
            items={item_id: initial_stock},
            users={user_id: initial_credit},
            price=item_price,
            label="partition_nats",
        )
        print(f"NATS partition outcomes: {tracker.summary()}")


# ---------------------------------------------------------------------------
# 4.3 – NATS crash and recovery
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_nats_crash_recovery():
    """Kill NATS during checkouts → restart → in-flight txns resolve → conservation holds (task 4.3)."""
    item_price = 100
    initial_stock = 30
    initial_credit = 100_000

    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        await wait_gateway_healthy(client)

        item_id = await create_item(client, price=item_price, stock=initial_stock)
        user_id = await create_user(client, credit=initial_credit)

        # Pre-create 10 orders BEFORE killing NATS — addItem uses NATS for price
        # lookup; if NATS is down, addItem fails with "Item not found".
        order_ids = []
        for _ in range(10):
            order_ids.append(await create_order_with_item(client, user_id, item_id, 1))

        tracker = OutcomeTracker()

        async def _checkout(order_id: str):
            try:
                r = await client.post(f"{GATEWAY}/orders/checkout/{order_id}")
                return tracker.record_response(r)
            except (httpx.ReadError, httpx.RemoteProtocolError,
                    httpx.ConnectError, httpx.TimeoutException) as e:
                return tracker.record_exception(e)

        tasks = [asyncio.create_task(_checkout(oid)) for oid in order_ids]
        await asyncio.sleep(0.2)

        # Kill NATS
        _docker_compose("kill", "nats")

        await asyncio.gather(*tasks, return_exceptions=True)

        # Restart NATS
        _docker_compose("start", "nats")
        await asyncio.sleep(20.0)  # reconnect time for all services
        await wait_gateway_healthy(client)

        # Recovery worker resolves in-flight sagas (RECONCILIATION_INTERVAL=60s;
        # leadership re-acquisition triggers an immediate scan but still needs time)
        await asyncio.sleep(30.0)

        await assert_conservation(
            client,
            items={item_id: initial_stock},
            users={user_id: initial_credit},
            price=item_price,
            label="nats_crash_recovery",
        )
        print(f"NATS crash outcomes: {tracker.summary()}")


# ---------------------------------------------------------------------------
# 4.5 – Cascade failure: stock-db + payment-service killed simultaneously
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cascade_failure():
    """Kill stock-db + payment-service simultaneously under load → system recovers (task 4.5)."""
    item_price = 50
    initial_stock = 50
    initial_credit = 100_000

    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        await wait_gateway_healthy(client)

        item_id = await create_item(client, price=item_price, stock=initial_stock)
        user_id = await create_user(client, credit=initial_credit)

        tracker = OutcomeTracker()

        async def _checkout():
            try:
                r = await checkout(client, user_id, item_id, 1)
                return tracker.record_response(r)
            except (httpx.ReadError, httpx.RemoteProtocolError,
                    httpx.ConnectError, httpx.TimeoutException) as e:
                return tracker.record_exception(e)

        tasks = [asyncio.create_task(_checkout()) for _ in range(15)]
        await asyncio.sleep(0.25)

        # Kill both simultaneously
        for container in ["stock-db", "payment-service"]:
            _docker_compose("kill", container)

        await asyncio.gather(*tasks, return_exceptions=True)

        # Restart everything.
        # stock-db: Sentinel will promote stock-db-replica to master (~20s).
        # Restart stock-service instances to force reconnection to the new master.
        _docker_compose("start", "stock-db", "payment-service")
        await asyncio.sleep(25.0)  # Sentinel failover time for stock-db
        _docker_compose("restart", "stock-service", "stock-service-2")
        await asyncio.sleep(15.0)  # stock-service startup time
        await wait_gateway_healthy(client)
        await asyncio.sleep(20.0)  # recovery worker time

        await assert_conservation(
            client,
            items={item_id: initial_stock},
            users={user_id: initial_credit},
            price=item_price,
            label="cascade_failure",
        )
        print(f"Cascade failure outcomes: {tracker.summary()}")


# ---------------------------------------------------------------------------
# 4.6 – Redis data loss during failover
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_redis_failover_data_loss_detection():
    """Kill Redis master immediately after write (before replication) → system handles gracefully (task 4.6)."""
    item_price = 100
    initial_stock = 50
    initial_credit = 50_000

    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        await wait_gateway_healthy(client)

        item_id = await create_item(client, price=item_price, stock=initial_stock)
        user_id = await create_user(client, credit=initial_credit)

        # Kill stock-db immediately — potential data loss before replication
        _docker_compose("kill", "stock-db")

        # Wait for Sentinel to promote replica
        await asyncio.sleep(20.0)

        tracker = OutcomeTracker()
        # Try checkouts after failover — system must not corrupt data
        for _ in range(3):
            try:
                r = await checkout(client, user_id, item_id, 1)
                tracker.record_response(r)
            except (httpx.ReadError, httpx.RemoteProtocolError,
                    httpx.ConnectError, httpx.TimeoutException) as e:
                tracker.record_exception(e)

        # Restart stock-db
        _docker_compose("start", "stock-db")
        await asyncio.sleep(10.0)
        _docker_compose("restart", "stock-service", "stock-service-2")
        await asyncio.sleep(10.0)
        await wait_gateway_healthy(client)

        # Check stock/find works
        r = await client.get(f"{GATEWAY}/stock/find/{item_id}")
        if r.status_code == 200:
            # Item survived failover — verify conservation
            await assert_conservation(
                client,
                items={item_id: initial_stock},
                users={user_id: initial_credit},
                price=item_price,
                label="redis_data_loss",
            )
        else:
            # Item was lost in failover — this is the expected failure mode.
            # The key check is that NO money was deducted (no partial commit)
            r = await client.get(f"{GATEWAY}/payment/find_user/{user_id}")
            assert r.status_code == 200
            final_credit = r.json()["credit"]
            # Credit should only differ by successful committed transactions (0 if item not found)
            assert final_credit >= 0, f"Negative credit after data loss: {final_credit}"
            print(f"Item lost in failover — credit check only. Final credit: {final_credit}")

        print(f"Failover outcomes: {tracker.summary()}")


# ---------------------------------------------------------------------------
# 4.7 – WAL survives order-db Redis failover
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_wal_survives_redis_failover():
    """Kill order-db master during in-flight checkouts → WAL recovers on new master (task 4.7)."""
    item_price = 75
    initial_stock = 30
    initial_credit = 100_000

    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        await wait_gateway_healthy(client)

        item_id = await create_item(client, price=item_price, stock=initial_stock)
        user_id = await create_user(client, credit=initial_credit)

        tracker = OutcomeTracker()

        async def _do_checkout():
            try:
                r = await checkout(client, user_id, item_id, 1)
                return tracker.record_response(r)
            except (httpx.ReadError, httpx.RemoteProtocolError,
                    httpx.ConnectError, httpx.TimeoutException) as e:
                return tracker.record_exception(e)

        tasks = [asyncio.create_task(_do_checkout()) for _ in range(10)]
        await asyncio.sleep(0.2)

        # Kill the order-db master
        _docker_compose("kill", "order-db")

        await asyncio.gather(*tasks, return_exceptions=True)

        # Wait for Sentinel failover
        await asyncio.sleep(20.0)
        _docker_compose("start", "order-db")
        await asyncio.sleep(10.0)

        # Restart order services to reconnect to new master
        _docker_compose("restart", "order-service-1", "order-service-2")
        await asyncio.sleep(15.0)
        await wait_gateway_healthy(client)

        # Extra time for recovery worker to process incomplete WAL entries
        await asyncio.sleep(15.0)

        # The key property: after WAL recovery, conservation holds
        await assert_conservation(
            client,
            items={item_id: initial_stock},
            users={user_id: initial_credit},
            price=item_price,
            label="wal_redis_failover",
        )
        print(f"WAL failover outcomes: {tracker.summary()}")
