import logging
import os
import uuid
import json
import asyncio

import redis.asyncio as aioredis

from quart import Quart, jsonify, abort, Response


DB_ERROR_STR = "DB error"

app = Quart("stock-service")

# --- Redis connection with pool (own database) ---
_pool = aioredis.ConnectionPool(
    host=os.environ['REDIS_HOST'],
    port=int(os.environ['REDIS_PORT']),
    password=os.environ['REDIS_PASSWORD'],
    db=int(os.environ['REDIS_DB']),
    max_connections=2000,
)
db: aioredis.Redis = aioredis.Redis(connection_pool=_pool)

# Lua script handle (set during startup)
_subtract_script = None

SUBTRACT_LUA = """
local avail = tonumber(redis.call('HGET', KEYS[1], 'available_stock'))
if not avail then return -1 end
local amount = tonumber(ARGV[1])
if avail < amount then return 0 end
redis.call('HINCRBY', KEYS[1], 'available_stock', -amount)
redis.call('HINCRBY', KEYS[1], 'reserved_stock', amount)
return 1
"""

DIRECT_SUBTRACT_LUA = """
local avail = tonumber(redis.call('HGET', KEYS[1], 'available_stock'))
if not avail then return -1 end
local amount = tonumber(ARGV[1])
if avail < amount then return 0 end
redis.call('HINCRBY', KEYS[1], 'available_stock', -amount)
return 1
"""

CONFIRM_LUA = """
local amount = tonumber(ARGV[1])
redis.call('HINCRBY', KEYS[1], 'reserved_stock', -amount)
return 1
"""

COMPENSATE_LUA = """
local amount = tonumber(ARGV[1])
redis.call('HINCRBY', KEYS[1], 'reserved_stock', -amount)
redis.call('HINCRBY', KEYS[1], 'available_stock', amount)
return 1
"""

# Stream consumer config
STREAM_NAME = "stock-commands"
GROUP_NAME = "stock-consumers"
CONSUMER_NAME = f"stock-consumer-{uuid.uuid4().hex[:8]}"


# ==========================================
#   REST ENDPOINTS (external API — unchanged)
# ==========================================

@app.post('/item/create/<price>')
async def create_item(price):
    key = str(uuid.uuid4())
    price = int(price)
    try:
        await db.hset(key, mapping={
            'available_stock': 0,
            'reserved_stock': 0,
            'price': price,
        })
    except aioredis.RedisError:
        abort(400, DB_ERROR_STR)
    return jsonify({'item_id': key})


@app.post('/batch_init/<n>/<starting_stock>/<item_price>')
async def batch_init_users(n, starting_stock, item_price):
    try:
        n = int(n)
        starting_stock = int(starting_stock)
        item_price = int(item_price)
    except ValueError:
        return Response("Invalid arguments", status=400)
        
    for burst in range(0, n, 10000):
        pipe = db.pipeline()
        for i in range(burst, min(n, burst + 10000)):
            pipe.hset(str(i), mapping={
                'available_stock': starting_stock,
                'reserved_stock': 0,
                'price': item_price,
            })
        try:
            await pipe.execute()
        except aioredis.RedisError:
            return Response(DB_ERROR_STR, status=400)
            
    return jsonify({"msg": "Batch init for stock successful"})


@app.get('/find/<item_id>')
async def find_item(item_id):
    data = await db.hgetall(item_id)
    if not data:
        abort(400, f"Item: {item_id} not found!")
    return jsonify({
        "stock": int(data[b'available_stock']) + int(data[b'reserved_stock']),
        "price": int(data[b'price']),
    })


@app.post('/add/<item_id>/<amount>')
async def add_stock(item_id, amount):
    amount = int(amount)
    if not await db.exists(item_id):
        abort(400, f"Item: {item_id} not found!")
    await db.hincrby(item_id, 'available_stock', amount)
    return Response(f"Item: {item_id} stock updated", status=200)


@app.get('/health')
async def health():
    """Liveness probe: service is running."""
    return Response('OK', status=200)


@app.get('/ready')
async def ready():
    """Readiness probe: service can serve requests."""
    try:
        await db.ping()
        return Response('OK', status=200)
    except Exception:
        return Response('NOT READY', status=503)


