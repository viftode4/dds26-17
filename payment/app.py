import asyncio
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
STREAM_COMMANDS = "payment-commands"
STREAM_OUTBOX = "payment-outbox"
CONSUMER_GROUP = "payment-workers"
CONSUMER_NAME = f"worker-{socket.gethostname()}-{os.getpid()}"
DLQ_STREAM = f"dead-letter:{STREAM_COMMANDS}"

log = get_logger("payment")

db: aioredis.Redis | None = None
db_read: aioredis.Redis | None = None
_consumer_task: asyncio.Task | None = None
_dlq_task: asyncio.Task | None = None


@asynccontextmanager
async def lifespan(app):
    global db, db_read, _consumer_task, _dlq_task
    setup_logging("payment-service")

    db = create_redis_connection(prefix="", decode_responses=True)
    await wait_for_redis(db, "payment-db")

    # Prewarm connection pool
    await asyncio.gather(*[db.ping() for _ in range(8)])

    # Replica connection for read-only find endpoints
    db_read = create_replica_connection(prefix="", decode_responses=True)

    # Load Lua function library
    lua_path = Path(__file__).parent / "lua" / "payment_lib.lua"
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
    _dlq_task = asyncio.create_task(dlq_sweep_loop(db, STREAM_COMMANDS, CONSUMER_GROUP, DLQ_STREAM))
    log.info("Payment service started", consumer=CONSUMER_NAME)

    yield

    for task in (_consumer_task, _dlq_task):
        if task:
            task.cancel()
            try:
                await task
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
    user_id = fields.get("user_id", "")
    amount = fields.get("amount", "0")

    if action == "prepare":
        ttl = int(fields.get("ttl", "30"))
        await _2pc_prepare(saga_id, user_id, int(amount), ttl)
    elif action == "commit":
        await _2pc_commit(saga_id, user_id)
    elif action == "abort":
        await _2pc_abort(saga_id, user_id)
    elif action == "execute":
        await _saga_execute(saga_id, user_id, int(amount))
    elif action == "compensate":
        await _saga_compensate(saga_id, user_id)
    else:
        log.warning("Unknown action", action=action, saga_id=saga_id)


async def _2pc_prepare(saga_id: str, user_id: str, amount: int, ttl: int = 30):
    keys = [
        f"user:{user_id}",
        f"lock:2pc:{saga_id}:{user_id}",
        f"saga:{saga_id}:payment:status",
        STREAM_OUTBOX,
    ]
    args = [str(amount), saga_id, user_id, str(ttl)]
    await db.fcall("payment_2pc_prepare", len(keys), *keys, *args)


async def _2pc_commit(saga_id: str, user_id: str):
    keys = [
        f"user:{user_id}",
        f"lock:2pc:{saga_id}:{user_id}",
        f"saga:{saga_id}:payment:status",
        STREAM_OUTBOX,
    ]
    await db.fcall("payment_2pc_commit", len(keys), *keys, saga_id)


async def _2pc_abort(saga_id: str, user_id: str):
    if not user_id:
        await db.xadd(STREAM_OUTBOX, {
            "saga_id": saga_id, "event": "aborted", "reason": "no_user",
        }, maxlen=10000, approximate=True)
        return
    keys = [
        f"user:{user_id}",
        f"lock:2pc:{saga_id}:{user_id}",
        f"saga:{saga_id}:payment:status",
        STREAM_OUTBOX,
    ]
    await db.fcall("payment_2pc_abort", len(keys), *keys, saga_id)


async def _saga_execute(saga_id: str, user_id: str, amount: int):
    keys = [
        f"user:{user_id}",
        STREAM_OUTBOX,
        f"saga:{saga_id}:payment:status",
        f"saga:{saga_id}:payment:amounts",
    ]
    args = [str(amount), saga_id, user_id]
    await db.fcall("payment_saga_execute", len(keys), *keys, *args)


