import asyncio
import json
import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import msgpack

import redis.asyncio as aioredis
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route

from common.config import create_redis_connection, create_replica_connection, wait_for_redis, subscribe_failover_invalidation
from common.fault_injection import get_injector
from common.nats_transport import NatsTransport
from common.logging import setup_logging, get_logger
from common.tracing import setup_tracing, shutdown_tracing, extract_trace_context, get_tracer
from orchestrator.metrics import LatencyHistogram
from opentelemetry.instrumentation.starlette import StarletteInstrumentor


DB_ERROR_STR = "DB error"

log = get_logger("payment")

db: aioredis.Redis | None = None
db_read: aioredis.Redis | None = None
_nats_transport: NatsTransport | None = None
_scripts: dict[str, str] = {}

# Per-action Prometheus metrics
_action_counts: dict[str, dict[str, int]] = {}
_action_latency: dict[str, LatencyHistogram] = {}


@asynccontextmanager
async def lifespan(app):
    global db, db_read, _nats_transport
    setup_tracing("payment-service")
    setup_logging("payment-service")

    db = create_redis_connection(prefix="", decode_responses=True)
    await wait_for_redis(db, "payment-db")

    # Prewarm connection pool
    await asyncio.gather(*[db.ping() for _ in range(64)])

    # Replica connection for read-only find endpoints
    db_read = create_replica_connection(prefix="", decode_responses=True)

    # Load Lua scripts into Dragonfly script cache
    global _scripts
    script_names = [
        "payment_2pc_prepare", "payment_2pc_commit", "payment_2pc_abort",
        "payment_saga_execute", "payment_saga_compensate",
        "payment_subtract_direct", "payment_add_direct",
    ]
    lua_dir = Path(__file__).parent / "lua"
    try:
        for name in script_names:
            code = (lua_dir / f"{name}.lua").read_text()
            _scripts[name] = await db.script_load(code)
    except aioredis.RedisError as e:
        log.error("Failed to load Lua scripts", error=str(e))
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
    shutdown_tracing()


def _record_metric(action: str, result: str, duration: float) -> None:
    """Record per-action counter and latency."""
    if action not in _action_counts:
        _action_counts[action] = {"success": 0, "failed": 0}
        _action_latency[action] = LatencyHistogram()
    _action_counts[action][result] += 1
    _action_latency[action].observe(duration)


async def handle_command(fields: dict) -> str:
    """Dispatch a command to the appropriate Lua function."""
    import time as _time
    _cmd_start = _time.monotonic()
    injector = get_injector()
    action = fields.get("action", "")
    saga_id = fields.get("saga_id", "")
    user_id = fields.get("user_id", "")
    amount = fields.get("amount", "0")

    await injector.maybe_inject(f"before_{action}", saga_id)

    if action == "prepare":
        ttl = int(fields.get("ttl", "30"))
        result = await _2pc_prepare(saga_id, user_id, int(amount), ttl)
        outcome = "prepared" if result == 1 else "failed"
    elif action == "commit":
        await _2pc_commit(saga_id, user_id, int(amount))
        outcome = "committed"
    elif action == "abort":
        await _2pc_abort(saga_id, user_id)
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

    await injector.maybe_inject(f"after_{action}", saga_id)

    _record_metric(action, "success" if outcome not in ("failed",) else "failed",
                   _time.monotonic() - _cmd_start)
    log.info("Command handled", action=action, saga_id=saga_id, result=outcome)
    return outcome


async def handle_nats_message(msg):
    from opentelemetry.trace import SpanKind
    fields = msgpack.unpackb(msg.data, raw=False)
    ctx = extract_trace_context(fields)
    tracer = get_tracer("payment")
    action = fields.get("action", "unknown")
    saga_id = fields.get("saga_id", "")
    with tracer.start_as_current_span(
        f"payment.handle {action}", context=ctx, kind=SpanKind.SERVER
    ) as span:
        span.set_attribute("messaging.system", "nats")
        span.set_attribute("messaging.operation", "receive")
        span.set_attribute("saga_id", saga_id)
        span.set_attribute("action", action)
        try:
            event = await handle_command(fields)
            if event == "failed":
                response = {"saga_id": saga_id, "event": "failed", "reason": "insufficient_credit"}
                span.set_attribute("outcome", "failed")
            else:
                response = {"saga_id": saga_id, "event": event}
                span.set_attribute("outcome", event)
        except Exception as e:
            log.error("NATS handler error", error=str(e), saga_id=saga_id)
            response = {"saga_id": saga_id, "event": "failed", "reason": str(e)}
            span.record_exception(e)
        await msg.respond(msgpack.packb(response, use_bin_type=True))


