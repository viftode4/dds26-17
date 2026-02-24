import logging
import os
import atexit
import uuid
import json
import threading
import time

import redis
from redis.backoff import ExponentialBackoff
from redis.retry import Retry

from msgspec import msgpack, Struct
from flask import Flask, jsonify, abort, Response


DB_ERROR_STR = "DB error"

app = Flask("stock-service")

# --- Configuration ---
TX_MODE = os.environ.get('TX_MODE', 'saga')

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


class StockValue(Struct):
    stock: int
    price: int


# ==========================================
#   HELPERS
# ==========================================

def get_item_from_db(item_id: str) -> StockValue | None:
    try:
        entry: bytes = db.get(item_id)
    except redis.exceptions.RedisError:
        return abort(400, DB_ERROR_STR)
    entry: StockValue | None = msgpack.decode(entry, type=StockValue) if entry else None
    if entry is None:
        abort(400, f"Item: {item_id} not found!")
    return entry


def atomic_add_stock(item_id: str, amount: int) -> bool:
    """Atomically add stock using WATCH/MULTI/EXEC. Returns True on success."""
    with db.pipeline() as pipe:
        while True:
            try:
                pipe.watch(item_id)
                entry = pipe.get(item_id)
                if entry is None:
                    pipe.unwatch()
                    return False
                item = msgpack.decode(entry, type=StockValue)
                item.stock += amount
                pipe.multi()
                pipe.set(item_id, msgpack.encode(item))
                pipe.execute()
                return True
            except redis.WatchError:
                continue


def atomic_subtract_stock(item_id: str, amount: int) -> bool:
    """Atomically subtract stock using WATCH/MULTI/EXEC. Returns True on success."""
    with db.pipeline() as pipe:
        while True:
            try:
                pipe.watch(item_id)
                entry = pipe.get(item_id)
                if entry is None:
                    pipe.unwatch()
                    return False
                item = msgpack.decode(entry, type=StockValue)
                if item.stock < amount:
                    pipe.unwatch()
                    return False
                item.stock -= amount
                pipe.multi()
                pipe.set(item_id, msgpack.encode(item))
                pipe.execute()
                return True
            except redis.WatchError:
                continue


# ==========================================
#   REST ENDPOINTS (external API — unchanged)
# ==========================================

@app.post('/item/create/<price>')
def create_item(price: int):
    key = str(uuid.uuid4())
    app.logger.debug(f"Item: {key} created")
    value = msgpack.encode(StockValue(stock=0, price=int(price)))
    try:
        db.set(key, value)
    except redis.exceptions.RedisError:
        return abort(400, DB_ERROR_STR)
    return jsonify({'item_id': key})


@app.post('/batch_init/<n>/<starting_stock>/<item_price>')
def batch_init_users(n: int, starting_stock: int, item_price: int):
    n = int(n)
    starting_stock = int(starting_stock)
    item_price = int(item_price)
    kv_pairs: dict[str, bytes] = {f"{i}": msgpack.encode(StockValue(stock=starting_stock, price=item_price))
                                  for i in range(n)}
    try:
        db.mset(kv_pairs)
    except redis.exceptions.RedisError:
        return abort(400, DB_ERROR_STR)
    return jsonify({"msg": "Batch init for stock successful"})


@app.get('/find/<item_id>')
def find_item(item_id: str):
    item_entry: StockValue = get_item_from_db(item_id)
    return jsonify(
        {
            "stock": item_entry.stock,
            "price": item_entry.price
        }
    )


@app.post('/add/<item_id>/<amount>')
def add_stock(item_id: str, amount: int):
    amount = int(amount)
    if not atomic_add_stock(item_id, amount):
        return abort(400, f"Item: {item_id} not found!")
    return Response(f"Item: {item_id} stock updated", status=200)


@app.post('/subtract/<item_id>/<amount>')
def remove_stock(item_id: str, amount: int):
    amount = int(amount)
    if not atomic_subtract_stock(item_id, amount):
        abort(400, f"Item: {item_id} stock cannot get reduced below zero or not found!")
    return Response(f"Item: {item_id} stock updated", status=200)


# ==========================================
#   EVENT-BUS COMMAND HANDLERS
# ==========================================

