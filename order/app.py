import asyncio
import json
import os
import random
import uuid
from collections import defaultdict
from contextlib import asynccontextmanager
from pathlib import Path

import redis.asyncio as aioredis
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.routing import Route

from common.config import create_redis_connection, create_replica_connection, wait_for_redis
from common.logging import setup_logging, get_logger
from common.result import wait_for_result
from orchestrator import (
    Orchestrator, TransactionDefinition, Step,
    LeaderElection, RecoveryWorker,
)


DB_ERROR_STR = "DB error"
LOCK_TTL = 30  # seconds for 2PC locks


log = get_logger("order")

db: aioredis.Redis | None = None
db_read: aioredis.Redis | None = None
orchestrator: Orchestrator | None = None
leader_election: LeaderElection | None = None
recovery_worker: RecoveryWorker | None = None
_leader_task: asyncio.Task | None = None


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
# Direct executors — bypass streams, call Lua FCALL directly on target Redis.
# Signature: (saga_id, action, context, db) -> {"event": ...}
# ---------------------------------------------------------------------------

async def stock_direct_executor(
    saga_id: str, action: str, context: dict, db: aioredis.Redis
) -> dict:
    """Call stock Lua functions directly — eliminates 2 stream hops."""
    items = context.get("items", [])
    n = len(items)

    if action == "prepare":
        keys = [f"item:{iid}" for iid, _ in items]
        keys += [f"lock:2pc:{saga_id}:{iid}" for iid, _ in items]
        keys += ["stock-outbox", f"saga:{saga_id}:stock:status"]
        args = [saga_id, str(LOCK_TTL)]
        for iid, qty in items:
            args += [str(iid), str(qty)]
        args.append("1")  # skip_outbox
        result = await db.fcall("stock_2pc_prepare", len(keys), *keys, *args)
        return {"event": "prepared"} if result == 1 else {"event": "failed", "reason": "insufficient_stock_or_locked"}

    elif action == "commit":
        keys = [f"item:{iid}" for iid, _ in items]
        keys += [f"lock:2pc:{saga_id}:{iid}" for iid, _ in items]
        keys += ["stock-outbox", f"saga:{saga_id}:stock:status"]
        result = await db.fcall("stock_2pc_commit", len(keys), *keys, saga_id, str(n), "1")
        if result == 1:
            return {"event": "committed"}
        return {"event": "commit_failed", "reason": "lock_expired"}

    elif action == "abort":
        keys = [f"item:{iid}" for iid, _ in items]
        keys += [f"lock:2pc:{saga_id}:{iid}" for iid, _ in items]
        keys += ["stock-outbox", f"saga:{saga_id}:stock:status"]
        await db.fcall("stock_2pc_abort", len(keys), *keys, saga_id, str(n), "1")
        return {"event": "aborted"}

    elif action == "execute":
        keys = [f"item:{iid}" for iid, _ in items]
        keys += ["stock-outbox", f"saga:{saga_id}:stock:status", f"saga:{saga_id}:stock:amounts"]
        args = [saga_id]
        for iid, qty in items:
            args += [str(iid), str(qty)]
        args.append("1")  # skip_outbox
        result = await db.fcall("stock_saga_execute", len(keys), *keys, *args)
        return {"event": "executed"} if result == 1 else {"event": "failed", "reason": "insufficient_stock"}

    elif action == "compensate":
        keys = [f"item:{iid}" for iid, _ in items]
        keys += ["stock-outbox", f"saga:{saga_id}:stock:status", f"saga:{saga_id}:stock:amounts"]
        result = await db.fcall("stock_saga_compensate", len(keys), *keys, saga_id, str(n), "1")
        return {"event": "compensated"}

    return {"event": "failed", "reason": f"unknown_action:{action}"}


