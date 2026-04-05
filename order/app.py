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
from redis.asyncio.cluster import RedisCluster

logging.getLogger("httpx").setLevel(logging.WARNING)
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.routing import Route

from common.config import create_redis_cluster_connection, get_cluster_nodes, wait_for_redis
from common.nats_transport import NatsTransport, NatsOrchestratorTransport
from common.logging import setup_logging, get_logger
from common.result import wait_for_result
from orchestrator import (
    Orchestrator, TransactionDefinition, Step,
    LeaderElection, RecoveryWorker,
)


DB_ERROR_STR = "DB error"
GATEWAY_URL = os.environ.get("GATEWAY_URL", "http://gateway:80")


log = get_logger("order")

db: RedisCluster | None = None
orchestrator: Orchestrator | None = None
leader_election: LeaderElection | None = None
recovery_worker: RecoveryWorker | None = None
_leader_task: asyncio.Task | None = None
_http_client: httpx.AsyncClient | None = None
_nats_transport: NatsTransport | None = None


# ---------------------------------------------------------------------------
# Payload builders — domain-specific, kept OUT of the orchestrator package.
# Signature: (saga_id: str, action: str, context: dict) -> dict
# ---------------------------------------------------------------------------

def stock_payload(saga_id: str, action: str, context: dict) -> dict:
    """Build stream command fields for the stock service."""
    items = context.get("items", [])
    cmd: dict[str, str] = {}
    if items:
        cmd["items"] = json.dumps(items)
    cmd["ttl"] = str(context.get("_reservation_ttl", 60))
    return cmd


def payment_payload(saga_id: str, action: str, context: dict) -> dict:
    """Build stream command fields for the payment service."""
    return {
        "user_id": context.get("user_id", ""),
        "amount": str(context.get("total_cost", 0)),
        "ttl": str(context.get("_reservation_ttl", 60)),
    }


# ---------------------------------------------------------------------------
# Transaction definition — application-specific, uses payload builders above.
# ---------------------------------------------------------------------------

checkout_tx = TransactionDefinition(
    name="checkout",
    steps=[
        Step(
            name="reserve_stock",
            service="stock",
            payload_builder=stock_payload,
        ),
        Step(
            name="reserve_payment",
            service="payment",
            payload_builder=payment_payload,
        ),
    ],
)


# ---------------------------------------------------------------------------
# Lifespan + leadership
# ---------------------------------------------------------------------------

async def _leadership_loop():
    """Background task: compete for leader role to run maintenance workers."""
    while True:
        try:
            acquired = await leader_election.acquire()
            if acquired:
                log.info("This instance is now the LEADER (background maintenance)")
                await recovery_worker.recover_incomplete_sagas()
                await recovery_worker.start_reconciliation()
                while leader_election.is_leader:
                    await asyncio.sleep(1)
                log.warning("Lost leadership, stopping maintenance workers")
                await recovery_worker.stop()
            else:
                await asyncio.sleep(2)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.error("Leadership loop error", error=str(e))
            await asyncio.sleep(2)


@asynccontextmanager
async def lifespan(app):
    global db, orchestrator, leader_election, recovery_worker, _leader_task, _http_client, _nats_transport

    setup_logging("order-service")

    startup_nodes = get_cluster_nodes("ORDER_CLUSTER_NODES")
    pool_size = int(os.environ.get("REDIS_MASTER_POOL_SIZE", "512"))
    db = create_redis_cluster_connection(startup_nodes, pool_size=pool_size)
    await wait_for_redis(db, "order-cluster")

    # Load Lua function library on ALL cluster primaries
    lua_path = Path(__file__).parent / "lua" / "order_lib.lua"
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

    # Prewarm connection pools — prevents first requests from hitting connection creation latency
    await asyncio.gather(*[db.ping() for _ in range(32)])

    # Shared HTTP client for inter-service lookups via gateway
    _http_client = httpx.AsyncClient(base_url=GATEWAY_URL, timeout=5.0)

    # NATS transport for orchestrator-to-service communication
    _nats_transport = NatsTransport(os.environ.get("NATS_URL", "nats://nats:4222"))
    await _nats_transport.connect()
    orch_transport = NatsOrchestratorTransport(_nats_transport)

    orchestrator = Orchestrator(
        order_db=db,
        transport=orch_transport,
        definitions=[checkout_tx],
        protocol="saga",
    )
    await orchestrator.start()

    leader_election = LeaderElection(db)
    recovery_worker = RecoveryWorker(
        wal=orchestrator.wal,
        transport=orch_transport,
        definitions=orchestrator.definitions,
    )

    _leader_task = asyncio.create_task(_leadership_loop())

    log.info("Order service started (active-active mode)")

    yield

    if _http_client:
        await _http_client.aclose()
    if _leader_task:
        _leader_task.cancel()
        try:
            await _leader_task
        except asyncio.CancelledError:
            pass
    if leader_election:
        await leader_election.stop()
    if recovery_worker:
        await recovery_worker.stop()
    if orchestrator:
        await orchestrator.stop()
    if _nats_transport:
        await _nats_transport.close()
    if db:
        await db.aclose()


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------