@app.post('/subtract/<item_id>/<amount>')
async def remove_stock(item_id, amount):
    amount = int(amount)
    result = await _direct_subtract_script(keys=[item_id], args=[amount])
    if result == -1:
        abort(400, f"Item: {item_id} not found!")
    elif result == 0:
        abort(400, f"Item: {item_id} stock cannot get reduced below zero!")
    return Response(f"Item: {item_id} stock updated", status=200)


# ==========================================
#   EVENT STREAM COMMAND HANDLERS
# ==========================================

async def handle_saga_reserve(order_id, item_id, quantity):
    """Idempotent stock reservation for SAGA."""
    idempotency_key = f"op:reserve:{order_id}:{item_id}"
    cached = await db.get(idempotency_key)
    if cached is not None:
        return json.loads(cached)
    result_code = await _subtract_script(keys=[item_id], args=[quantity])
    success = result_code == 1
    result = {"status": "ok" if success else "fail", "order_id": order_id, "item_id": item_id}
    await db.set(idempotency_key, json.dumps(result)) # No TTL, wait for confirm/clear_keys
    return result


async def handle_saga_compensate(order_id, item_id, quantity):
    """Idempotent stock compensation (rollback) for SAGA."""
    idempotency_key = f"op:compensate:{order_id}:{item_id}"
    cached = await db.get(idempotency_key)
    if cached is not None:
        return json.loads(cached)
    reserve_key = f"op:reserve:{order_id}:{item_id}"
    reserve_result = await db.get(reserve_key)
    if reserve_result is not None:
        reserve_data = json.loads(reserve_result)
        if reserve_data.get("status") == "ok":
            await _compensate_script(keys=[item_id], args=[quantity])
            await db.delete(reserve_key)
    result = {"status": "ok", "order_id": order_id, "item_id": item_id}
    await db.set(idempotency_key, json.dumps(result))
    return result

async def handle_saga_confirm(order_id, item_id, quantity):
    """Confirm stock consumption permanently (SAGA confirm)."""
    idempotency_key = f"op:confirm:{order_id}:{item_id}"
    cached = await db.get(idempotency_key)
    if cached is not None:
        return json.loads(cached)
    reserve_key = f"op:reserve:{order_id}:{item_id}"
    if await db.exists(reserve_key):
        await _confirm_script(keys=[item_id], args=[quantity])
        await db.delete(reserve_key)
    result = {"status": "ok", "order_id": order_id, "item_id": item_id}
    await db.set(idempotency_key, json.dumps(result))
    return result


async def handle_2pc_prepare(order_id, item_id, quantity):
    """2PC prepare: tentatively reserve stock."""
    lock_key = f"2pc:lock:{order_id}:{item_id}"
    acquired = await db.set(lock_key, quantity, nx=True)
    if not acquired:
        existing = await db.get(lock_key)
        if existing is not None:
            return {"status": "prepared", "order_id": order_id, "item_id": item_id}
        return {"status": "fail", "order_id": order_id, "item_id": item_id, "reason": "lock_conflict"}
    result_code = await _subtract_script(keys=[item_id], args=[quantity])
    if result_code != 1:
        await db.delete(lock_key)
        return {"status": "fail", "order_id": order_id, "item_id": item_id, "reason": "insufficient_stock"}
    return {"status": "prepared", "order_id": order_id, "item_id": item_id}


async def handle_2pc_commit(order_id, item_id, quantity):
    """2PC commit: release lock and confirm deduction."""
    lock_key = f"2pc:lock:{order_id}:{item_id}"
    if await db.exists(lock_key):
        await _confirm_script(keys=[item_id], args=[quantity])
        await db.delete(lock_key)
    return {"status": "committed", "order_id": order_id, "item_id": item_id}


async def handle_2pc_abort(order_id, item_id, quantity):
    """2PC abort: restore stock and release lock."""
    lock_key = f"2pc:lock:{order_id}:{item_id}"
    if await db.exists(lock_key):
        await _compensate_script(keys=[item_id], args=[quantity])
        await db.delete(lock_key)
    return {"status": "aborted", "order_id": order_id, "item_id": item_id}


