import logging
import os
import uuid
import json
import asyncio

import redis.asyncio as aioredis

from quart import Quart, jsonify, abort, Response


DB_ERROR_STR = "DB error"

app = Quart("payment-service")

# --- Redis connection (own database) ---
db: aioredis.Redis = aioredis.Redis(
    host=os.environ['REDIS_HOST'],
    port=int(os.environ['REDIS_PORT']),
    password=os.environ['REDIS_PASSWORD'],
    db=int(os.environ['REDIS_DB']),
)

# Lua script handle (set during startup)
_pay_script = None

PAY_LUA = """
local avail = tonumber(redis.call('HGET', KEYS[1], 'available_credit'))
if not avail then return -1 end
local amount = tonumber(ARGV[1])
if avail < amount then return 0 end
redis.call('HINCRBY', KEYS[1], 'available_credit', -amount)
return 1
"""

# Stream consumer config
STREAM_NAME = "payment-commands"
GROUP_NAME = "payment-consumers"
CONSUMER_NAME = f"payment-consumer-{uuid.uuid4().hex[:8]}"


# ==========================================
#   REST ENDPOINTS (external API — unchanged)
# ==========================================

@app.post('/create_user')
async def create_user():
    key = str(uuid.uuid4())
    try:
        await db.hset(key, mapping={'available_credit': 0, 'held_credit': 0})
    except aioredis.RedisError:
        abort(400, DB_ERROR_STR)
    return jsonify({'user_id': key})


@app.post('/batch_init/<n>/<starting_money>')
async def batch_init_users(n, starting_money):
    n = int(n)
    starting_money = int(starting_money)
    pipe = db.pipeline()
    for i in range(n):
        pipe.hset(str(i), mapping={'available_credit': starting_money, 'held_credit': 0})
    try:
        await pipe.execute()
    except aioredis.RedisError:
        abort(400, DB_ERROR_STR)
    return jsonify({"msg": "Batch init for users successful"})


@app.get('/find_user/<user_id>')
async def find_user(user_id):
    data = await db.hgetall(user_id)
    if not data:
        abort(400, f"User: {user_id} not found!")
    return jsonify({
        "user_id": user_id,
        "credit": int(data[b'available_credit']) + int(data[b'held_credit']),
    })


@app.post('/add_funds/<user_id>/<amount>')
async def add_credit(user_id, amount):
    amount = int(amount)
    if not await db.exists(user_id):
        abort(400, f"User: {user_id} not found!")
    await db.hincrby(user_id, 'available_credit', amount)
    return Response(f"User: {user_id} credit updated", status=200)


@app.get('/health')
async def health():
    """Liveness probe."""
    return Response('OK', status=200)


@app.get('/ready')
async def ready():
    """Readiness probe."""
    try:
        await db.ping()
        return Response('OK', status=200)
    except Exception:
        return Response('NOT READY', status=503)


@app.post('/pay/<user_id>/<amount>')
async def remove_credit(user_id, amount):
    amount = int(amount)
    result = await _pay_script(keys=[user_id], args=[amount])
    if result == -1:
        abort(400, f"User: {user_id} not found!")
    elif result == 0:
        abort(400, f"User: {user_id} credit cannot get reduced below zero!")
    return Response(f"User: {user_id} credit updated", status=200)


# ==========================================
#   EVENT STREAM COMMAND HANDLERS
# ==========================================

async def handle_saga_deduct(order_id, user_id, amount):
    """Idempotent credit deduction for SAGA."""
    idempotency_key = f"op:deduct:{order_id}"
    cached = await db.get(idempotency_key)
    if cached is not None:
        return json.loads(cached)
    result_code = await _pay_script(keys=[user_id], args=[amount])
    success = result_code == 1
    result = {"status": "ok" if success else "fail", "order_id": order_id, "user_id": user_id}
    await db.set(idempotency_key, json.dumps(result), ex=300)
    return result


async def handle_saga_compensate(order_id, user_id, amount):
    """Idempotent credit refund (rollback) for SAGA."""
    idempotency_key = f"op:compensate:{order_id}"
    cached = await db.get(idempotency_key)
    if cached is not None:
        return json.loads(cached)
    deduct_key = f"op:deduct:{order_id}"
    deduct_result = await db.get(deduct_key)
    if deduct_result is not None:
        deduct_data = json.loads(deduct_result)
        if deduct_data.get("status") == "ok":
            await db.hincrby(user_id, 'available_credit', amount)
    result = {"status": "ok", "order_id": order_id, "user_id": user_id}
    await db.set(idempotency_key, json.dumps(result), ex=300)
    return result


async def handle_2pc_prepare(order_id, user_id, amount):
    """2PC prepare: lock user funds and tentatively deduct."""
    lock_key = f"2pc:lock:{order_id}"
    acquired = await db.set(lock_key, json.dumps({"user_id": user_id, "amount": amount}), nx=True, ex=30)
    if not acquired:
        existing = await db.get(lock_key)
        if existing is not None:
            return {"status": "prepared", "order_id": order_id}
        return {"status": "fail", "order_id": order_id, "reason": "lock_conflict"}
    result_code = await _pay_script(keys=[user_id], args=[amount])
    if result_code != 1:
        await db.delete(lock_key)
        return {"status": "fail", "order_id": order_id, "reason": "insufficient_credit"}
    return {"status": "prepared", "order_id": order_id}