async def create_order(request: Request):
    user_id = request.path_params["user_id"]
    key = str(uuid.uuid4())
    try:
        await db.hset(f"{{order_{key}}}:data", mapping={
            "user_id": user_id,
            "paid": "false",
            "total_cost": 0,
        })
    except Exception:
        raise HTTPException(400, detail=DB_ERROR_STR)
    return JSONResponse({"order_id": key})


async def batch_init_users(request: Request):
    n = int(request.path_params["n"])
    n_items = int(request.path_params["n_items"])
    n_users = int(request.path_params["n_users"])
    item_price = int(request.path_params["item_price"])
    try:
        # Non-transactional pipeline — cluster routes each command to the correct shard
        async with db.pipeline(transaction=False) as pipe:
            for i in range(n):
                user_id = random.randint(0, n_users - 1)
                item1_id = random.randint(0, n_items - 1)
                item2_id = random.randint(0, n_items - 1)
                pipe.hset(f"{{order_{i}}}:data", mapping={
                    "user_id": str(user_id),
                    "paid": "false",
                    "total_cost": 2 * item_price,
                })
                pipe.rpush(f"{{order_{i}}}:items", f"{item1_id}:1", f"{item2_id}:1")
            await pipe.execute()
    except Exception:
        raise HTTPException(400, detail=DB_ERROR_STR)
    return JSONResponse({"msg": "Batch init for orders successful"})


async def find_order(request: Request):
    order_id = request.path_params["order_id"]
    try:
        async with db.pipeline(transaction=False) as pipe:
            pipe.hgetall(f"{{order_{order_id}}}:data")
            pipe.lrange(f"{{order_{order_id}}}:items", 0, -1)
            entry, raw_items = await pipe.execute()
    except Exception:
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

    items_key = f"{{order_{order_id}}}:items"
    order_key = f"{{order_{order_id}}}:data"
    try:
        await db.fcall("order_add_item", 2, items_key, order_key,
                       f"{item_id}:{quantity}", cost_increase)
    except Exception as e:
        if "ORDER_NOT_FOUND" in str(e):
            raise HTTPException(400, detail=f"Order: {order_id} not found!")
        raise HTTPException(400, detail=DB_ERROR_STR)

    return PlainTextResponse(
        f"Item: {item_id} added to: {order_id} price updated to: {cost_increase}",
    )


