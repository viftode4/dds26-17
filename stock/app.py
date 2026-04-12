import asyncio
import json
import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import msgpack

from redis.asyncio.cluster import RedisCluster
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route

from common.config import create_redis_cluster_connection, get_cluster_nodes, wait_for_redis
from common.fault_injection import get_injector
from common.nats_transport import NatsTransport
from common.logging import setup_logging, get_logger
from common.tracing import setup_tracing, shutdown_tracing, extract_trace_context, get_tracer
from orchestrator.metrics import LatencyHistogram
from opentelemetry.instrumentation.starlette import StarletteInstrumentor


DB_ERROR_STR = "DB error"
BATCH_INIT_CHUNK_SIZE = max(1, int(os.environ.get("BATCH_INIT_CHUNK_SIZE", "5000")))

log = get_logger("stock")

db: RedisCluster | None = None
_nats_transport: NatsTransport | None = None

# Per-action Prometheus metrics
_action_counts: dict[str, dict[str, int]] = {}
_action_latency: dict[str, LatencyHistogram] = {}


@asynccontextmanager
async def lifespan(app):
    global db, _nats_transport
    setup_tracing("stock-service")
    setup_logging("stock-service")

    startup_nodes = get_cluster_nodes("STOCK_CLUSTER_NODES")
    pool_size = int(os.environ.get("REDIS_MASTER_POOL_SIZE", "512"))
    db = create_redis_cluster_connection(startup_nodes, pool_size=pool_size)
    await wait_for_redis(db, "stock-cluster")

    # Prewarm connection pool
    await asyncio.gather(*[db.ping() for _ in range(64)])

    # Load Lua function library on ALL cluster primaries
    lua_path = Path(__file__).parent / "lua" / "stock_lib.lua"
    lua_code = lua_path.read_text()
    for _attempt in range(15):
        try:
            await db.execute_command(
                "FUNCTION", "LOAD", "REPLACE", lua_code,
                target_nodes=RedisCluster.PRIMARIES,
            )
            break
        except Exception as e:
            if "NOREPLICAS" in str(e) and _attempt < 14:
                await asyncio.sleep(2.0)
                continue
            log.error("Failed to load Lua library", error=str(e))
            raise

    _nats_transport = NatsTransport(os.environ.get("NATS_URL", "nats://nats:4222"))
    await _nats_transport.connect()
    await _nats_transport.subscribe("svc.stock.*", "stock-workers", handle_nats_message)

    log.info("Stock service started")

    yield

    if _nats_transport:
        await _nats_transport.close()
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

    await injector.maybe_inject(f"before_{action}", saga_id)

    if action == "prepare":
        items = _parse_items(fields)
        ttl = int(fields.get("ttl", "30"))
        result = await _2pc_prepare(saga_id, items, ttl)
        outcome = "prepared" if result == 1 else "failed"
    elif action == "commit":
        items = _parse_items(fields)
        await _2pc_commit(saga_id, items)
        outcome = "committed"
    elif action == "abort":
        items = _parse_items(fields)
        await _2pc_abort(saga_id, items)
        outcome = "aborted"
    elif action == "execute":
        items = _parse_items(fields)
        result = await _saga_execute(saga_id, items)
        outcome = "executed" if result == 1 else "failed"
    elif action == "compensate":
        items = _parse_items(fields)
        await _saga_compensate(saga_id, items)
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
    tracer = get_tracer("stock")
    action = fields.get("action", "unknown")
    saga_id = fields.get("saga_id", "")
    with tracer.start_as_current_span(
        f"stock.handle {action}", context=ctx, kind=SpanKind.SERVER
    ) as span:
        span.set_attribute("messaging.system", "nats")
        span.set_attribute("messaging.operation", "receive")
        span.set_attribute("saga_id", saga_id)
        span.set_attribute("action", action)
        try:
            event = await handle_command(fields)
            if event == "failed":
                response = {"saga_id": saga_id, "event": "failed", "reason": "insufficient_stock"}
                span.set_attribute("outcome", "failed")
            else:
                response = {"saga_id": saga_id, "event": event}
                span.set_attribute("outcome", event)
        except Exception as e:
            log.error("NATS handler error", error=str(e), saga_id=saga_id)
            response = {"saga_id": saga_id, "event": "failed", "reason": str(e)}
            span.record_exception(e)
        await msg.respond(msgpack.packb(response, use_bin_type=True))