def handle_saga_reserve(order_id: str, item_id: str, quantity: int) -> dict:
    """Idempotent stock reservation for SAGA."""
    idempotency_key = f"op:reserve:{order_id}:{item_id}"
    # Check if already processed
    cached = db.get(idempotency_key)
    if cached is not None:
        return json.loads(cached)

    success = atomic_subtract_stock(item_id, quantity)
    result = {"status": "ok" if success else "fail", "order_id": order_id, "item_id": item_id}

    # Store result for idempotency (TTL: 5 minutes)
    db.set(idempotency_key, json.dumps(result), ex=300)
    return result


def handle_saga_compensate(order_id: str, item_id: str, quantity: int) -> dict:
    """Idempotent stock compensation (rollback) for SAGA."""
    idempotency_key = f"op:compensate:{order_id}:{item_id}"
    cached = db.get(idempotency_key)
    if cached is not None:
        return json.loads(cached)

    # Only compensate if the reserve actually happened
    reserve_key = f"op:reserve:{order_id}:{item_id}"
    reserve_result = db.get(reserve_key)
    if reserve_result is not None:
        reserve_data = json.loads(reserve_result)
        if reserve_data.get("status") == "ok":
            atomic_add_stock(item_id, quantity)

    result = {"status": "ok", "order_id": order_id, "item_id": item_id}
    db.set(idempotency_key, json.dumps(result), ex=300)
    return result


def handle_2pc_prepare(order_id: str, item_id: str, quantity: int) -> dict:
    """2PC prepare: lock item and tentatively hold stock."""
    lock_key = f"2pc:lock:{order_id}:{item_id}"

    # Try to acquire lock (SETNX with TTL)
    acquired = db.set(lock_key, quantity, nx=True, ex=30)
    if not acquired:
        # Lock already held — check if it's for the same order (idempotent)
        existing = db.get(lock_key)
        if existing is not None:
            return {"status": "prepared", "order_id": order_id, "item_id": item_id}
        return {"status": "fail", "order_id": order_id, "item_id": item_id, "reason": "lock_conflict"}

    # Check stock without subtracting yet
    try:
        entry = db.get(item_id)
    except redis.exceptions.RedisError:
        db.delete(lock_key)
        return {"status": "fail", "order_id": order_id, "item_id": item_id, "reason": "db_error"}

    if entry is None:
        db.delete(lock_key)
        return {"status": "fail", "order_id": order_id, "item_id": item_id, "reason": "not_found"}

    item = msgpack.decode(entry, type=StockValue)
    if item.stock < quantity:
        db.delete(lock_key)
        return {"status": "fail", "order_id": order_id, "item_id": item_id, "reason": "insufficient_stock"}

    # Tentatively subtract stock (will be restored on abort)
    success = atomic_subtract_stock(item_id, quantity)
    if not success:
        db.delete(lock_key)
        return {"status": "fail", "order_id": order_id, "item_id": item_id, "reason": "concurrent_conflict"}

    return {"status": "prepared", "order_id": order_id, "item_id": item_id}


def handle_2pc_commit(order_id: str, item_id: str) -> dict:
    """2PC commit: finalize (stock already deducted in prepare) and release lock."""
    lock_key = f"2pc:lock:{order_id}:{item_id}"
    db.delete(lock_key)
    return {"status": "committed", "order_id": order_id, "item_id": item_id}


def handle_2pc_abort(order_id: str, item_id: str, quantity: int) -> dict:
    """2PC abort: restore stock and release lock."""
    lock_key = f"2pc:lock:{order_id}:{item_id}"
    if db.exists(lock_key):
        atomic_add_stock(item_id, quantity)
        db.delete(lock_key)
    return {"status": "aborted", "order_id": order_id, "item_id": item_id}


def handle_clear_keys(order_id: str, item_id: str) -> dict:
    """Clear idempotency keys for an order/item so it can be retried."""
    keys_to_delete = [
        f"op:reserve:{order_id}:{item_id}",
        f"op:compensate:{order_id}:{item_id}",
    ]
    for key in keys_to_delete:
        db.delete(key)
    return {"status": "ok", "order_id": order_id}


# ==========================================
#   STREAM CONSUMER
# ==========================================

STREAM_NAME = "stock-stream"
GROUP_NAME = "stock-consumers"
CONSUMER_NAME = f"stock-consumer-{uuid.uuid4().hex[:8]}"


