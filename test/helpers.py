"""Shared test helpers for integration tests.

Provides:
- assert_conservation()    — verify credit_spent == stock_sold_cost
- wait_until()             — polling replacement for asyncio.sleep
- RequestOutcome           — categorised request result for diagnostics
- GATEWAY                  — configurable gateway URL
"""
from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from typing import Literal

import httpx

# ---------------------------------------------------------------------------
# Gateway URL — configurable via env var
# ---------------------------------------------------------------------------

GATEWAY: str = os.environ.get("GATEWAY_URL", "http://127.0.0.1:8000")
TIMEOUT = httpx.Timeout(30.0, connect=10.0)
LIMITS = httpx.Limits(max_connections=50, max_keepalive_connections=20)


# ---------------------------------------------------------------------------
# 0.2 – Polling helper
# ---------------------------------------------------------------------------

async def wait_until(
    predicate,
    timeout: float = 30.0,
    interval: float = 0.5,
    msg: str = "",
) -> None:
    """Poll predicate() until it returns True or timeout expires.

    predicate can be a sync callable → bool, or an async callable → bool.
    Raises TimeoutError on expiry.
    """
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        result = predicate()
        if asyncio.iscoroutine(result):
            result = await result
        if result:
            return
        await asyncio.sleep(interval)
    raise TimeoutError(msg or f"Condition not met within {timeout}s")


# ---------------------------------------------------------------------------
# 0.3 – Request outcome tracker
# ---------------------------------------------------------------------------

OutcomeStatus = Literal["success", "client_error", "server_error", "connection_error"]


@dataclass
class RequestOutcome:
    """Categorised result of a single HTTP request."""

    status: OutcomeStatus
    code: int | None = None
    detail: str = ""

    def __repr__(self) -> str:
        return f"[{self.status} HTTP={self.code} '{self.detail[:60]}']"

    @classmethod
    def from_response(cls, r: httpx.Response) -> "RequestOutcome":
        if r.status_code < 400:
            return cls("success", r.status_code, r.text[:200])
        if r.status_code < 500:
            return cls("client_error", r.status_code, r.text[:200])
        return cls("server_error", r.status_code, r.text[:200])

    @classmethod
    def connection_error(cls, exc: Exception) -> "RequestOutcome":
        return cls("connection_error", None, str(exc)[:200])


@dataclass
class OutcomeTracker:
    """Collect and summarise RequestOutcomes for a batch of requests."""

    outcomes: list[RequestOutcome] = field(default_factory=list)

    def record_response(self, r: httpx.Response) -> RequestOutcome:
        o = RequestOutcome.from_response(r)
        self.outcomes.append(o)
        return o

    def record_exception(self, exc: Exception) -> RequestOutcome:
        o = RequestOutcome.connection_error(exc)
        self.outcomes.append(o)
        return o

    @property
    def successes(self) -> int:
        return sum(1 for o in self.outcomes if o.status == "success")

    @property
    def client_errors(self) -> int:
        return sum(1 for o in self.outcomes if o.status == "client_error")

    @property
    def server_errors(self) -> int:
        return sum(1 for o in self.outcomes if o.status == "server_error")

    @property
    def connection_errors(self) -> int:
        return sum(1 for o in self.outcomes if o.status == "connection_error")

    def summary(self) -> str:
        return (
            f"total={len(self.outcomes)} "
            f"success={self.successes} "
            f"client_err={self.client_errors} "
            f"server_err={self.server_errors} "
            f"conn_err={self.connection_errors}"
        )


# ---------------------------------------------------------------------------
# 0.1 – Conservation assertion helper
# ---------------------------------------------------------------------------

async def assert_conservation(
    client: httpx.AsyncClient,
    items: dict[str, int],
    users: dict[str, int],
    price: int,
    *,
    label: str = "",
) -> None:
    """Verify the global conservation equation and non-negative invariants.

    Args:
        client:   httpx.AsyncClient pointed at the gateway.
        items:    Mapping of item_id → initial_stock.
        users:    Mapping of user_id → initial_credit.
        price:    Price per unit (assumes homogeneous pricing for multi-item
                  setups where all items have the same price).
        label:    Optional label for assertion messages.

    Asserts:
        • Σ credit_spent == Σ stock_sold × price
        • All current stock  ≥ 0
        • All current credit ≥ 0
    """
    pfx = f"[{label}] " if label else ""

    total_credit_spent = 0
    for user_id, initial_credit in users.items():
        r = await client.get(f"{GATEWAY}/payment/find_user/{user_id}")
        assert r.status_code == 200, f"{pfx}find_user {user_id} failed: {r.text}"
        current_credit = r.json()["credit"]
        assert current_credit >= 0, f"{pfx}Negative credit for user {user_id}: {current_credit}"
        total_credit_spent += initial_credit - current_credit

    total_stock_value_sold = 0
    for item_id, initial_stock in items.items():
        r = await client.get(f"{GATEWAY}/stock/find/{item_id}")
        assert r.status_code == 200, f"{pfx}find_item {item_id} failed: {r.text}"
        current_stock = r.json()["stock"]
        assert current_stock >= 0, f"{pfx}Negative stock for item {item_id}: {current_stock}"
        total_stock_value_sold += (initial_stock - current_stock) * price

    assert total_credit_spent == total_stock_value_sold, (
        f"{pfx}Conservation violated: "
        f"credit_spent={total_credit_spent} != "
        f"stock_sold_cost={total_stock_value_sold}"
    )


