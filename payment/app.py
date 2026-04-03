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

from common.config import create_redis_connection, create_replica_connection, wait_for_redis, subscribe_failover_invalidation
from common.nats_transport import NatsTransport
from common.logging import setup_logging, get_logger


DB_ERROR_STR = "DB error"

log = get_logger("payment")

db: aioredis.Redis | None = None
db_read: aioredis.Redis | None = None
_nats_transport: NatsTransport | None = None


@asynccontextmanager
async def lifespan(app):
    global db, db_read, _nats_transport
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

    _nats_transport = NatsTransport(os.environ.get("NATS_URL", "nats://nats:4222"))
    await _nats_transport.connect()
    await _nats_transport.subscribe("svc.payment.*", "payment-workers", handle_nats_message)

    failover_task = await subscribe_failover_invalidation(
        db, db_read, service_name="payment")
    log.info("Payment service started")

    yield

    if failover_task:
        failover_task.cancel()
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
    user_id = fields.get("user_id", "")
    amount = fields.get("amount", "0")

    if action == "prepare":
        ttl = int(fields.get("ttl", "30"))
        result = await _2pc_prepare(saga_id, user_id, int(amount), ttl)
        outcome = "prepared" if result == 1 else "failed"
    elif action == "commit":
        await _2pc_commit(saga_id, user_id, int(amount))
        outcome = "committed"
    elif action == "abort":
        await _2pc_abort(saga_id, user_id, int(amount))
        outcome = "aborted"
    elif action == "execute":
        result = await _saga_execute(saga_id, user_id, int(amount))
        outcome = "executed" if result == 1 else "failed"
    elif action == "compensate":
        await _saga_compensate(saga_id, user_id)
        outcome = "compensated"
    else:
        log.warning("Unknown action", action=action, saga_id=saga_id)
        return "failed"

    log.info("Command handled", action=action, saga_id=saga_id, result=outcome)
    return outcome


async def handle_nats_message(msg):
    fields = json.loads(msg.data.decode())
    saga_id = fields.get("saga_id", "")
    try:
        event = await handle_command(fields)
        # Ensure mutating writes reach at least one replica before confirming.
        # Prevents data loss during Sentinel failover between our response and
        # the orchestrator's next action (e.g. commit after prepare).
        if event in ("prepared", "committed", "executed", "aborted", "compensated"):
            try:
                await db.execute_command("WAIT", 1, 5000)
            except Exception:
                log.warning("Replication WAIT failed (no replicas?)", saga_id=saga_id)
        if event == "failed":
            response = {"saga_id": saga_id, "event": "failed", "reason": "insufficient_credit"}
        else:
            response = {"saga_id": saga_id, "event": event}
    except Exception as e:
        log.error("NATS handler error", error=str(e), saga_id=saga_id)
        response = {"saga_id": saga_id, "event": "failed", "reason": str(e)}
    await msg.respond(json.dumps(response).encode())


async def _2pc_prepare(saga_id: str, user_id: str, amount: int, ttl: int = 30) -> int:
    keys = [
        f"user:{user_id}",
        f"lock:2pc:{saga_id}:{user_id}",
        f"saga:{saga_id}:payment:status",
    ]
    args = [str(amount), saga_id, user_id, str(ttl)]
    result = await db.fcall("payment_2pc_prepare", len(keys), *keys, *args)
    if result == 1:
        try:
            await db.execute_command("WAIT", 1, 5000)
        except Exception:
            pass
    return result


async def _2pc_commit(saga_id: str, user_id: str, amount: int):
    keys = [
        f"user:{user_id}",
        f"lock:2pc:{saga_id}:{user_id}",
        f"saga:{saga_id}:payment:status",
    ]
    await db.fcall("payment_2pc_commit", len(keys), *keys, saga_id, str(amount), user_id)


async def _2pc_abort(saga_id: str, user_id: str, amount: int = 0):
    if not user_id:
        return
    keys = [
        f"user:{user_id}",
        f"lock:2pc:{saga_id}:{user_id}",
        f"saga:{saga_id}:payment:status",
    ]
    await db.fcall("payment_2pc_abort", len(keys), *keys, saga_id, str(amount), user_id)


async def _saga_execute(saga_id: str, user_id: str, amount: int) -> int:
    keys = [
        f"user:{user_id}",
        f"saga:{saga_id}:payment:status",
        f"saga:{saga_id}:payment:amounts",
    ]
    args = [str(amount), saga_id, user_id]
    return await db.fcall("payment_saga_execute", len(keys), *keys, *args)


async def _saga_compensate(saga_id: str, user_id: str):
    if not user_id:
        return
    keys = [
        f"user:{user_id}",
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
        try:
            await db.execute_command("WAIT", 1, 5000)
        except Exception:
            pass
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
    key = f"user:{user_id}"
    entry = None
    # Try replica first, fall back to master on error or empty result
    if db_read:
        try:
            entry = await db_read.hgetall(key)
        except Exception:
            entry = None
    if not entry:
        try:
            entry = await db.hgetall(key)
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
    try:
        await db.execute_command("WAIT", 1, 5000)
    except Exception:
        pass
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