def ensure_consumer_group():
    """Create the consumer group if it doesn't exist."""
    try:
        event_bus.xgroup_create(STREAM_NAME, GROUP_NAME, id='0', mkstream=True)
    except redis.exceptions.ResponseError as e:
        if "BUSYGROUP" not in str(e):
            raise


def process_message(msg_id: str, data: dict):
    """Route a stream message to the right handler."""
    cmd = data.get(b'cmd', b'').decode()
    order_id = data.get(b'order_id', b'').decode()
    item_id = data.get(b'item_id', b'').decode()
    quantity = int(data.get(b'quantity', b'0'))

    if cmd == 'reserve':
        result = handle_saga_reserve(order_id, item_id, quantity)
    elif cmd == 'compensate':
        result = handle_saga_compensate(order_id, item_id, quantity)
    elif cmd == 'prepare':
        result = handle_2pc_prepare(order_id, item_id, quantity)
    elif cmd == 'commit':
        result = handle_2pc_commit(order_id, item_id)
    elif cmd == 'abort':
        result = handle_2pc_abort(order_id, item_id, quantity)
    elif cmd == 'clear_keys':
        result = handle_clear_keys(order_id, item_id)
    else:
        app.logger.warning(f"Unknown command: {cmd}")
        result = {"status": "error", "reason": f"unknown command: {cmd}"}

    # Push response to the order service
    response_key = f"response:{order_id}:stock"
    event_bus.lpush(response_key, json.dumps(result))
    # Set TTL on response key so it gets cleaned up
    event_bus.expire(response_key, 60)

    # ACK the message
    event_bus.xack(STREAM_NAME, GROUP_NAME, msg_id)


def consumer_loop():
    """Background thread: consume from stock-stream."""
    ensure_consumer_group()
    app.logger.info(f"Stock consumer started: {CONSUMER_NAME}")

    # First, process any pending messages (from previous crashes)
    while True:
        try:
            pending = event_bus.xreadgroup(
                GROUP_NAME, CONSUMER_NAME, {STREAM_NAME: '0'}, count=10
            )
            if not pending or not pending[0][1]:
                break
            for msg_id, data in pending[0][1]:
                process_message(msg_id, data)
        except redis.exceptions.ConnectionError:
            app.logger.warning("Event-bus connection error during pending replay, retrying...")
            time.sleep(1)
        except Exception as e:
            app.logger.error(f"Error replaying pending messages: {e}")
            break

    # Then, read new messages
    while True:
        try:
            messages = event_bus.xreadgroup(
                GROUP_NAME, CONSUMER_NAME, {STREAM_NAME: '>'}, count=10, block=5000
            )
            if messages:
                for msg_id, data in messages[0][1]:
                    process_message(msg_id, data)
        except redis.exceptions.ConnectionError:
            app.logger.warning("Event-bus connection lost, retrying in 2s...")
            time.sleep(2)
        except Exception as e:
            app.logger.error(f"Consumer error: {e}")
            time.sleep(1)


# ==========================================
#   STARTUP RECOVERY
# ==========================================

def cleanup_stale_locks():
    """Remove stale 2PC locks on startup."""
    try:
        cursor = 0
        while True:
            cursor, keys = db.scan(cursor, match="2pc:lock:*", count=100)
            for key in keys:
                ttl = db.ttl(key)
                if ttl == -1:
                    # Key exists without TTL — stale lock, remove it
                    app.logger.warning(f"Removing stale lock: {key}")
                    db.delete(key)
            if cursor == 0:
                break
    except redis.exceptions.RedisError as e:
        app.logger.error(f"Error during lock cleanup: {e}")


# ==========================================
#   APP STARTUP
# ==========================================

def start_consumer_thread():
    """Start the background consumer thread."""
    thread = threading.Thread(target=consumer_loop, daemon=True)
    thread.start()
    app.logger.info("Stock consumer thread started")


# Run recovery and start consumer on app load
cleanup_stale_locks()
start_consumer_thread()


if __name__ == '__main__':
    app.run(host="0.0.0.0", port=8000, debug=True)
else:
    gunicorn_logger = logging.getLogger('gunicorn.error')
    app.logger.handlers = gunicorn_logger.handlers
    app.logger.setLevel(gunicorn_logger.level)
