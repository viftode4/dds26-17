import asyncio
import json
import logging
import os
import random
import uuid
from collections import defaultdict
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import redis.asyncio as aioredis

logging.getLogger("httpx").setLevel(logging.WARNING)
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.routing import Route

from common.config import create_redis_connection, create_replica_connection, wait_for_redis, subscribe_failover_invalidation
from common.logging import setup_logging, get_logger


DB_ERROR_STR = "DB error"
GATEWAY_URL = os.environ.get("GATEWAY_URL", "http://gateway:80")


log = get_logger("order")

db: aioredis.Redis | None = None
db_read: aioredis.Redis | None = None
_http_client: httpx.AsyncClient | None = None


@asynccontextmanager
async def lifespan(app):
    global db, db_read, _http_client

    setup_logging("order-service")

    db = create_redis_connection(prefix="", decode_responses=True)
    await wait_for_redis(db, "order-db")

    db_read = create_replica_connection(prefix="", decode_responses=True)

    lua_path = Path(__file__).parent / "lua" / "order_lib.lua"
    lua_code = lua_path.read_text()
    try:
        await db.function_load(lua_code, replace=True)
    except aioredis.RedisError as e:
        log.error("Failed to load Lua library", error=str(e))
        raise

    await asyncio.gather(*[db.ping() for _ in range(32)])

    _http_client = httpx.AsyncClient(base_url=GATEWAY_URL, timeout=5.0)

    failover_task = await subscribe_failover_invalidation(
        db, db_read, service_name="order")
    log.info("Order service started (active-active mode)")

    yield

    if failover_task:
        failover_task.cancel()
    if _http_client:
        await _http_client.aclose()
    if db_read:
        await db_read.aclose()
    if db:
        await db.aclose()


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------

async def create_order(request: Request):
    user_id = request.path_params["user_id"]
    key = str(uuid.uuid4())
    try:
        await db.hset(f"order:{key}", mapping={
            "user_id": user_id,
            "paid": "false",
            "total_cost": 0,
        })
    except aioredis.RedisError:
        raise HTTPException(400, detail=DB_ERROR_STR)
    return JSONResponse({"order_id": key})


async def batch_init_users(request: Request):
    n = int(request.path_params["n"])
    n_items = int(request.path_params["n_items"])
    n_users = int(request.path_params["n_users"])
    item_price = int(request.path_params["item_price"])
    try:
        async with db.pipeline(transaction=False) as pipe:
            for i in range(n):
                user_id = random.randint(0, n_users - 1)
                item1_id = random.randint(0, n_items - 1)
                item2_id = random.randint(0, n_items - 1)
                pipe.hset(f"order:{i}", mapping={
                    "user_id": str(user_id),
                    "paid": "false",
                    "total_cost": 2 * item_price,
                })
                pipe.rpush(f"order:{i}:items", f"{item1_id}:1", f"{item2_id}:1")
            await pipe.execute()
    except aioredis.RedisError:
        raise HTTPException(400, detail=DB_ERROR_STR)
    return JSONResponse({"msg": "Batch init for orders successful"})


async def find_order(request: Request):
    order_id = request.path_params["order_id"]
    conn = db_read or db
    try:
        async with conn.pipeline(transaction=False) as pipe:
            pipe.hgetall(f"order:{order_id}")
            pipe.lrange(f"order:{order_id}:items", 0, -1)
            entry, raw_items = await pipe.execute()
    except Exception:
        try:
            async with db.pipeline(transaction=False) as pipe:
                pipe.hgetall(f"order:{order_id}")
                pipe.lrange(f"order:{order_id}:items", 0, -1)
                entry, raw_items = await pipe.execute()
        except aioredis.RedisError:
            raise HTTPException(400, detail=DB_ERROR_STR)
    if not entry:
        raise HTTPException(400, detail=f"Order: {order_id} not found!")
    items = []
    for raw in raw_items:
        item_id, _quantity = raw.split(":", 1)
        items.append(item_id)
    return JSONResponse({
        "order_id": order_id,
        "paid": entry["paid"] == "true",
        "items": items,
        "user_id": entry["user_id"],
        "total_cost": int(entry["total_cost"]),
    })


async def add_item(request: Request):
    order_id = request.path_params["order_id"]
    item_id = request.path_params["item_id"]
    quantity = int(request.path_params["quantity"])
    if quantity <= 0:
        raise HTTPException(400, detail="Quantity must be positive")
    try:
        resp = await _http_client.get(f"/stock/find/{item_id}")
    except httpx.HTTPError:
        raise HTTPException(400, detail=DB_ERROR_STR)
    if resp.status_code != 200:
        raise HTTPException(400, detail=f"Item: {item_id} does not exist!")
    item_data = resp.json()
    cost_increase = quantity * int(item_data["price"])

    items_key = f"order:{order_id}:items"
    order_key = f"order:{order_id}"
    try:
        await db.fcall("order_add_item", 2, items_key, order_key,
                       f"{item_id}:{quantity}", cost_increase)
    except aioredis.ResponseError as e:
        if "ORDER_NOT_FOUND" in str(e):
            raise HTTPException(400, detail=f"Order: {order_id} not found!")
        raise HTTPException(400, detail=DB_ERROR_STR)
    except aioredis.RedisError:
        raise HTTPException(400, detail=DB_ERROR_STR)

    return PlainTextResponse(
        f"Item: {item_id} added to: {order_id} price updated to: {cost_increase}",
    )


