import asyncio
import json
import os
import socket
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import redis.asyncio as aioredis
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route

from common.config import create_redis_connection, create_replica_connection, wait_for_redis
from common.consumer import consumer_loop, dlq_sweep_loop
from common.logging import setup_logging, get_logger


DB_ERROR_STR = "DB error"
STREAM_COMMANDS = "stock-commands"
STREAM_OUTBOX = "stock-outbox"
CONSUMER_GROUP = "stock-workers"
CONSUMER_NAME = f"worker-{socket.gethostname()}-{os.getpid()}"
RESERVATION_TTL = 60  # 1 minute
DLQ_STREAM = f"dead-letter:{STREAM_COMMANDS}"

log = get_logger("stock")

db: aioredis.Redis | None = None
db_read: aioredis.Redis | None = None
_consumer_task: asyncio.Task | None = None


@asynccontextmanager
async def lifespan(app):
    global db, db_read, _consumer_task
    setup_logging("stock-service")

    db = create_redis_connection(prefix="", decode_responses=True)
    await wait_for_redis(db, "stock-db")

    # Prewarm connection pool
    await asyncio.gather(*[db.ping() for _ in range(8)])

    # Replica connection for read-only find/list endpoints
    db_read = create_replica_connection(prefix="", decode_responses=True)

    # Load Lua function library
    lua_path = Path(__file__).parent / "lua" / "stock_lib.lua"
    lua_code = lua_path.read_text()
    try:
        await db.function_load(lua_code, replace=True)
    except aioredis.RedisError as e:
        log.error("Failed to load Lua library", error=str(e))
        raise

    # Create consumer group (idempotent — safe for multiple instances)
    try:
        await db.xgroup_create(STREAM_COMMANDS, CONSUMER_GROUP, id="0", mkstream=True)
    except aioredis.ResponseError as e:
        if "BUSYGROUP" not in str(e):
            raise

    _consumer_task = asyncio.create_task(
        consumer_loop(db, STREAM_COMMANDS, CONSUMER_GROUP, CONSUMER_NAME, handle_command)
    )
    asyncio.create_task(dlq_sweep_loop(db, STREAM_COMMANDS, CONSUMER_GROUP, DLQ_STREAM))
    log.info("Stock service started", consumer=CONSUMER_NAME)

    yield

    if _consumer_task:
        _consumer_task.cancel()
        try:
            await _consumer_task
        except asyncio.CancelledError:
            pass
    if db_read:
        await db_read.aclose()
    if db:
        await db.aclose()


async def handle_command(fields: dict):
    """Dispatch a command to the appropriate Lua function."""
    action = fields.get("action", "")
    saga_id = fields.get("saga_id", "")

    if action == "try_reserve":
        items_raw = fields.get("items", "")
        if not items_raw:
            log.warning("try_reserve with no items", saga_id=saga_id)
            await db.xadd(STREAM_OUTBOX, {
                "saga_id": saga_id, "event": "failed", "reason": "no_items",
            }, maxlen=10000, approximate=True)
            return
        items = [(str(item_id), int(qty)) for item_id, qty in json.loads(items_raw)]
        ttl = int(fields.get("ttl", RESERVATION_TTL))
        await _try_reserve(saga_id, items, ttl)
    elif action == "confirm":
        items_raw = fields.get("items", "")
        items = [(str(item_id), int(qty)) for item_id, qty in json.loads(items_raw)] if items_raw else []
        await _confirm(saga_id, items)
    elif action == "cancel":
        items_raw = fields.get("items", "")
        items = [(str(item_id), int(qty)) for item_id, qty in json.loads(items_raw)] if items_raw else []
        await _cancel(saga_id, items)
    else:
        log.warning("Unknown action", action=action, saga_id=saga_id)


async def _try_reserve(saga_id: str, items: list[tuple[str, int]], ttl: int = RESERVATION_TTL):
    n = len(items)
    keys = [f"item:{item_id}" for item_id, _ in items]
    keys += [f"reservation:{saga_id}:{item_id}" for item_id, _ in items]
    keys += [f"reservation_amount:{saga_id}:{item_id}" for item_id, _ in items]
    keys += [STREAM_OUTBOX, f"saga:{saga_id}:stock:status"]
    args = [saga_id, str(ttl)]
    for item_id, amount in items:
        args += [item_id, str(amount)]
    await db.fcall("stock_try_reserve_batch", len(keys), *keys, *args)


async def _confirm(saga_id: str, items: list[tuple[str, int]]):
    if not items:
        await db.xadd(STREAM_OUTBOX, {
            "saga_id": saga_id, "event": "confirmed", "reason": "no_items",
        }, maxlen=10000, approximate=True)
        return
    n = len(items)
    keys = [f"item:{item_id}" for item_id, _ in items]
    keys += [f"reservation:{saga_id}:{item_id}" for item_id, _ in items]
    keys += [f"reservation_amount:{saga_id}:{item_id}" for item_id, _ in items]
    keys += [STREAM_OUTBOX, f"saga:{saga_id}:stock:status"]
    await db.fcall("stock_confirm_batch", len(keys), *keys, saga_id, str(n))


