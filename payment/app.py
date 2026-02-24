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

app = Flask("payment-service")

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


class UserValue(Struct):
    credit: int


# ==========================================
#   HELPERS
# ==========================================

def get_user_from_db(user_id: str) -> UserValue | None:
    try:
        entry: bytes = db.get(user_id)
    except redis.exceptions.RedisError:
        return abort(400, DB_ERROR_STR)
    entry: UserValue | None = msgpack.decode(entry, type=UserValue) if entry else None
    if entry is None:
        abort(400, f"User: {user_id} not found!")
    return entry


def atomic_add_credit(user_id: str, amount: int) -> bool:
    """Atomically add credit using WATCH/MULTI/EXEC. Returns True on success."""
    with db.pipeline() as pipe:
        while True:
            try:
                pipe.watch(user_id)
                entry = pipe.get(user_id)
                if entry is None:
                    pipe.unwatch()
                    return False
                user = msgpack.decode(entry, type=UserValue)
                user.credit += amount
                pipe.multi()
                pipe.set(user_id, msgpack.encode(user))
                pipe.execute()
                return True
            except redis.WatchError:
                continue


def atomic_subtract_credit(user_id: str, amount: int) -> bool:
    """Atomically subtract credit using WATCH/MULTI/EXEC. Returns True on success."""
    with db.pipeline() as pipe:
        while True:
            try:
                pipe.watch(user_id)
                entry = pipe.get(user_id)
                if entry is None:
                    pipe.unwatch()
                    return False
                user = msgpack.decode(entry, type=UserValue)
                if user.credit < amount:
                    pipe.unwatch()
                    return False
                user.credit -= amount
                pipe.multi()
                pipe.set(user_id, msgpack.encode(user))
                pipe.execute()
                return True
            except redis.WatchError:
                continue


# ==========================================
#   REST ENDPOINTS (external API — unchanged)
# ==========================================

@app.post('/create_user')
def create_user():
    key = str(uuid.uuid4())
    value = msgpack.encode(UserValue(credit=0))
    try:
        db.set(key, value)
    except redis.exceptions.RedisError:
        return abort(400, DB_ERROR_STR)
    return jsonify({'user_id': key})


@app.post('/batch_init/<n>/<starting_money>')
def batch_init_users(n: int, starting_money: int):
    n = int(n)
    starting_money = int(starting_money)
    kv_pairs: dict[str, bytes] = {f"{i}": msgpack.encode(UserValue(credit=starting_money))
                                  for i in range(n)}
    try:
        db.mset(kv_pairs)
    except redis.exceptions.RedisError:
        return abort(400, DB_ERROR_STR)
    return jsonify({"msg": "Batch init for users successful"})


@app.get('/find_user/<user_id>')
def find_user(user_id: str):
    user_entry: UserValue = get_user_from_db(user_id)
    return jsonify(
        {
            "user_id": user_id,
            "credit": user_entry.credit
        }
    )


@app.post('/add_funds/<user_id>/<amount>')
def add_credit(user_id: str, amount: int):
    amount = int(amount)
    if not atomic_add_credit(user_id, amount):
        return abort(400, f"User: {user_id} not found!")
    return Response(f"User: {user_id} credit updated", status=200)


@app.post('/pay/<user_id>/<amount>')
def remove_credit(user_id: str, amount: int):
    amount = int(amount)
    app.logger.debug(f"Removing {amount} credit from user: {user_id}")
    if not atomic_subtract_credit(user_id, amount):
        abort(400, f"User: {user_id} credit cannot get reduced below zero or not found!")
    return Response(f"User: {user_id} credit updated", status=200)


# ==========================================
#   EVENT-BUS COMMAND HANDLERS
# ==========================================

def handle_saga_deduct(order_id: str, user_id: str, amount: int) -> dict:
    """Idempotent credit deduction for SAGA."""
    idempotency_key = f"op:deduct:{order_id}"
    cached = db.get(idempotency_key)
    if cached is not None:
        return json.loads(cached)

    success = atomic_subtract_credit(user_id, amount)
    result = {"status": "ok" if success else "fail", "order_id": order_id, "user_id": user_id}

    # Store result for idempotency (TTL: 5 minutes)
    db.set(idempotency_key, json.dumps(result), ex=300)
    return result