async def _saga_compensate(saga_id: str, user_id: str):
    if not user_id:
        await db.xadd(STREAM_OUTBOX, {
            "saga_id": saga_id, "event": "compensated", "reason": "no_user",
        }, maxlen=10000, approximate=True)
        return
    keys = [
        f"user:{user_id}",
        STREAM_OUTBOX,
        f"saga:{saga_id}:payment:status",
        f"saga:{saga_id}:payment:amounts",
    ]
    await db.fcall("payment_saga_compensate", len(keys), *keys, saga_id)


# ============================================================================
# HTTP API endpoints
# ============================================================================

async def create_user(request: Request):
    key = str(uuid.uuid4())
    try:
        await db.hset(f"user:{key}", mapping={
            "available_credit": 0,
            "held_credit": 0,
        })
    except aioredis.RedisError:
        raise HTTPException(400, detail=DB_ERROR_STR)
    return JSONResponse({"user_id": key})


async def batch_init_users(request: Request):
    n = int(request.path_params["n"])
    starting_money = int(request.path_params["starting_money"])
    try:
        async with db.pipeline(transaction=False) as pipe:
            for i in range(n):
                pipe.hset(f"user:{i}", mapping={
                    "available_credit": starting_money,
                    "held_credit": 0,
                })
            await pipe.execute()
    except aioredis.RedisError:
        raise HTTPException(400, detail=DB_ERROR_STR)
    return JSONResponse({"msg": "Batch init for users successful"})


async def find_user(request: Request):
    user_id = request.path_params["user_id"]
    conn = db_read or db
    try:
        entry = await conn.hgetall(f"user:{user_id}")
    except Exception:
        try:
            entry = await db.hgetall(f"user:{user_id}")
        except aioredis.RedisError:
            raise HTTPException(400, detail=DB_ERROR_STR)
    if not entry:
        raise HTTPException(400, detail=f"User: {user_id} not found!")
    available = int(entry["available_credit"])
    held = int(entry["held_credit"])
    return JSONResponse({
        "user_id": user_id,
        "credit": available + held,
    })


async def add_credit(request: Request):
    user_id = request.path_params["user_id"]
    amount = int(request.path_params["amount"])
    key = f"user:{user_id}"
    try:
        await db.fcall("payment_add_direct", 1, key, amount)
    except aioredis.ResponseError as e:
        if "USER_NOT_FOUND" in str(e):
            raise HTTPException(400, detail=f"User: {user_id} not found!")
        raise HTTPException(400, detail=DB_ERROR_STR)
    except aioredis.RedisError:
        raise HTTPException(400, detail=DB_ERROR_STR)
    return JSONResponse({"done": True})


async def remove_credit(request: Request):
    user_id = request.path_params["user_id"]
    amount = int(request.path_params["amount"])
    key = f"user:{user_id}"
    try:
        new_credit = await db.fcall("payment_subtract_direct", 1, key, amount)
    except aioredis.ResponseError as e:
        err_msg = str(e)
        if "USER_NOT_FOUND" in err_msg:
            raise HTTPException(400, detail=f"User: {user_id} not found!")
        if "INSUFFICIENT_CREDIT" in err_msg:
            raise HTTPException(400, detail=f"User: {user_id} credit cannot get reduced below zero!")
        raise HTTPException(400, detail=DB_ERROR_STR)
    except aioredis.RedisError:
        raise HTTPException(400, detail=DB_ERROR_STR)
    return PlainTextResponse(f"User: {user_id} credit updated to: {new_credit}")


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
    Route("/create_user", create_user, methods=["POST"]),
    Route("/batch_init/{n}/{starting_money}", batch_init_users, methods=["POST"]),
    Route("/find_user/{user_id}", find_user, methods=["GET"]),
    Route("/add_funds/{user_id}/{amount}", add_credit, methods=["POST"]),
    Route("/pay/{user_id}/{amount}", remove_credit, methods=["POST"]),
    Route("/health", health, methods=["GET"]),
]

app = Starlette(
    routes=routes,
    lifespan=lifespan,
    exception_handlers={HTTPException: http_exception_handler},
)