async def handle_clear_keys(order_id, item_id):
    """Clear idempotency keys so an order can be retried."""
    for key in [f"op:reserve:{order_id}:{item_id}", f"op:compensate:{order_id}:{item_id}", f"op:confirm:{order_id}:{item_id}"]:
        await db.delete(key)
    return {"status": "ok", "order_id": order_id}


# ==========================================
#   STREAM CONSUMER (async background task)
# ==========================================

HANDLERS = {
    'reserve': lambda d: handle_saga_reserve(d['order_id'], d['item_id'], d['quantity']),
    'compensate': lambda d: handle_saga_compensate(d['order_id'], d['item_id'], d['quantity']),
    'confirm': lambda d: handle_saga_confirm(d['order_id'], d['item_id'], d['quantity']),
    'prepare': lambda d: handle_2pc_prepare(d['order_id'], d['item_id'], d['quantity']),
    'commit': lambda d: handle_2pc_commit(d['order_id'], d['item_id'], d['quantity']),
    'abort': lambda d: handle_2pc_abort(d['order_id'], d['item_id'], d['quantity']),
    'clear_keys': lambda d: handle_clear_keys(d['order_id'], d['item_id']),
}


async def process_message(msg_id, data):
    """Route a stream message to the right handler."""
    parsed = {
        'cmd': data.get(b'cmd', b'').decode(),
        'order_id': data.get(b'order_id', b'').decode(),
        'item_id': data.get(b'item_id', b'').decode(),
        'quantity': int(data.get(b'quantity', b'0')),
    }
    handler = HANDLERS.get(parsed['cmd'])
    if handler:
        result = await handler(parsed)
    else:
        app.logger.warning(f"Unknown command: {parsed['cmd']}")
        result = {"status": "error", "reason": f"unknown command: {parsed['cmd']}"}

    response_key = f"response:{parsed['order_id']}:stock"
    await db.lpush(response_key, json.dumps(result))
    await db.expire(response_key, 60)
    await db.xack(STREAM_NAME, GROUP_NAME, msg_id)


AUTOCLAIM_IDLE_MS = 30_000  # Claim messages idle for >30s from dead consumers
AUTOCLAIM_INTERVAL = 15     # Run autoclaim every 15 iterations (~75s)


async def consumer_loop():
    """Background task: consume from stock-commands stream with XAUTOCLAIM."""
    try:
        await db.xgroup_create(STREAM_NAME, GROUP_NAME, id='0', mkstream=True)
    except aioredis.ResponseError as e:
        if "BUSYGROUP" not in str(e):
            raise
    app.logger.info(f"Stock consumer started: {CONSUMER_NAME}")

    # Process own pending messages first (from previous crash)
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
            # Periodically claim orphaned messages from dead consumers
            iteration += 1
            if iteration % AUTOCLAIM_INTERVAL == 0:
                try:
                    result = await db.xautoclaim(
                        STREAM_NAME, GROUP_NAME, CONSUMER_NAME,
                        min_idle_time=AUTOCLAIM_IDLE_MS, start_id='0-0', count=10
                    )
                    # result = (next_start_id, [(msg_id, data), ...], deleted_ids)
                    claimed = result[1] if len(result) > 1 else []
                    for msg_id, data in claimed:
                        if data:  # Skip deleted messages
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
    global _subtract_script, _direct_subtract_script, _confirm_script, _compensate_script, _consumer_task
    _subtract_script = db.register_script(SUBTRACT_LUA)
    _direct_subtract_script = db.register_script(DIRECT_SUBTRACT_LUA)
    _confirm_script = db.register_script(CONFIRM_LUA)
    _compensate_script = db.register_script(COMPENSATE_LUA)
    _consumer_task = asyncio.create_task(consumer_loop())
    # Cleanup stale 2PC locks: REMOVED since locks shouldn't expire if they hold reserved stock.


@app.after_serving
async def shutdown():
    if _consumer_task:
        _consumer_task.cancel()
        try:
            await _consumer_task
        except asyncio.CancelledError:
            pass
    await db.aclose()