def handle_saga_compensate(order_id: str, user_id: str, amount: int) -> dict:
    """Idempotent credit refund (rollback) for SAGA."""
    idempotency_key = f"op:compensate:{order_id}"
    cached = db.get(idempotency_key)
    if cached is not None:
        return json.loads(cached)

    # Only compensate if the deduction actually happened
    deduct_key = f"op:deduct:{order_id}"
    deduct_result = db.get(deduct_key)
    if deduct_result is not None:
        deduct_data = json.loads(deduct_result)
        if deduct_data.get("status") == "ok":
            atomic_add_credit(user_id, amount)

    result = {"status": "ok", "order_id": order_id, "user_id": user_id}
    db.set(idempotency_key, json.dumps(result), ex=300)
    return result


def handle_2pc_prepare(order_id: str, user_id: str, amount: int) -> dict:
    """2PC prepare: lock user funds and tentatively deduct."""
    lock_key = f"2pc:lock:{order_id}"

    # Try to acquire lock (SETNX with TTL)
    acquired = db.set(lock_key, json.dumps({"user_id": user_id, "amount": amount}), nx=True, ex=30)
    if not acquired:
        # Lock already held — idempotent
        existing = db.get(lock_key)
        if existing is not None:
            return {"status": "prepared", "order_id": order_id}
        return {"status": "fail", "order_id": order_id, "reason": "lock_conflict"}

    # Tentatively subtract credit
    success = atomic_subtract_credit(user_id, amount)
    if not success:
        db.delete(lock_key)
        return {"status": "fail", "order_id": order_id, "reason": "insufficient_credit"}

    return {"status": "prepared", "order_id": order_id}


def handle_2pc_commit(order_id: str) -> dict:
    """2PC commit: finalize (credit already deducted in prepare) and release lock."""
    lock_key = f"2pc:lock:{order_id}"
    db.delete(lock_key)
    return {"status": "committed", "order_id": order_id}


def handle_2pc_abort(order_id: str) -> dict:
    """2PC abort: restore credit and release lock."""
    lock_key = f"2pc:lock:{order_id}"
    lock_data = db.get(lock_key)
    if lock_data is not None:
        info = json.loads(lock_data)
        atomic_add_credit(info["user_id"], info["amount"])
        db.delete(lock_key)
    return {"status": "aborted", "order_id": order_id}


def handle_clear_keys(order_id: str) -> dict:
    """Clear idempotency keys for an order so it can be retried."""
    keys_to_delete = [
        f"op:deduct:{order_id}",
        f"op:compensate:{order_id}",
    ]
    for key in keys_to_delete:
        db.delete(key)
    return {"status": "ok", "order_id": order_id}


# ==========================================
#   STREAM CONSUMER
# ==========================================

STREAM_NAME = "payment-stream"
GROUP_NAME = "payment-consumers"
CONSUMER_NAME = f"payment-consumer-{uuid.uuid4().hex[:8]}"


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
    user_id = data.get(b'user_id', b'').decode()
    amount = int(data.get(b'amount', b'0'))

    if cmd == 'deduct':
        result = handle_saga_deduct(order_id, user_id, amount)
    elif cmd == 'compensate':
        result = handle_saga_compensate(order_id, user_id, amount)
    elif cmd == 'prepare':
        result = handle_2pc_prepare(order_id, user_id, amount)
    elif cmd == 'commit':
        result = handle_2pc_commit(order_id)
    elif cmd == 'abort':
        result = handle_2pc_abort(order_id)
    elif cmd == 'clear_keys':
        result = handle_clear_keys(order_id)
    else:
        app.logger.warning(f"Unknown command: {cmd}")
        result = {"status": "error", "reason": f"unknown command: {cmd}"}

    # Push response to the order service
    response_key = f"response:{order_id}:payment"
    event_bus.lpush(response_key, json.dumps(result))
    event_bus.expire(response_key, 60)

    # ACK the message
    event_bus.xack(STREAM_NAME, GROUP_NAME, msg_id)


def consumer_loop():
    """Background thread: consume from payment-stream."""
    ensure_consumer_group()
    app.logger.info(f"Payment consumer started: {CONSUMER_NAME}")

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
    app.logger.info("Payment consumer thread started")


# Run recovery and start consumer on app load
cleanup_stale_locks()
start_consumer_thread()


if __name__ == '__main__':
    app.run(host="0.0.0.0", port=8000, debug=True)
else:
    gunicorn_logger = logging.getLogger('gunicorn.error')
    app.logger.handlers = gunicorn_logger.handlers
    app.logger.setLevel(gunicorn_logger.level)
