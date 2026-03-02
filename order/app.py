import logging
import os
import random
import uuid
import asyncio
from collections import defaultdict

import redis.asyncio as aioredis
import httpx

from msgspec import msgpack, Struct
from quart import Quart, jsonify, abort, Response

import saga
import tpc
import recovery


DB_ERROR_STR = "DB error"
REQ_ERROR_STR = "Requests error"

GATEWAY_URL = os.environ['GATEWAY_URL']
TX_MODE = os.environ.get('TX_MODE', 'saga')

app = Quart("order-service")

# --- Redis connections with pools ---
_order_pool = aioredis.ConnectionPool(
    host=os.environ['REDIS_HOST'],
    port=int(os.environ['REDIS_PORT']),
    password=os.environ['REDIS_PASSWORD'],
    db=int(os.environ['REDIS_DB']),
    max_connections=2000,
)
db: aioredis.Redis = aioredis.Redis(connection_pool=_order_pool)

_stock_pool = aioredis.ConnectionPool(
    host=os.environ['STOCK_REDIS_HOST'],
    port=int(os.environ.get('STOCK_REDIS_PORT', 6379)),
    password=os.environ.get('STOCK_REDIS_PASSWORD', 'redis'),
    db=0,
    max_connections=2000,
)
stock_db: aioredis.Redis = aioredis.Redis(connection_pool=_stock_pool)

_payment_pool = aioredis.ConnectionPool(
    host=os.environ['PAYMENT_REDIS_HOST'],
    port=int(os.environ.get('PAYMENT_REDIS_PORT', 6379)),
    password=os.environ.get('PAYMENT_REDIS_PASSWORD', 'redis'),
    db=0,
    max_connections=2000,
)
payment_db: aioredis.Redis = aioredis.Redis(connection_pool=_payment_pool)

# Async HTTP client (created at startup)
http_client: httpx.AsyncClient = None


class OrderValue(Struct):
    paid: bool
    items: list[tuple[str, int]]
    user_id: str
    total_cost: int
    checkout_status: str = ""       # "", "PENDING", "COMMITTED", "ABORTED"
    checkout_step: str = ""         # "", "STOCK", "PAYMENT", "PREPARE", "COMMIT", "ABORT", "DONE"


async def save_order(redis_db, order_id: str, order_entry: OrderValue):
    """Save an order to the database."""
    await redis_db.set(order_id, msgpack.encode(order_entry))


async def get_order_from_db(order_id: str) -> OrderValue | None:
    try:
        entry: bytes = await db.get(order_id)
    except aioredis.RedisError:
        abort(400, DB_ERROR_STR)
    entry: OrderValue | None = msgpack.decode(entry, type=OrderValue) if entry else None
    if entry is None:
        abort(400, f"Order: {order_id} not found!")
    return entry


# ==========================================
#   HEALTH CHECKS
# ==========================================

@app.get('/health')
async def health():
    """Liveness probe."""
    return Response('OK', status=200)


@app.get('/ready')
async def ready():
    """Readiness probe: checks all 3 Redis connections."""
    try:
        await db.ping()
        await stock_db.ping()
        await payment_db.ping()
        return Response('OK', status=200)
    except Exception:
        return Response('NOT READY', status=503)


# ==========================================
#   REST ENDPOINTS (external API — unchanged)
# ==========================================

@app.post('/create/<user_id>')
async def create_order(user_id: str):
    key = str(uuid.uuid4())
    value = msgpack.encode(OrderValue(paid=False, items=[], user_id=user_id, total_cost=0))
    try:
        await db.set(key, value)
    except aioredis.RedisError:
        abort(400, DB_ERROR_STR)
    return jsonify({'order_id': key})


@app.post('/batch_init/<n>/<n_items>/<n_users>/<item_price>')
async def batch_init_users(n, n_items, n_users, item_price):
    try:
        n = int(n)
        n_items = int(n_items)
        n_users = int(n_users)
        item_price = int(item_price)
    except ValueError:
        return Response("Invalid arguments", status=400)

    def generate_entry() -> OrderValue:
        user_id = random.randint(0, n_users - 1)
        item1_id = random.randint(0, n_items - 1)
        item2_id = random.randint(0, n_items - 1)
        return OrderValue(paid=False,
                          items=[(f"{item1_id}", 1), (f"{item2_id}", 1)],
                          user_id=f"{user_id}",
                          total_cost=2*item_price)

    for burst in range(0, n, 10000):
        batch_size = min(10000, n - burst)
        kv_pairs: dict[str, bytes] = {f"{burst + i}": msgpack.encode(generate_entry())
                                      for i in range(batch_size)}
        try:
            await db.mset(kv_pairs)
        except aioredis.RedisError:
            return Response(DB_ERROR_STR, status=400)
            
    return jsonify({"msg": "Batch init for orders successful"})