async def handle_2pc_commit(order_id):
    """2PC commit: release lock (credit already deducted)."""
    await db.delete(f"2pc:lock:{order_id}")
    return {"status": "committed", "order_id": order_id}


async def handle_2pc_abort(order_id):
    """2PC abort: restore credit and release lock."""
    lock_key = f"2pc:lock:{order_id}"
    lock_data = await db.get(lock_key)
    if lock_data is not None:
        info = json.loads(lock_data)
        await db.hincrby(info["user_id"], 'available_credit', info["amount"])
        await db.delete(lock_key)
    return {"status": "aborted", "order_id": order_id}


async def handle_clear_keys(order_id):
    """Clear idempotency keys so an order can be retried."""
    for key in [f"op:deduct:{order_id}", f"op:compensate:{order_id}"]:
        await db.delete(key)
    return {"status": "ok", "order_id": order_id}


# ==========================================
#   STREAM CONSUMER (async background task)
# ==========================================

async def process_message(msg_id, data):
    """Route a stream message to the right handler."""
    cmd = data.get(b'cmd', b'').decode()
    order_id = data.get(b'order_id', b'').decode()
    user_id = data.get(b'user_id', b'').decode()
    amount = int(data.get(b'amount', b'0'))

    if cmd == 'deduct':
        result = await handle_saga_deduct(order_id, user_id, amount)
    elif cmd == 'compensate':
        result = await handle_saga_compensate(order_id, user_id, amount)
    elif cmd == 'prepare':
        result = await handle_2pc_prepare(order_id, user_id, amount)
    elif cmd == 'commit':
        result = await handle_2pc_commit(order_id)
    elif cmd == 'abort':
        result = await handle_2pc_abort(order_id)
    elif cmd == 'clear_keys':
        result = await handle_clear_keys(order_id)
    else:
        app.logger.warning(f"Unknown command: {cmd}")
        result = {"status": "error", "reason": f"unknown command: {cmd}"}

    response_key = f"response:{order_id}:payment"
    await db.lpush(response_key, json.dumps(result))
    await db.expire(response_key, 60)
    await db.xack(STREAM_NAME, GROUP_NAME, msg_id)


AUTOCLAIM_IDLE_MS = 30_000
AUTOCLAIM_INTERVAL = 15


async def consumer_loop():
    """Background task: consume from payment-commands stream with XAUTOCLAIM."""
    try:
        await db.xgroup_create(STREAM_NAME, GROUP_NAME, id='0', mkstream=True)
    except aioredis.ResponseError as e:
        if "BUSYGROUP" not in str(e):
            raise
    app.logger.info(f"Payment consumer started: {CONSUMER_NAME}")

    # Process own pending messages first
    while True:
        try:
            pending = await db.xreadgroup(GROUP_NAME, CONSUMER_NAME, {STREAM_NAME: '0'}, count=10)
            if not pending or not pending[0][1]:
                break
            for msg_id, data in pending[0][1]:
                await process_message(msg_id, data)
        except aioredis.ConnectionError:
            await asyncio.sleep(1)
        except Exception as e:
            app.logger.error(f"Error replaying pending: {e}")
            break

    # Read new messages + periodically autoclaim from dead consumers
    iteration = 0
    while True:
        try:
            iteration += 1
            if iteration % AUTOCLAIM_INTERVAL == 0:
                try:
                    result = await db.xautoclaim(
                        STREAM_NAME, GROUP_NAME, CONSUMER_NAME,
                        min_idle_time=AUTOCLAIM_IDLE_MS, start_id='0-0', count=10
                    )
                    claimed = result[1] if len(result) > 1 else []
                    for msg_id, data in claimed:
                        if data:
                            app.logger.info(f"Autoclaimed orphan message: {msg_id}")
                            await process_message(msg_id, data)
                except Exception as e:
                    app.logger.warning(f"XAUTOCLAIM error: {e}")

            messages = await db.xreadgroup(GROUP_NAME, CONSUMER_NAME, {STREAM_NAME: '>'}, count=10, block=5000)
            if messages:
                for msg_id, data in messages[0][1]:
                    await process_message(msg_id, data)
        except aioredis.ConnectionError:
            app.logger.warning("Connection lost, retrying in 2s...")
            await asyncio.sleep(2)
        except asyncio.CancelledError:
            return
        except Exception as e:
            app.logger.error(f"Consumer error: {e}")
            await asyncio.sleep(1)


# ==========================================
#   LIFECYCLE
# ==========================================

_consumer_task = None


@app.before_serving
async def startup():
    global _pay_script, _consumer_task
    _pay_script = db.register_script(PAY_LUA)
    _consumer_task = asyncio.create_task(consumer_loop())
    # Cleanup stale 2PC locks
    cursor = 0
    while True:
        cursor, keys = await db.scan(cursor, match="2pc:lock:*", count=100)
        for key in keys:
            if await db.ttl(key) == -1:
                app.logger.warning(f"Removing stale lock: {key}")
                await db.delete(key)
        if cursor == 0:
            break


@app.after_serving
async def shutdown():
    if _consumer_task:
        _consumer_task.cancel()
        try:
            await _consumer_task
        except asyncio.CancelledError:
            pass
    await db.aclose()
