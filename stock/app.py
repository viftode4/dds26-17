import asyncio
import json
import os
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
from common.nats_transport import NatsTransport
from common.logging import setup_logging, get_logger


DB_ERROR_STR = "DB error"

log = get_logger("stock")

db: aioredis.Redis | None = None
db_read: aioredis.Redis | None = None
_nats_transport: NatsTransport | None = None


@asynccontextmanager
async def lifespan(app):
    global db, db_read, _nats_transport
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

    _nats_transport = NatsTransport(os.environ.get("NATS_URL", "nats://nats:4222"))
    await _nats_transport.connect()
    await _nats_transport.subscribe("svc.stock.*", "stock-workers", handle_nats_message)
    log.info("Stock service started")

    yield

    if _nats_transport:
        await _nats_transport.close()
    if db_read:
        await db_read.aclose()
    if db:
        await db.aclose()


async def handle_command(fields: dict) -> str:
    """Dispatch a command to the appropriate Lua function."""
    action = fields.get("action", "")
    saga_id = fields.get("saga_id", "")

    if action == "prepare":
        items = _parse_items(fields)
        ttl = int(fields.get("ttl", "30"))
        result = await _2pc_prepare(saga_id, items, ttl)
        return "prepared" if result == 1 else "failed"
    elif action == "commit":
        items = _parse_items(fields)
        await _2pc_commit(saga_id, items)
        return "committed"
    elif action == "abort":
        items = _parse_items(fields)
        await _2pc_abort(saga_id, items)
        return "aborted"
    elif action == "execute":
        items = _parse_items(fields)
        result = await _saga_execute(saga_id, items)
        return "executed" if result == 1 else "failed"
    elif action == "compensate":
        items = _parse_items(fields)
        await _saga_compensate(saga_id, items)
        return "compensated"
    else:
        log.warning("Unknown action", action=action, saga_id=saga_id)
        return "failed"


async def handle_nats_message(msg):
    fields = json.loads(msg.data.decode())
    saga_id = fields.get("saga_id", "")
    try:
        event = await handle_command(fields)
        if event == "failed":
            response = {"saga_id": saga_id, "event": "failed", "reason": "insufficient_stock"}
        else:
            response = {"saga_id": saga_id, "event": event}
    except Exception as e:
        log.error("NATS handler error", error=str(e), saga_id=saga_id)
        response = {"saga_id": saga_id, "event": "failed", "reason": str(e)}
    await msg.respond(json.dumps(response).encode())


def _parse_items(fields: dict) -> list[tuple[str, int]]:
    items_raw = fields.get("items", "")
    if not items_raw:
        return []
    return [(str(item_id), int(qty)) for item_id, qty in json.loads(items_raw)]


async def _2pc_prepare(saga_id: str, items: list[tuple[str, int]], ttl: int = 30) -> int:
    keys = [f"item:{item_id}" for item_id, _ in items]
    keys += [f"lock:2pc:{saga_id}:{item_id}" for item_id, _ in items]
    keys += [f"saga:{saga_id}:stock:status"]
    args = [saga_id, str(ttl)]
    for item_id, amount in items:
        args += [item_id, str(amount)]
    return await db.fcall("stock_2pc_prepare", len(keys), *keys, *args)


async def _2pc_commit(saga_id: str, items: list[tuple[str, int]]):
    n = len(items)
    keys = [f"item:{item_id}" for item_id, _ in items]
    keys += [f"lock:2pc:{saga_id}:{item_id}" for item_id, _ in items]
    keys += [f"saga:{saga_id}:stock:status"]
    await db.fcall("stock_2pc_commit", len(keys), *keys, saga_id, str(n))


async def _2pc_abort(saga_id: str, items: list[tuple[str, int]]):
    n = len(items)
    keys = [f"item:{item_id}" for item_id, _ in items]
    keys += [f"lock:2pc:{saga_id}:{item_id}" for item_id, _ in items]
    keys += [f"saga:{saga_id}:stock:status"]
    args = [saga_id, str(n)]
    for item_id, _ in items:
        args.append(item_id)
    await db.fcall("stock_2pc_abort", len(keys), *keys, *args)


async def _saga_execute(saga_id: str, items: list[tuple[str, int]]) -> int:
    keys = [f"item:{item_id}" for item_id, _ in items]
    keys += [f"saga:{saga_id}:stock:status", f"saga:{saga_id}:stock:amounts"]
    args = [saga_id]
    for item_id, amount in items:
        args += [item_id, str(amount)]
    return await db.fcall("stock_saga_execute", len(keys), *keys, *args)


async def _saga_compensate(saga_id: str, items: list[tuple[str, int]]):
    n = len(items)
    keys = [f"item:{item_id}" for item_id, _ in items]
    keys += [f"saga:{saga_id}:stock:status", f"saga:{saga_id}:stock:amounts"]
    await db.fcall("stock_saga_compensate", len(keys), *keys, saga_id, str(n))


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
