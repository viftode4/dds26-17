import asyncio
import json
import os
import random
import uuid
from collections import defaultdict
from pathlib import Path

import httpx
import redis.asyncio as aioredis

from msgspec import msgpack
from quart import Quart, jsonify, abort, Response

from common.config import create_redis_connection, create_replica_connection, wait_for_redis
from common.logging import setup_logging, get_logger
from common.result import wait_for_result
from orchestrator import (
    Orchestrator, TransactionDefinition, Step,
    LeaderElection, RecoveryWorker,
)


DB_ERROR_STR = "DB error"
REQ_ERROR_STR = "Requests error"

GATEWAY_URL = os.environ['GATEWAY_URL']

app = Quart("order-service")
log = get_logger("order")

db: aioredis.Redis | None = None
db_read: aioredis.Redis | None = None  # Replica connection for read-only endpoints
http_client: httpx.AsyncClient | None = None
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
        cmd["items"] = msgpack.encode(items).decode("latin-1")
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
            action="try_reserve",
            compensate="cancel",
            confirm="confirm",
            payload_builder=stock_payload,
        ),
        Step(
            name="reserve_payment",
            service="payment",
            action="try_reserve",
            compensate="cancel",
            confirm="confirm",
            payload_builder=payment_payload,
        ),
    ],
)


# ---------------------------------------------------------------------------
# Reconciliation callback — domain-specific invariant checks.
# ---------------------------------------------------------------------------