async def assert_conservation_multi(
    client: httpx.AsyncClient,
    items: dict[str, tuple[int, int]],
    users: dict[str, int],
    *,
    label: str = "",
) -> None:
    """Conservation helper for multi-item orders with different prices.

    Args:
        items:  Mapping of item_id → (initial_stock, price_per_unit).
        users:  Mapping of user_id → initial_credit.
    """
    pfx = f"[{label}] " if label else ""

    total_credit_spent = 0
    for user_id, initial_credit in users.items():
        r = await client.get(f"{GATEWAY}/payment/find_user/{user_id}")
        assert r.status_code == 200, f"{pfx}find_user {user_id} failed: {r.text}"
        current_credit = r.json()["credit"]
        assert current_credit >= 0, f"{pfx}Negative credit for user {user_id}: {current_credit}"
        total_credit_spent += initial_credit - current_credit

    total_stock_value_sold = 0
    for item_id, (initial_stock, item_price) in items.items():
        r = await client.get(f"{GATEWAY}/stock/find/{item_id}")
        assert r.status_code == 200, f"{pfx}find_item {item_id} failed: {r.text}"
        current_stock = r.json()["stock"]
        assert current_stock >= 0, f"{pfx}Negative stock for item {item_id}: {current_stock}"
        total_stock_value_sold += (initial_stock - current_stock) * item_price

    assert total_credit_spent == total_stock_value_sold, (
        f"{pfx}Conservation violated: "
        f"credit_spent={total_credit_spent} != "
        f"stock_sold_cost={total_stock_value_sold}"
    )


# ---------------------------------------------------------------------------
# Common setup helpers
# ---------------------------------------------------------------------------

async def create_item(client: httpx.AsyncClient, price: int, stock: int) -> str:
    """Create a stock item, add inventory. Returns item_id."""
    r = await client.post(f"{GATEWAY}/stock/item/create/{price}")
    assert r.status_code == 200, f"item/create failed: {r.text}"
    item_id = r.json()["item_id"]
    r = await client.post(f"{GATEWAY}/stock/add/{item_id}/{stock}")
    assert r.status_code == 200, f"stock/add failed: {r.text}"
    return item_id


async def create_user(client: httpx.AsyncClient, credit: int) -> str:
    """Create a payment user, add credit. Returns user_id."""
    r = await client.post(f"{GATEWAY}/payment/create_user")
    assert r.status_code == 200, f"create_user failed: {r.text}"
    user_id = r.json()["user_id"]
    if credit > 0:
        r = await client.post(f"{GATEWAY}/payment/add_funds/{user_id}/{credit}")
        assert r.status_code == 200, f"add_funds failed: {r.text}"
    return user_id


async def create_order_with_item(
    client: httpx.AsyncClient,
    user_id: str,
    item_id: str,
    quantity: int = 1,
) -> str:
    """Create an order, add one item, return order_id."""
    r = await client.post(f"{GATEWAY}/orders/create/{user_id}")
    assert r.status_code == 200, f"orders/create failed: {r.text}"
    order_id = r.json()["order_id"]
    r = await client.post(f"{GATEWAY}/orders/addItem/{order_id}/{item_id}/{quantity}")
    assert r.status_code == 200, f"addItem failed: {r.text}"
    return order_id


async def checkout(
    client: httpx.AsyncClient,
    user_id: str,
    item_id: str,
    quantity: int = 1,
) -> httpx.Response:
    """Create order + add item + checkout in one call."""
    order_id = await create_order_with_item(client, user_id, item_id, quantity)
    return await client.post(f"{GATEWAY}/orders/checkout/{order_id}")


async def wait_gateway_healthy(
    client: httpx.AsyncClient,
    max_wait: float = 60.0,
) -> None:
    """Poll until all backend services are healthy behind the gateway."""
    endpoints = [
        "/orders/health",
        "/orders/__checkout_health",
        "/stock/health",
        "/payment/health",
    ]

    async def _healthy():
        try:
            for ep in endpoints:
                r = await client.get(f"{GATEWAY}{ep}")
                if r.status_code != 200:
                    print(f"wait_gateway_healthy {ep} status={r.status_code}")
                    return False
            return True
        except Exception as e:
            print(f"wait_gateway_healthy exception: {e!r}")
            return False

    await wait_until(_healthy, timeout=max_wait, interval=1.0,
                     msg=f"Gateway {GATEWAY} did not become healthy within {max_wait}s")