async def checkout(request: Request):
    order_id = request.path_params["order_id"]
    log.debug("Checking out order", order_id=order_id)

    # 1. Load order data + claim idempotency key in single Lua FCALL (1 RTT)
    idempotency_key = f"{{order_{order_id}}}:idempotency:checkout"
    saga_id = str(uuid.uuid4())
    claim_value = json.dumps({"status": "processing", "saga_id": saga_id})

    keys = [f"{{order_{order_id}}}:data", f"{{order_{order_id}}}:items", idempotency_key]
    raw = await db.fcall("order_load_and_claim", len(keys), *keys, claim_value, "120")
    found, entry_flat_json, items_json, acquired_int = raw[0], raw[1], raw[2], raw[3]
    if not found:
        raise HTTPException(400, detail=f"Order: {order_id} not found!")

    # Parse flat HGETALL array [k1, v1, k2, v2, ...] into dict
    entry_flat = json.loads(entry_flat_json)
    entry = {}
    for i in range(0, len(entry_flat), 2):
        entry[entry_flat[i]] = entry_flat[i + 1]

    # Parse items list ["item_id:qty", ...]
    raw_items = json.loads(items_json)
    items = []
    for raw_item in raw_items:
        item_id, quantity = raw_item.split(":", 1)
        items.append((item_id, int(quantity)))

    acquired = acquired_int == 1
    total_cost = int(entry["total_cost"])
    user_id = entry["user_id"]

    # 2. Pre-check: already paid? Idempotent success (not an error)
    if entry["paid"] == "true":
        if acquired:
            await db.delete(idempotency_key)
        return PlainTextResponse("Checkout successful")

    # 3. Aggregate items
    items_quantities: dict[str, int] = defaultdict(int)
    for item_id, quantity in items:
        items_quantities[item_id] += quantity
    aggregated_items = list(items_quantities.items())

    if not aggregated_items:
        if acquired:
            await db.delete(idempotency_key)
        raise HTTPException(400, detail="Order has no items")

    if not acquired:
        # Another request already processing — get its saga_id and wait for result
        existing = await db.get(idempotency_key)
        if existing:
            existing_data = json.loads(existing)
            if isinstance(existing_data, dict):
                status = existing_data.get("status")
                if status == "success":
                    return PlainTextResponse("Checkout successful")
                if status == "failed":
                    raise HTTPException(400, detail=f"Checkout failed: {existing_data.get('error', 'unknown')}")
                # Still processing on another instance — wait via pub/sub fallback
                existing_saga_id = existing_data.get("saga_id", "")
                if existing_saga_id:
                    result = await wait_for_result(db, existing_saga_id, timeout=30.0)
                    if result.get("status") == "success":
                        return PlainTextResponse("Checkout successful")
                    raise HTTPException(400, detail=f"Checkout failed: {result.get('error', 'unknown')}")
        raise HTTPException(400, detail="Checkout already in progress")

    # 5. Execute saga directly — active-active, no leader dependency, no stream hop
    context = {
        "order_id": order_id,
        "user_id": user_id,
        "items": aggregated_items,
        "total_cost": total_cost,
    }
    result = await orchestrator.execute("checkout", context, saga_id_override=saga_id)

    # 6. Persist result — all three keys share {order_{order_id}} tag → same-slot transaction
    if result.get("status") == "success":
        final_value = json.dumps({"status": "success", "saga_id": saga_id})
        await db.fcall(
            "order_mark_paid", 2,
            idempotency_key,
            f"{{order_{order_id}}}:data",
            final_value, "86400",
        )
        log.info("Checkout successful", order_id=order_id, saga_id=saga_id,
                 protocol=result.get("protocol"))
        return PlainTextResponse("Checkout successful")
    else:
        # Delete idempotency key — allow client to retry after fixing conditions
        await db.delete(idempotency_key)
        error = result.get("error", "unknown")
        log.info("Checkout failed", order_id=order_id, saga_id=saga_id, error=error)
        raise HTTPException(400, detail=f"Checkout failed: {error}")


async def health(request: Request):
    try:
        await db.ping()
    except Exception:
        raise HTTPException(503, detail="Redis unavailable")
    return JSONResponse({"status": "healthy"})


async def metrics(request: Request):
    """Prometheus-compatible metrics with per-protocol latency histograms."""
    m = orchestrator.metrics
    abort_rate = m.sliding_abort_rate()
    is_leader = 1 if leader_election and leader_election.is_leader else 0

    lines = [
        "# HELP saga_total Total number of completed sagas",
        "# TYPE saga_total counter",
        f'saga_total{{result="success"}} {m.total_success}',
        f'saga_total{{result="failure"}} {m.total_failure}',
        "",
        "# HELP saga_abort_rate Current sliding window abort rate (100-saga window)",
        "# TYPE saga_abort_rate gauge",
        f"saga_abort_rate {abort_rate:.4f}",
        "",
        "# HELP saga_current_protocol Currently active transaction protocol",
        "# TYPE saga_current_protocol gauge",
        f'saga_current_protocol{{protocol="{m.current_protocol}"}} 1',
        "",
        "# HELP leader_status Whether this instance runs background maintenance",
        "# TYPE leader_status gauge",
        f"leader_status {is_leader}",
        "",
        "# HELP checkout_duration_seconds Checkout saga latency histogram",
        "# TYPE checkout_duration_seconds histogram",
    ]
    lines.extend(m.latency_2pc.prometheus_lines(
        "checkout_duration_seconds", 'protocol="2pc"'
    ))
    lines.extend(m.latency_saga.prometheus_lines(
        "checkout_duration_seconds", 'protocol="saga"'
    ))

    return Response("\n".join(lines) + "\n", status_code=200, media_type="text/plain")


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------

async def http_exception_handler(request, exc):
    return JSONResponse({"error": exc.detail}, status_code=exc.status_code)


routes = [
    Route("/create/{user_id}", create_order, methods=["POST"]),
    Route("/batch_init/{n}/{n_items}/{n_users}/{item_price}", batch_init_users, methods=["POST"]),
    Route("/find/{order_id}", find_order, methods=["GET"]),
    Route("/addItem/{order_id}/{item_id}/{quantity}", add_item, methods=["POST"]),
    Route("/checkout/{order_id}", checkout, methods=["POST"]),
    Route("/health", health, methods=["GET"]),
    Route("/metrics", metrics, methods=["GET"]),
]

app = Starlette(
    routes=routes,
    lifespan=lifespan,
    exception_handlers={HTTPException: http_exception_handler},
)