async def reconcile_invariants(service_dbs: dict[str, aioredis.Redis]):
    """Verify data invariants across stock and payment services."""
    from common.logging import get_logger
    _log = get_logger("reconciliation")

    stock_db = service_dbs.get("stock")
    if stock_db:
        cursor = "0"
        while True:
            cursor, keys = await stock_db.scan(cursor=cursor, match="item:*", count=100)
            for key in keys:
                data = await stock_db.hgetall(key)
                avail = int(data.get("available_stock", 0))
                reserved = int(data.get("reserved_stock", 0))
                if avail < 0 or reserved < 0:
                    _log.critical(
                        "INVARIANT VIOLATION: stock",
                        key=key, available_stock=avail, reserved_stock=reserved,
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
                    _log.critical(
                        "INVARIANT VIOLATION: payment",
                        key=key, available_credit=avail, held_credit=held,
                    )
            if cursor == "0" or cursor == 0:
                break


# ---- JSON error handlers (replaces Quart's HTML error pages) ----

@app.errorhandler(400)
async def bad_request(e):
    return jsonify({"error": str(e.description)}), 400

@app.errorhandler(404)
async def not_found(e):
    return jsonify({"error": str(e.description)}), 404

@app.errorhandler(500)
async def internal_error(e):
    return jsonify({"error": str(e.description)}), 500

@app.errorhandler(503)
async def service_unavailable(e):
    return jsonify({"error": str(e.description)}), 503


@app.before_serving
async def setup():
    global db, db_read, http_client, orchestrator, leader_election, recovery_worker, _leader_task

    setup_logging("order-service")

    # Master connection for writes
    db = create_redis_connection(prefix="", decode_responses=True)
    await wait_for_redis(db, "order-db")

    # Replica connection for read-only lookups (falls back to master if unavailable)
    db_read = create_replica_connection(prefix="", decode_responses=True)

    http_client = httpx.AsyncClient(base_url=GATEWAY_URL, timeout=10.0)

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

    service_dbs = {"stock": stock_db, "payment": payment_db}

    orchestrator = Orchestrator(
        order_db=db,
        service_dbs=service_dbs,
        definitions=[checkout_tx],
        protocol="auto",
    )

    # Start outbox readers on ALL instances — both run sagas directly
    await orchestrator.start()

    leader_election = LeaderElection(db)
    recovery_worker = RecoveryWorker(
        wal=orchestrator.wal,
        service_dbs=service_dbs,
        outbox_reader=orchestrator.outbox_reader,
        definitions=orchestrator.definitions,
        reconcile_fn=reconcile_invariants,
    )

    # Leader governs background maintenance only: XAUTOCLAIM, reconciliation, orphan abort
    _leader_task = asyncio.create_task(_leadership_loop())
    log.info("Order service started (active-active mode)")


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


@app.after_serving
async def teardown():
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
    if http_client:
        await http_client.aclose()
    if db_read:
        await db_read.aclose()
    if db:
        await db.aclose()


async def get_order_from_db(order_id: str, use_replica: bool = False) -> dict:
    key = f"order:{order_id}"
    conn = db_read if use_replica else db
    try:
        entry = await conn.hgetall(key)
    except Exception:
        # Fallback to master on replica error
        try:
            entry = await db.hgetall(key)
        except aioredis.RedisError:
            abort(400, DB_ERROR_STR)
    if not entry:
        abort(400, f"Order: {order_id} not found!")
    return entry


async def get_order_items(order_id: str, use_replica: bool = False) -> list[tuple[str, int]]:
    key = f"order:{order_id}:items"
    conn = db_read if use_replica else db
    try:
        raw_items = await conn.lrange(key, 0, -1)
    except Exception:
        try:
            raw_items = await db.lrange(key, 0, -1)
        except aioredis.RedisError:
            abort(400, DB_ERROR_STR)
    items = []
    for raw in raw_items:
        item_id, quantity = raw.split(":", 1)
        items.append((item_id, int(quantity)))
    return items


@app.post('/create/<user_id>')
async def create_order(user_id: str):
    key = str(uuid.uuid4())
    try:
        await db.hset(f"order:{key}", mapping={
            "user_id": user_id,
            "paid": "false",
            "total_cost": 0,
        })
    except aioredis.RedisError:
        abort(400, DB_ERROR_STR)
    return jsonify({'order_id': key})


@app.post('/batch_init/<n>/<n_items>/<n_users>/<item_price>')
async def batch_init_users(n: int, n_items: int, n_users: int, item_price: int):
    n = int(n)
    n_items = int(n_items)
    n_users = int(n_users)
    item_price = int(item_price)
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
        abort(400, DB_ERROR_STR)
    return jsonify({"msg": "Batch init for orders successful"})


@app.get('/find/<order_id>')
async def find_order(order_id: str):
    # Read-only: use replica for reduced master load
    entry = await get_order_from_db(order_id, use_replica=True)
    items = await get_order_items(order_id, use_replica=True)
    return jsonify({
        "order_id": order_id,
        "paid": entry["paid"] == "true",
        "items": items,
        "user_id": entry["user_id"],
        "total_cost": int(entry["total_cost"]),
    })


async def send_get_request(url: str):
    try:
        response = await http_client.get(url)
    except httpx.RequestError:
        abort(400, REQ_ERROR_STR)
    return response


@app.post('/addItem/<order_id>/<item_id>/<quantity>')
async def add_item(order_id: str, item_id: str, quantity: int):
    quantity = int(quantity)
    if quantity <= 0:
        abort(400, "Quantity must be positive")
    item_reply = await send_get_request(f"/stock/find/{item_id}")
    if item_reply.status_code != 200:
        abort(400, f"Item: {item_id} does not exist!")
    item_json: dict = item_reply.json()
    cost_increase = quantity * item_json["price"]

    items_key = f"order:{order_id}:items"
    order_key = f"order:{order_id}"
    try:
        await db.fcall("order_add_item", 2, items_key, order_key,
                       f"{item_id}:{quantity}", cost_increase)
    except aioredis.ResponseError as e:
        if "ORDER_NOT_FOUND" in str(e):
            abort(400, f"Order: {order_id} not found!")
        abort(400, DB_ERROR_STR)
    except aioredis.RedisError:
        abort(400, DB_ERROR_STR)

    return Response(
        f"Item: {item_id} added to: {order_id} price updated to: {cost_increase}",
        status=200,
    )


@app.post('/checkout/<order_id>')
async def checkout(order_id: str):
    log.debug("Checking out order", order_id=order_id)

    # 1. Load order data from master (needs fresh paid status)
    entry = await get_order_from_db(order_id)
    items = await get_order_items(order_id)
    total_cost = int(entry["total_cost"])
    user_id = entry["user_id"]

    # 2. Pre-check: already paid?
    if entry["paid"] == "true":
        abort(400, "Order already paid")

    # 3. Aggregate items
    items_quantities: dict[str, int] = defaultdict(int)
    for item_id, quantity in items:
        items_quantities[item_id] += quantity
    aggregated_items = list(items_quantities.items())

    if not aggregated_items:
        abort(400, "Order has no items")

    # 4. Idempotency check — exactly-once checkout guarantee
    idempotency_key = f"idempotency:checkout:{order_id}"
    saga_id = str(uuid.uuid4())
    claim_value = json.dumps({"status": "processing", "saga_id": saga_id})

    acquired = await db.set(idempotency_key, claim_value, nx=True, ex=3600)
    if not acquired:
        # Another request already processing — get its saga_id and wait for result
        existing = await db.get(idempotency_key)
        if existing:
            existing_data = json.loads(existing)
            if isinstance(existing_data, dict):
                status = existing_data.get("status")
                if status == "success":
                    return Response("Checkout successful", status=200)
                if status == "failed":
                    abort(400, f"Checkout failed: {existing_data.get('error', 'unknown')}")
                # Still processing on another instance — wait via pub/sub fallback
                existing_saga_id = existing_data.get("saga_id", "")
                if existing_saga_id:
                    result = await wait_for_result(db, existing_saga_id, timeout=30.0)
                    if result.get("status") == "success":
                        return Response("Checkout successful", status=200)
                    abort(400, f"Checkout failed: {result.get('error', 'unknown')}")
        abort(400, "Checkout already in progress")

    # 5. Execute saga directly — active-active, no leader dependency, no stream hop
    context = {
        "order_id": order_id,
        "user_id": user_id,
        "items": aggregated_items,
        "total_cost": total_cost,
    }
    result = await orchestrator.execute("checkout", context, saga_id_override=saga_id)

    # 6. Cache result and update order
    if result.get("status") == "success":
        final_value = json.dumps({"status": "success", "saga_id": saga_id})
        await db.set(idempotency_key, final_value, ex=86400)
        await db.hset(f"order:{order_id}", "paid", "true")
        log.info("Checkout successful", order_id=order_id, saga_id=saga_id,
                 protocol=result.get("protocol"))
        return Response("Checkout successful", status=200)
    else:
        # Delete idempotency key — allow client to retry after fixing conditions
        await db.delete(idempotency_key)
        error = result.get("error", "unknown")
        log.info("Checkout failed", order_id=order_id, saga_id=saga_id, error=error)
        abort(400, f"Checkout failed: {error}")


@app.get('/health')
async def health():
    try:
        await db.ping()
    except Exception:
        abort(503, "Redis unavailable")
    return jsonify({"status": "healthy"})


@app.get('/metrics')
async def metrics():
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

    return Response("\n".join(lines) + "\n", status=200, content_type="text/plain")


if __name__ == '__main__':
    app.run(host="0.0.0.0", port=8000, debug=True)