async def payment_direct_executor(
    saga_id: str, action: str, context: dict, db: aioredis.Redis
) -> dict:
    """Call payment Lua functions directly — eliminates 2 stream hops."""
    user_id = context.get("user_id", "")
    amount = context.get("total_cost", 0)

    if action == "prepare":
        keys = [
            f"user:{user_id}",
            f"lock:2pc:{saga_id}:{user_id}",
            f"saga:{saga_id}:payment:status",
            "payment-outbox",
        ]
        args = [str(amount), saga_id, user_id, str(LOCK_TTL), "1"]
        result = await db.fcall("payment_2pc_prepare", len(keys), *keys, *args)
        return {"event": "prepared"} if result == 1 else {"event": "failed", "reason": "insufficient_credit_or_locked"}

    elif action == "commit":
        keys = [
            f"user:{user_id}",
            f"lock:2pc:{saga_id}:{user_id}",
            f"saga:{saga_id}:payment:status",
            "payment-outbox",
        ]
        result = await db.fcall("payment_2pc_commit", len(keys), *keys, saga_id, "1")
        if result == 1:
            return {"event": "committed"}
        return {"event": "commit_failed", "reason": "lock_expired"}

    elif action == "abort":
        if not user_id:
            return {"event": "aborted"}
        keys = [
            f"user:{user_id}",
            f"lock:2pc:{saga_id}:{user_id}",
            f"saga:{saga_id}:payment:status",
            "payment-outbox",
        ]
        await db.fcall("payment_2pc_abort", len(keys), *keys, saga_id, "1")
        return {"event": "aborted"}

    elif action == "execute":
        keys = [
            f"user:{user_id}",
            "payment-outbox",
            f"saga:{saga_id}:payment:status",
            f"saga:{saga_id}:payment:amounts",
        ]
        args = [str(amount), saga_id, user_id, "1"]
        result = await db.fcall("payment_saga_execute", len(keys), *keys, *args)
        return {"event": "executed"} if result == 1 else {"event": "failed", "reason": "insufficient_credit"}

    elif action == "compensate":
        if not user_id:
            return {"event": "compensated"}
        keys = [
            f"user:{user_id}",
            "payment-outbox",
            f"saga:{saga_id}:payment:status",
            f"saga:{saga_id}:payment:amounts",
        ]
        await db.fcall("payment_saga_compensate", len(keys), *keys, saga_id, "1")
        return {"event": "compensated"}

    return {"event": "failed", "reason": f"unknown_action:{action}"}


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
            direct_executor=stock_direct_executor,
        ),
        Step(
            name="reserve_payment",
            service="payment",
            payload_builder=payment_payload,
            direct_executor=payment_direct_executor,
        ),
    ],
)


# ---------------------------------------------------------------------------
# Reconciliation callback — domain-specific invariant checks.
# ---------------------------------------------------------------------------