def _parse_order_fcall(raw):
    found, entry_flat_json, items_json, acquired_int = raw[0], raw[1], raw[2], raw[3]
    if not found:
        return None, None, None, False
    entry_flat = json.loads(entry_flat_json)
    entry = {}
    for i in range(0, len(entry_flat), 2):
        entry[entry_flat[i]] = entry_flat[i + 1]
    raw_items = json.loads(items_json)
    items = []
    for raw_item in raw_items:
        item_id, quantity = raw_item.split(":", 1)
        items.append((item_id, int(quantity)))
    return entry, items, acquired_int == 1


async def internal_checkout_begin(request: Request):
    """Coordinator-only: atomically load order + claim idempotency key."""
    order_id = request.path_params["order_id"]
    try:
        body = await request.json()
        saga_id = body.get("saga_id", "")
    except Exception:
        raise HTTPException(400, detail="Invalid JSON body")
    if not saga_id:
        raise HTTPException(400, detail="saga_id required")

    idempotency_key = f"idempotency:checkout:{order_id}"
    claim_value = json.dumps({"status": "processing", "saga_id": saga_id})
    keys = [f"order:{order_id}", f"order:{order_id}:items", idempotency_key]
    try:
        raw = await db.fcall("order_load_and_claim", len(keys), *keys, claim_value, "120")
    except aioredis.RedisError:
        raise HTTPException(400, detail=DB_ERROR_STR)

    parsed = _parse_order_fcall(raw)
    if parsed[0] is None:
        raise HTTPException(404, detail="Order not found")
    entry, items, acquired = parsed

    if entry["paid"] == "true":
        if acquired:
            await db.delete(idempotency_key)
        return JSONResponse({"already_paid": True})

    if not acquired:
        existing = await db.get(idempotency_key)
        if existing:
            existing_data = json.loads(existing)
            if isinstance(existing_data, dict):
                st = existing_data.get("status")
                if st == "success":
                    return JSONResponse({"duplicate": "success"})
                if st == "failed":
                    return JSONResponse({
                        "duplicate": "failed",
                        "error": existing_data.get("error", "unknown"),
                    })
                if st == "processing":
                    return JSONResponse({
                        "duplicate": "processing",
                        "saga_id": existing_data.get("saga_id", ""),
                    })
        raise HTTPException(409, detail="Checkout already in progress")

    items_quantities: dict[str, int] = defaultdict(int)
    for item_id, quantity in items:
        items_quantities[item_id] += quantity
    aggregated = list(items_quantities.items())

    if not aggregated:
        await db.delete(idempotency_key)
        raise HTTPException(400, detail="Order has no items")

    return JSONResponse({
        "acquired": True,
        "user_id": entry["user_id"],
        "total_cost": int(entry["total_cost"]),
        "items": aggregated,
    })


async def internal_checkout_complete(request: Request):
    order_id = request.path_params["order_id"]
    try:
        body = await request.json()
        saga_id = body.get("saga_id", "")
    except Exception:
        raise HTTPException(400, detail="Invalid JSON body")
    if not saga_id:
        raise HTTPException(400, detail="saga_id required")

    idempotency_key = f"idempotency:checkout:{order_id}"
    success_json = json.dumps({"status": "success", "saga_id": saga_id})
    keys = [f"order:{order_id}", idempotency_key]
    try:
        raw = await db.fcall("order_complete_checkout", len(keys), *keys, saga_id, success_json)
    except aioredis.RedisError:
        raise HTTPException(400, detail=DB_ERROR_STR)
    ok, reason = raw[0] == 1, raw[1]
    if not ok and reason == "mismatch":
        raise HTTPException(409, detail="Idempotency saga mismatch")
    if not ok and reason == "no_key":
        raise HTTPException(404, detail="No checkout claim for order")
    return JSONResponse({"ok": True, "reason": reason})


async def internal_checkout_release(request: Request):
    order_id = request.path_params["order_id"]
    try:
        body = await request.json()
        saga_id = body.get("saga_id", "")
    except Exception:
        raise HTTPException(400, detail="Invalid JSON body")
    if not saga_id:
        raise HTTPException(400, detail="saga_id required")

    idempotency_key = f"idempotency:checkout:{order_id}"
    try:
        raw = await db.fcall("order_release_checkout", 1, idempotency_key, saga_id)
    except aioredis.RedisError:
        raise HTTPException(400, detail=DB_ERROR_STR)
    ok = raw[0] == 1
    return JSONResponse({"ok": ok, "reason": raw[1]})


async def health(request: Request):
    try:
        await db.ping()
    except Exception:
        raise HTTPException(503, detail="Redis unavailable")
    return JSONResponse({"status": "healthy"})


async def metrics(request: Request):
    """Minimal metrics — saga coordination lives on checkout-service."""
    lines = [
        "# HELP order_up Order service is up",
        "# TYPE order_up gauge",
        "order_up 1",
        "",
    ]
    return Response("\n".join(lines), status_code=200, media_type="text/plain")


async def http_exception_handler(request, exc):
    return JSONResponse({"error": exc.detail}, status_code=exc.status_code)


routes = [
    Route("/create/{user_id}", create_order, methods=["POST"]),
    Route("/batch_init/{n}/{n_items}/{n_users}/{item_price}", batch_init_users, methods=["POST"]),
    Route("/find/{order_id}", find_order, methods=["GET"]),
    Route("/addItem/{order_id}/{item_id}/{quantity}", add_item, methods=["POST"]),
    Route("/internal/checkout/begin/{order_id}", internal_checkout_begin, methods=["POST"]),
    Route("/internal/checkout/complete/{order_id}", internal_checkout_complete, methods=["POST"]),
    Route("/internal/checkout/release/{order_id}", internal_checkout_release, methods=["POST"]),
    Route("/health", health, methods=["GET"]),
    Route("/metrics", metrics, methods=["GET"]),
]

app = Starlette(
    routes=routes,
    lifespan=lifespan,
    exception_handlers={HTTPException: http_exception_handler},
)