def _parse_items(fields: dict) -> list[tuple[str, int]]:
    items_raw = fields.get("items", "")
    if not items_raw:
        return []
    return [(str(item_id), int(qty)) for item_id, qty in json.loads(items_raw)]


async def _2pc_prepare(saga_id: str, items: list[tuple[str, int]], ttl: int = 30) -> int:
    """Prepare all items in parallel. Returns 1 only if all items prepared successfully."""
    results = await asyncio.gather(*[
        db.fcall(
            "stock_2pc_prepare_one", 3,
            f"{{item_{item_id}}}:data",
            f"{{item_{item_id}}}:lock:{saga_id}",
            f"{{item_{item_id}}}:2pc-status:{saga_id}",
            saga_id, item_id, str(amount), str(ttl),
        )
        for item_id, amount in items
    ])
    return 1 if all(r == 1 for r in results) else 0


async def _2pc_commit(saga_id: str, items: list[tuple[str, int]]):
    """Commit all items in parallel."""
    await asyncio.gather(*[
        db.fcall(
            "stock_2pc_commit_one", 3,
            f"{{item_{item_id}}}:data",
            f"{{item_{item_id}}}:lock:{saga_id}",
            f"{{item_{item_id}}}:2pc-status:{saga_id}",
            saga_id, item_id, str(amount),
        )
        for item_id, amount in items
    ])


async def _2pc_abort(saga_id: str, items: list[tuple[str, int]]):
    """Abort all items in parallel."""
    await asyncio.gather(*[
        db.fcall(
            "stock_2pc_abort_one", 3,
            f"{{item_{item_id}}}:data",
            f"{{item_{item_id}}}:lock:{saga_id}",
            f"{{item_{item_id}}}:2pc-status:{saga_id}",
            saga_id, item_id,
        )
        for item_id, _ in items
    ])


async def _saga_execute(saga_id: str, items: list[tuple[str, int]]) -> int:
    """Execute saga deductions for all items in parallel. Returns 1 only if all succeed."""
    results = await asyncio.gather(*[
        db.fcall(
            "stock_saga_execute_one", 3,
            f"{{item_{item_id}}}:data",
            f"{{item_{item_id}}}:saga-status:{saga_id}",
            f"{{item_{item_id}}}:amounts:{saga_id}",
            saga_id, item_id, str(amount),
        )
        for item_id, amount in items
    ])
    if all(r == 1 for r in results):
        return 1
    # Partial failure: roll back any items that were successfully deducted
    succeeded = [items[i] for i, r in enumerate(results) if r == 1]
    if succeeded:
        await _saga_compensate(saga_id, succeeded)
    return 0


async def _saga_compensate(saga_id: str, items: list[tuple[str, int]]):
    """Compensate all items in parallel."""
    await asyncio.gather(*[
        db.fcall(
            "stock_saga_compensate_one", 3,
            f"{{item_{item_id}}}:data",
            f"{{item_{item_id}}}:saga-status:{saga_id}",
            f"{{item_{item_id}}}:amounts:{saga_id}",
            saga_id, item_id,
        )
        for item_id, _ in items
    ])


# ============================================================================
# HTTP API endpoints
# ============================================================================

async def create_item(request: Request):
    price = int(request.path_params["price"])
    key = str(uuid.uuid4())
    try:
        await db.hset(f"{{item_{key}}}:data", mapping={
            "available_stock": 0,
            "reserved_stock": 0,
            "price": price,
        })
    except Exception:
        raise HTTPException(400, detail=DB_ERROR_STR)
    return JSONResponse({"item_id": key})


