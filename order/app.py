import logging
import os
import atexit
import random
import uuid
from collections import defaultdict

import redis
from redis.backoff import ExponentialBackoff
from redis.retry import Retry

from msgspec import msgpack, Struct
from flask import Flask, jsonify, abort, Response

import saga
import tpc
import recovery


DB_ERROR_STR = "DB error"
REQ_ERROR_STR = "Requests error"

GATEWAY_URL = os.environ['GATEWAY_URL']
TX_MODE = os.environ.get('TX_MODE', 'saga')

app = Flask("order-service")

# --- Redis connections ---
retry_strategy = Retry(ExponentialBackoff(), retries=5)

db: redis.Redis = redis.Redis(
    host=os.environ['REDIS_HOST'],
    port=int(os.environ['REDIS_PORT']),
    password=os.environ['REDIS_PASSWORD'],
    db=int(os.environ['REDIS_DB']),
    retry_on_timeout=True,
    retry=retry_strategy,
)

event_bus: redis.Redis = redis.Redis(
    host=os.environ.get('EVENT_BUS_HOST', 'event-bus'),
    port=int(os.environ.get('EVENT_BUS_PORT', 6379)),
    password=os.environ.get('EVENT_BUS_PASSWORD', 'redis'),
    db=0,
    retry_on_timeout=True,
    retry=retry_strategy,
)


def close_db_connection():
    db.close()
    event_bus.close()


atexit.register(close_db_connection)


class OrderValue(Struct):
    paid: bool
    items: list[tuple[str, int]]
    user_id: str
    total_cost: int
    checkout_status: str = ""       # "", "PENDING", "COMMITTED", "ABORTED"
    checkout_step: str = ""         # "", "STOCK", "PAYMENT", "PREPARE", "COMMIT", "ABORT", "DONE"


def save_order(redis_db, order_id: str, order_entry: OrderValue):
    """Save an order to the database."""
    redis_db.set(order_id, msgpack.encode(order_entry))


def get_order_from_db(order_id: str) -> OrderValue | None:
    try:
        entry: bytes = db.get(order_id)
    except redis.exceptions.RedisError:
        return abort(400, DB_ERROR_STR)
    entry: OrderValue | None = msgpack.decode(entry, type=OrderValue) if entry else None
    if entry is None:
        abort(400, f"Order: {order_id} not found!")
    return entry


# ==========================================
#   REST ENDPOINTS (external API — unchanged)
# ==========================================

@app.post('/create/<user_id>')
def create_order(user_id: str):
    key = str(uuid.uuid4())
    value = msgpack.encode(OrderValue(paid=False, items=[], user_id=user_id, total_cost=0))
    try:
        db.set(key, value)
    except redis.exceptions.RedisError:
        return abort(400, DB_ERROR_STR)
    return jsonify({'order_id': key})


@app.post('/batch_init/<n>/<n_items>/<n_users>/<item_price>')
def batch_init_users(n: int, n_items: int, n_users: int, item_price: int):
    n = int(n)
    n_items = int(n_items)
    n_users = int(n_users)
    item_price = int(item_price)

    def generate_entry() -> OrderValue:
        user_id = random.randint(0, n_users - 1)
        item1_id = random.randint(0, n_items - 1)
        item2_id = random.randint(0, n_items - 1)
        value = OrderValue(paid=False,
                           items=[(f"{item1_id}", 1), (f"{item2_id}", 1)],
                           user_id=f"{user_id}",
                           total_cost=2*item_price)
        return value

    kv_pairs: dict[str, bytes] = {f"{i}": msgpack.encode(generate_entry())
                                  for i in range(n)}
    try:
        db.mset(kv_pairs)
    except redis.exceptions.RedisError:
        return abort(400, DB_ERROR_STR)
    return jsonify({"msg": "Batch init for orders successful"})


@app.get('/find/<order_id>')
def find_order(order_id: str):
    order_entry: OrderValue = get_order_from_db(order_id)
    return jsonify(
        {
            "order_id": order_id,
            "paid": order_entry.paid,
            "items": order_entry.items,
            "user_id": order_entry.user_id,
            "total_cost": order_entry.total_cost
        }
    )


@app.post('/addItem/<order_id>/<item_id>/<quantity>')
def add_item(order_id: str, item_id: str, quantity: int):
    order_entry: OrderValue = get_order_from_db(order_id)

    # Fetch item price from stock service via gateway
    import requests
    try:
        item_reply = requests.get(f"{GATEWAY_URL}/stock/find/{item_id}")
    except requests.exceptions.RequestException:
        abort(400, REQ_ERROR_STR)

    if item_reply.status_code != 200:
        abort(400, f"Item: {item_id} does not exist!")

    item_json: dict = item_reply.json()
    order_entry.items.append((item_id, int(quantity)))
    order_entry.total_cost += int(quantity) * item_json["price"]
    try:
        db.set(order_id, msgpack.encode(order_entry))
    except redis.exceptions.RedisError:
        return abort(400, DB_ERROR_STR)
    return Response(f"Item: {item_id} added to: {order_id} price updated to: {order_entry.total_cost}",
                    status=200)


@app.post('/checkout/<order_id>')
def checkout(order_id: str):
    app.logger.debug(f"Checking out {order_id} (mode: {TX_MODE})")
    order_entry: OrderValue = get_order_from_db(order_id)

    # Guard: prevent double checkout
    if order_entry.checkout_status == "COMMITTED":
        return Response("Order already checked out", status=400)
    if order_entry.checkout_status == "PENDING":
        return Response("Checkout already in progress", status=400)
    # If previously ABORTED, allow retry by resetting status
    if order_entry.checkout_status == "ABORTED":
        order_entry.checkout_status = ""
        order_entry.checkout_step = ""

    # Build aggregated items
    items_quantities: dict[str, int] = defaultdict(int)
    for item_id, quantity in order_entry.items:
        items_quantities[item_id] += quantity

    if not items_quantities:
        return abort(400, "Order has no items")

    # Dispatch to the appropriate transaction protocol
    if TX_MODE == '2pc':
        success, message = tpc.checkout(order_id, order_entry, items_quantities, db, event_bus)
    else:
        success, message = saga.checkout(order_id, order_entry, items_quantities, db, event_bus)

    if success:
        app.logger.debug("Checkout successful")
        return Response(message, status=200)
    else:
        app.logger.debug(f"Checkout failed: {message}")
        return abort(400, message)


# ==========================================
#   STARTUP RECOVERY
# ==========================================

# Run recovery on startup
try:
    recovery.run_recovery(db, event_bus, TX_MODE)
except Exception as e:
    app.logger.error(f"Recovery failed: {e}")


if __name__ == '__main__':
    app.run(host="0.0.0.0", port=8000, debug=True)
else:
    gunicorn_logger = logging.getLogger('gunicorn.error')
    app.logger.handlers = gunicorn_logger.handlers
    app.logger.setLevel(gunicorn_logger.level)