async def _2pc_prepare(saga_id: str, user_id: str, amount: int, ttl: int = 30) -> int:
    keys = [
        f"user:{user_id}",
        f"lock:2pc:{saga_id}:{user_id}",
        f"saga:{saga_id}:payment:status",
    ]
    args = [str(amount), saga_id, user_id, str(ttl)]
    result = await db.evalsha(_scripts["payment_2pc_prepare"], len(keys), *keys, *args)
    return result


async def _2pc_commit(saga_id: str, user_id: str, amount: int):
    keys = [
        f"user:{user_id}",
        f"lock:2pc:{saga_id}:{user_id}",
        f"saga:{saga_id}:payment:status",
    ]
    await db.evalsha(_scripts["payment_2pc_commit"], len(keys), *keys, saga_id, str(amount), user_id)


async def _2pc_abort(saga_id: str, user_id: str):
    if not user_id:
        return
    keys = [
        f"user:{user_id}",
        f"lock:2pc:{saga_id}:{user_id}",
        f"saga:{saga_id}:payment:status",
    ]
    await db.evalsha(_scripts["payment_2pc_abort"], len(keys), *keys, saga_id)


async def _saga_execute(saga_id: str, user_id: str, amount: int) -> int:
    keys = [
        f"user:{user_id}",
        f"saga:{saga_id}:payment:status",
        f"saga:{saga_id}:payment:amounts",
    ]
    args = [str(amount), saga_id, user_id]
    result = await db.evalsha(_scripts["payment_saga_execute"], len(keys), *keys, *args)
    return result


async def _saga_compensate(saga_id: str, user_id: str):
    if not user_id:
        return
    keys = [
        f"user:{user_id}",
        f"saga:{saga_id}:payment:status",
        f"saga:{saga_id}:payment:amounts",
    ]
    await db.evalsha(_scripts["payment_saga_compensate"], len(keys), *keys, saga_id)


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
    try:
        # External reads must prefer the current Sentinel master. After a failover,
        # the replica may still be reconnecting or serving stale data.
        entry = await db.hgetall(f"user:{user_id}")
    except Exception:
        if not db_read:
            raise HTTPException(400, detail=DB_ERROR_STR)
        try:
            entry = await db_read.hgetall(f"user:{user_id}")
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
        await db.evalsha(_scripts["payment_add_direct"], 1, key, amount)
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
        new_credit = await db.evalsha(_scripts["payment_subtract_direct"], 1, key, amount)
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


async def metrics(request: Request):
    """Prometheus-compatible metrics for payment service."""
    from starlette.responses import Response
    lines = [
        "# HELP payment_requests_total Total payment service requests by action and result",
        "# TYPE payment_requests_total counter",
    ]
    for action, counts in _action_counts.items():
        for result, count in counts.items():
            lines.append(f'payment_requests_total{{action="{action}",result="{result}"}} {count}')
    lines.append("")
    lines.append("# HELP payment_request_duration_seconds Payment service request latency")
    lines.append("# TYPE payment_request_duration_seconds histogram")
    for action, hist in _action_latency.items():
        lines.extend(hist.prometheus_lines("payment_request_duration_seconds", f'action="{action}"'))
    return Response("\n".join(lines) + "\n", status_code=200, media_type="text/plain")


# ---------------------------------------------------------------------------
# Fault injection control endpoints
# ---------------------------------------------------------------------------

async def set_fault(request: Request):
    body = await request.json()
    injector = get_injector()
    injector.set_fault(body["point"], body["action"], body.get("value", 0))
    return JSONResponse({"status": "ok"})


async def clear_fault(request: Request):
    injector = get_injector()
    injector.clear_fault(request.query_params.get("point"))
    return JSONResponse({"status": "ok"})


async def get_faults(request: Request):
    injector = get_injector()
    rules = {k: {"action": v.action, "value": v.value} for k, v in injector.get_rules().items()}
    return JSONResponse(rules)


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
    Route("/metrics", metrics, methods=["GET"]),
    Route("/fault/set", set_fault, methods=["POST"]),
    Route("/fault/clear", clear_fault, methods=["POST"]),
    Route("/fault/rules", get_faults, methods=["GET"]),
]

app = Starlette(
    routes=routes,
    lifespan=lifespan,
    exception_handlers={HTTPException: http_exception_handler},
)
StarletteInstrumentor().instrument_app(app)