async def batch_init_users(request: Request):
    n = int(request.path_params["n"])
    starting_stock = int(request.path_params["starting_stock"])
    item_price = int(request.path_params["item_price"])
    chunk_start = 0
    chunk_end = 0
    try:
        for chunk_start in range(0, n, BATCH_INIT_CHUNK_SIZE):
            chunk_end = min(chunk_start + BATCH_INIT_CHUNK_SIZE, n)
            async with db.pipeline(transaction=False) as pipe:
                for i in range(chunk_start, chunk_end):
                    pipe.hset(f"{{item_{i}}}:data", mapping={
                        "available_stock": starting_stock,
                        "reserved_stock": 0,
                        "price": item_price,
                    })
                await pipe.execute()
    except Exception as e:
        log.error(
            "Stock batch init failed",
            error=str(e),
            n=n,
            chunk_start=chunk_start,
            chunk_end=chunk_end,
            chunk_size=BATCH_INIT_CHUNK_SIZE,
        )
        raise HTTPException(400, detail=DB_ERROR_STR)
    return JSONResponse({"msg": "Batch init for stock successful"})


async def find_item(request: Request):
    item_id = request.path_params["item_id"]
    try:
        entry = await db.hgetall(f"{{item_{item_id}}}:data")
    except Exception:
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
    key = f"{{item_{item_id}}}:data"
    try:
        new_stock = await db.fcall("stock_add_direct", 1, key, amount)
    except Exception as e:
        if "ITEM_NOT_FOUND" in str(e):
            raise HTTPException(400, detail=f"Item: {item_id} not found!")
        raise HTTPException(400, detail=DB_ERROR_STR)
    return PlainTextResponse(f"Item: {item_id} stock updated to: {new_stock}")


async def remove_stock(request: Request):
    item_id = request.path_params["item_id"]
    amount = int(request.path_params["amount"])
    key = f"{{item_{item_id}}}:data"
    try:
        new_stock = await db.fcall("stock_subtract_direct", 1, key, amount)
    except Exception as e:
        err_msg = str(e)
        if "ITEM_NOT_FOUND" in err_msg:
            raise HTTPException(400, detail=f"Item: {item_id} not found!")
        if "INSUFFICIENT_STOCK" in err_msg:
            raise HTTPException(400, detail=f"Item: {item_id} stock cannot get reduced below zero!")
        raise HTTPException(400, detail=DB_ERROR_STR)
    return PlainTextResponse(f"Item: {item_id} stock updated to: {new_stock}")


async def health(request: Request):
    try:
        await db.ping()
    except Exception:
        raise HTTPException(503, detail="Redis unavailable")
    return JSONResponse({"status": "healthy"})


async def metrics(request: Request):
    """Prometheus-compatible metrics for stock service."""
    from starlette.responses import Response
    lines = [
        "# HELP stock_requests_total Total stock service requests by action and result",
        "# TYPE stock_requests_total counter",
    ]
    for action, counts in _action_counts.items():
        for result, count in counts.items():
            lines.append(f'stock_requests_total{{action="{action}",result="{result}"}} {count}')
    lines.append("")
    lines.append("# HELP stock_request_duration_seconds Stock service request latency")
    lines.append("# TYPE stock_request_duration_seconds histogram")
    for action, hist in _action_latency.items():
        lines.extend(hist.prometheus_lines("stock_request_duration_seconds", f'action="{action}"'))
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
    Route("/item/create/{price}", create_item, methods=["POST"]),
    Route("/batch_init/{n}/{starting_stock}/{item_price}", batch_init_users, methods=["POST"]),
    Route("/find/{item_id}", find_item, methods=["GET"]),
    Route("/add/{item_id}/{amount}", add_stock, methods=["POST"]),
    Route("/subtract/{item_id}/{amount}", remove_stock, methods=["POST"]),
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