@app.get('/find/<order_id>')
async def find_order(order_id: str):
    order_entry: OrderValue = await get_order_from_db(order_id)
    return jsonify({
        "order_id": order_id,
        "paid": order_entry.paid,
        "items": order_entry.items,
        "user_id": order_entry.user_id,
        "total_cost": order_entry.total_cost
    })


@app.post('/addItem/<order_id>/<item_id>/<quantity>')
async def add_item(order_id: str, item_id: str, quantity):
    try:
        quantity = int(quantity)
    except ValueError:
        return Response("Invalid quantity", status=400)
        
    try:
        r = await http_client.get(f"{GATEWAY_URL}/stock/find/{item_id}")
        if r.status_code != 200:
            return Response(f"Item: {item_id} does not exist!", status=400)
        price = r.json()["price"]
    except Exception as e:
        app.logger.error(f"Error communicating with stock service: {e}")
        return Response(REQ_ERROR_STR, status=400)

    async with db.pipeline(transaction=True) as pipe:
        while True:
            try:
                await pipe.watch(order_id)
                entry = await pipe.get(order_id)
                if not entry:
                    return Response(f"Order: {order_id} not found!", status=400)
                
                order_entry = msgpack.decode(entry, type=OrderValue)
                order_entry.items.append((item_id, quantity))
                order_entry.total_cost += quantity * price
                
                pipe.multi()
                pipe.set(order_id, msgpack.encode(order_entry))
                await pipe.execute()
                break
            except aioredis.WatchError:
                continue
            except aioredis.RedisError:
                return Response(DB_ERROR_STR, status=400)
                
    return Response(f"Item: {item_id} added to: {order_id} price updated to: {order_entry.total_cost}", status=200)


@app.post('/checkout/<order_id>')
async def checkout(order_id: str):
    app.logger.debug(f"Checking out {order_id} (mode: {TX_MODE})")
    order_entry: OrderValue = await get_order_from_db(order_id)

    items_quantities: dict[str, int] = defaultdict(int)
    for item_id, quantity in order_entry.items:
        items_quantities[item_id] += quantity

    if order_entry.checkout_status == "COMMITTED":
        return Response("Order already checked out", status=400)
    if order_entry.checkout_status == "PENDING":
        return Response("Checkout already in progress", status=400)
    if order_entry.checkout_status == "ABORTED":
        # For 2PC retries, wipe stale lock state from participants so the
        # new prepare phase starts fresh.
        if TX_MODE == '2pc':
            await tpc._clear_locks(order_id, order_entry, items_quantities, stock_db, payment_db)
        order_entry.checkout_status = ""
        order_entry.checkout_step = ""

    if not items_quantities:
        abort(400, "Order has no items")

    # Track for crash recovery
    await db.sadd("pending_orders", order_id)

    if TX_MODE == '2pc':
        success, message = await tpc.checkout(order_id, order_entry, items_quantities, db, stock_db, payment_db)
    else:
        success, message = await saga.checkout(order_id, order_entry, items_quantities, db, stock_db, payment_db)

    await db.srem("pending_orders", order_id)

    if success:
        app.logger.debug("Checkout successful")
        return Response(message, status=200)
    else:
        app.logger.debug(f"Checkout failed: {message}")
        abort(400, message)


# ==========================================
#   LIFECYCLE
# ==========================================

_reconciliation_task = None


@app.before_serving
async def startup():
    global http_client, _reconciliation_task
    http_client = httpx.AsyncClient(timeout=30.0)
    try:
        await recovery.run_recovery(db, stock_db, payment_db, TX_MODE)
    except Exception as e:
        app.logger.error(f"Recovery failed: {e}")
    # Start periodic reconciliation worker
    import reconciliation
    _reconciliation_task = asyncio.create_task(
        reconciliation.reconciliation_loop(db, stock_db, payment_db, TX_MODE)
    )


@app.after_serving
async def shutdown():
    if _reconciliation_task:
        _reconciliation_task.cancel()
        try:
            await _reconciliation_task
        except asyncio.CancelledError:
            pass
    if http_client:
        await http_client.aclose()
    await db.aclose()
    await stock_db.aclose()
    await payment_db.aclose()