async def _cancel(saga_id: str, items: list[tuple[str, int]]):
    if not items:
        await db.xadd(STREAM_OUTBOX, {
            "saga_id": saga_id, "event": "cancelled", "reason": "no_items",
        }, maxlen=10000, approximate=True)
        return
    n = len(items)
    keys = [f"item:{item_id}" for item_id, _ in items]
    keys += [f"reservation:{saga_id}:{item_id}" for item_id, _ in items]
    keys += [f"reservation_amount:{saga_id}:{item_id}" for item_id, _ in items]
    keys += [STREAM_OUTBOX, f"saga:{saga_id}:stock:status"]
    await db.fcall("stock_cancel_batch", len(keys), *keys, saga_id, str(n))


# ============================================================================
# HTTP API endpoints
# ============================================================================

async def create_item(request: Request):
    price = int(request.path_params["price"])
    key = str(uuid.uuid4())
    try:
        await db.hset(f"item:{key}", mapping={
            "available_stock": 0,
            "reserved_stock": 0,
            "price": price,
        })
    except aioredis.RedisError:
        raise HTTPException(400, detail=DB_ERROR_STR)
    return JSONResponse({"item_id": key})


async def batch_init_users(request: Request):
    n = int(request.path_params["n"])
    starting_stock = int(request.path_params["starting_stock"])
    item_price = int(request.path_params["item_price"])
    try:
        async with db.pipeline(transaction=False) as pipe:
            for i in range(n):
                pipe.hset(f"item:{i}", mapping={
                    "available_stock": starting_stock,
                    "reserved_stock": 0,
                    "price": item_price,
                })
            await pipe.execute()
    except aioredis.RedisError:
        raise HTTPException(400, detail=DB_ERROR_STR)
    return JSONResponse({"msg": "Batch init for stock successful"})


async def find_item(request: Request):
    item_id = request.path_params["item_id"]
    conn = db_read or db
    try:
        entry = await conn.hgetall(f"item:{item_id}")
    except Exception:
        try:
            entry = await db.hgetall(f"item:{item_id}")
        except aioredis.RedisError:
            raise HTTPException(400, detail=DB_ERROR_STR)
    if not entry:
        raise HTTPException(400, detail=f"Item: {item_id} not found!")
    available = int(entry["available_stock"])
    reserved = int(entry["reserved_stock"])
    return JSONResponse({
        "stock": available + reserved,
        "price": int(entry["price"]),
    })


async def add_stock(request: Request):
    item_id = request.path_params["item_id"]
    amount = int(request.path_params["amount"])
    key = f"item:{item_id}"
    try:
        new_stock = await db.fcall("stock_add_direct", 1, key, amount)
    except aioredis.ResponseError as e:
        if "ITEM_NOT_FOUND" in str(e):
            raise HTTPException(400, detail=f"Item: {item_id} not found!")
        raise HTTPException(400, detail=DB_ERROR_STR)
    except aioredis.RedisError:
        raise HTTPException(400, detail=DB_ERROR_STR)
    return PlainTextResponse(f"Item: {item_id} stock updated to: {new_stock}")


async def remove_stock(request: Request):
    item_id = request.path_params["item_id"]
    amount = int(request.path_params["amount"])
    key = f"item:{item_id}"
    try:
        new_stock = await db.fcall("stock_subtract_direct", 1, key, amount)
    except aioredis.ResponseError as e:
        err_msg = str(e)
        if "ITEM_NOT_FOUND" in err_msg:
            raise HTTPException(400, detail=f"Item: {item_id} not found!")
        if "INSUFFICIENT_STOCK" in err_msg:
            raise HTTPException(400, detail=f"Item: {item_id} stock cannot get reduced below zero!")
        raise HTTPException(400, detail=DB_ERROR_STR)
    except aioredis.RedisError:
        raise HTTPException(400, detail=DB_ERROR_STR)
    return PlainTextResponse(f"Item: {item_id} stock updated to: {new_stock}")


async def health(request: Request):
    try:
        await db.ping()
    except Exception:
        raise HTTPException(503, detail="Redis unavailable")
    return JSONResponse({"status": "healthy"})


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------

async def http_exception_handler(request, exc):
    return JSONResponse({"error": exc.detail}, status_code=exc.status_code)


routes = [
    Route("/item/create/{price}", create_item, methods=["POST"]),
    Route("/batch_init/{n}/{starting_stock}/{item_price}", batch_init_users, methods=["POST"]),
    Route("/find/{item_id}", find_item, methods=["GET"]),
    Route("/add/{item_id}/{amount}", add_stock, methods=["POST"]),
    Route("/subtract/{item_id}/{amount}", remove_stock, methods=["POST"]),
    Route("/health", health, methods=["GET"]),
]

app = Starlette(
    routes=routes,
    lifespan=lifespan,
    exception_handlers={HTTPException: http_exception_handler},
)