async def reconcile_invariants(service_dbs: dict[str, aioredis.Redis]):
    """Verify data invariants across stock and payment services.

    Checks:
    1. No negative balances (available_stock, reserved_stock, credits)
    2. Orphan reservations: reservation keys whose saga is no longer active
    """
    stock_db = service_dbs.get("stock")
    if stock_db:
        # Check 1: negative balance invariant
        cursor = "0"
        while True:
            cursor, keys = await stock_db.scan(cursor=cursor, match="item:*", count=100)
            for key in keys:
                data = await stock_db.hgetall(key)
                avail = int(data.get("available_stock", 0))
                reserved = int(data.get("reserved_stock", 0))
                if avail < 0 or reserved < 0:
                    log.critical(
                        "INVARIANT VIOLATION: stock",
                        key=key, available_stock=avail, reserved_stock=reserved,
                    )
            if cursor == "0" or cursor == 0:
                break

        # Check 2: orphan reservation keys (saga completed but reservation lingers)
        cursor = "0"
        while True:
            cursor, keys = await stock_db.scan(
                cursor=cursor, match="saga:*:stock:status", count=100
            )
            for key in keys:
                # Extract saga_id from key pattern saga:{saga_id}:stock:status
                parts = key.split(":")
                if len(parts) >= 4:
                    saga_id = parts[1]
                    # Check if saga is still active in WAL
                    active = await db.sismember("active_sagas", saga_id)
                    if not active:
                        status_val = await stock_db.get(key)
                        if status_val and status_val not in ("confirmed", "cancelled"):
                            log.warning(
                                "Orphan stock reservation",
                                saga_id=saga_id, status=status_val,
                            )
            if cursor == "0" or cursor == 0:
                break

    payment_db = service_dbs.get("payment")
    if payment_db:
        cursor = "0"
        while True:
            cursor, keys = await payment_db.scan(cursor=cursor, match="user:*", count=100)
            for key in keys:
                data = await payment_db.hgetall(key)
                avail = int(data.get("available_credit", 0))
                held = int(data.get("held_credit", 0))
                if avail < 0 or held < 0:
                    log.critical(
                        "INVARIANT VIOLATION: payment",
                        key=key, available_credit=avail, held_credit=held,
                    )
            if cursor == "0" or cursor == 0:
                break


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
                await recovery_worker.start_claim_worker()
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
    global db, db_read, orchestrator, leader_election, recovery_worker, _leader_task

    setup_logging("order-service")

    # Master connection for writes
    db = create_redis_connection(prefix="", decode_responses=True)
    await wait_for_redis(db, "order-db")

    # Replica connection for read-only lookups
    db_read = create_replica_connection(prefix="", decode_responses=True)

    # Load Lua function library
    lua_path = Path(__file__).parent / "lua" / "order_lib.lua"
    lua_code = lua_path.read_text()
    try:
        await db.function_load(lua_code, replace=True)
    except aioredis.RedisError as e:
        log.error("Failed to load Lua library", error=str(e))
        raise

    # Cross-service Redis connections for orchestrator
    stock_db = create_redis_connection(prefix="STOCK_", decode_responses=True)
    payment_db = create_redis_connection(prefix="PAYMENT_", decode_responses=True)
    await wait_for_redis(stock_db, "stock-db")
    await wait_for_redis(payment_db, "payment-db")

    # Load Lua function libraries on stock/payment Redis (eliminates startup race —
    # order service may call FCALL before stock/payment services have loaded their Lua)
    stock_lua = (Path(__file__).parent / "lua" / "stock_lib.lua").read_text()
    payment_lua = (Path(__file__).parent / "lua" / "payment_lib.lua").read_text()
    await stock_db.function_load(stock_lua, replace=True)
    await payment_db.function_load(payment_lua, replace=True)

    # Prewarm connection pools — prevents first requests from hitting connection creation latency
    await asyncio.gather(*[db.ping() for _ in range(32)])
    await asyncio.gather(*[stock_db.ping() for _ in range(32)])
    await asyncio.gather(*[payment_db.ping() for _ in range(32)])

    service_dbs = {"stock": stock_db, "payment": payment_db}

    orchestrator = Orchestrator(
        order_db=db,
        service_dbs=service_dbs,
        definitions=[checkout_tx],
        protocol="auto",
    )
    await orchestrator.start()

    leader_election = LeaderElection(db)
    recovery_worker = RecoveryWorker(
        wal=orchestrator.wal,
        service_dbs=service_dbs,
        outbox_reader=orchestrator.outbox_reader,
        definitions=orchestrator.definitions,
        reconcile_fn=reconcile_invariants,
    )

    _leader_task = asyncio.create_task(_leadership_loop())
    log.info("Order service started (active-active mode)")

    yield

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
        for db_conn in orchestrator.service_dbs.values():
            await db_conn.aclose()
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
    # Pipeline: hgetall + lrange in 1 RTT
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
        item_id, quantity = raw.split(":", 1)
        items.append((item_id, int(quantity)))
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
    item_data = await orchestrator.service_dbs["stock"].hgetall(f"item:{item_id}")
    if not item_data:
        raise HTTPException(400, detail=f"Item: {item_id} does not exist!")
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


async def _load_order(order_id: str) -> tuple[dict, list[tuple[str, int]]]:
    """Load order + items in a single pipeline (1 RTT)."""
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
        item_id, quantity = raw.split(":", 1)
        items.append((item_id, int(quantity)))
    return entry, items


async def checkout(request: Request):
    order_id = request.path_params["order_id"]
    log.debug("Checking out order", order_id=order_id)

    # 1. Load order data + claim idempotency key in single Lua FCALL (1 RTT)
    idempotency_key = f"idempotency:checkout:{order_id}"
    saga_id = str(uuid.uuid4())
    claim_value = json.dumps({"status": "processing", "saga_id": saga_id})

    keys = [f"order:{order_id}", f"order:{order_id}:items", idempotency_key]
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

    # 2. Pre-check: already paid? (release idempotency claim if we grabbed it)
    if entry["paid"] == "true":
        if acquired:
            await db.delete(idempotency_key)
        raise HTTPException(400, detail="Order already paid")

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

    # 6. Persist result — awaited so order state is consistent before responding
    if result.get("status") == "success":
        final_value = json.dumps({"status": "success", "saga_id": saga_id})
        async with db.pipeline(transaction=True) as pipe:
            pipe.set(idempotency_key, final_value, ex=86400)
            pipe.hset(f"order:{order_id}", "paid", "true")
            await pipe.execute()
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
