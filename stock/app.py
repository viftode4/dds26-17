import asyncio
import os
import socket
import uuid
from pathlib import Path

import redis.asyncio as aioredis

from msgspec import msgpack
from quart import Quart, jsonify, abort, Response

from common.config import create_redis_connection, create_replica_connection, wait_for_redis
from common.logging import setup_logging, get_logger


DB_ERROR_STR = "DB error"
STREAM_COMMANDS = "stock-commands"
STREAM_OUTBOX = "stock-outbox"
CONSUMER_GROUP = "stock-workers"
CONSUMER_NAME = f"worker-{socket.gethostname()}-{os.getpid()}"
RESERVATION_TTL = 60  # 1 minute
MAX_RETRIES = 5
DLQ_STREAM = f"dead-letter:{STREAM_COMMANDS}"

app = Quart("stock-service")
log = get_logger("stock")

db: aioredis.Redis | None = None
db_read: aioredis.Redis | None = None  # Replica for read-only endpoints
_consumer_task: asyncio.Task | None = None


# ---- JSON error handlers ----

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
    global db, db_read, _consumer_task
    setup_logging("stock-service")

    db = create_redis_connection(prefix="", decode_responses=True)
    await wait_for_redis(db, "stock-db")

    # Replica connection for read-only find/list endpoints
    db_read = create_replica_connection(prefix="", decode_responses=True)

    # Load Lua function library
    lua_path = Path(__file__).parent / "lua" / "stock_lib.lua"
    lua_code = lua_path.read_text()
    try:
        await db.function_load(lua_code, replace=True)
    except aioredis.RedisError as e:
        log.error("Failed to load Lua library", error=str(e))
        raise

    # Create consumer group (idempotent — safe for multiple instances)
    try:
        await db.xgroup_create(STREAM_COMMANDS, CONSUMER_GROUP, id="0", mkstream=True)
    except aioredis.ResponseError as e:
        if "BUSYGROUP" not in str(e):
            raise

    _consumer_task = asyncio.create_task(consumer_loop())
    log.info("Stock service started", consumer=CONSUMER_NAME)


@app.after_serving
async def teardown():
    if _consumer_task:
        _consumer_task.cancel()
        try:
            await _consumer_task
        except asyncio.CancelledError:
            pass
    if db_read:
        await db_read.aclose()
    if db:
        await db.aclose()


async def sweep_dlq():
    """Move messages that exceeded MAX_RETRIES to the dead-letter queue."""
    try:
        pending = await db.xpending_range(
            STREAM_COMMANDS, CONSUMER_GROUP, "-", "+", count=100,
        )
        for entry in pending:
            if entry.get("times_delivered", 0) > MAX_RETRIES:
                msg_id = entry["message_id"]
                msgs = await db.xrange(STREAM_COMMANDS, msg_id, msg_id)
                if msgs:
                    _, fields = msgs[0]
                    fields["original_stream"] = STREAM_COMMANDS
                    fields["original_id"] = msg_id
                    fields["reason"] = "max_retries_exceeded"
                    await db.xadd(DLQ_STREAM, fields, maxlen=5000, approximate=True)
                await db.xack(STREAM_COMMANDS, CONSUMER_GROUP, msg_id)
                log.warning("Moved to DLQ", msg_id=msg_id)
    except Exception as e:
        log.error("DLQ sweep error", error=str(e))


async def consumer_loop():
    """Read commands from stock-commands stream and dispatch to Lua functions."""
    iteration = 0
    while True:
        try:
            messages = await db.xreadgroup(
                CONSUMER_GROUP, CONSUMER_NAME,
                {STREAM_COMMANDS: ">"},
                count=50, block=2000,
            )

            iteration += 1
            if iteration % 10 == 0:
                await sweep_dlq()

            if not messages:
                continue

            for _stream_name, entries in messages:
                for msg_id, fields in entries:
                    try:
                        await handle_command(fields)
                    except Exception as e:
                        log.error("Error processing message", msg_id=msg_id, error=str(e))
                    await db.xack(STREAM_COMMANDS, CONSUMER_GROUP, msg_id)

        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.error("Consumer loop error", error=str(e))
            await asyncio.sleep(1)


async def handle_command(fields: dict):
    """Dispatch a command to the appropriate Lua function."""
    action = fields.get("action", "")
    saga_id = fields.get("saga_id", "")

    if action == "try_reserve":
        items_raw = fields.get("items", "")
        items = msgpack.decode(items_raw.encode("latin-1"), type=list[tuple[str, int]])
        ttl = int(fields.get("ttl", RESERVATION_TTL))
        await _try_reserve(saga_id, items, ttl)
    elif action == "confirm":
        items_raw = fields.get("items", "")
        items = msgpack.decode(items_raw.encode("latin-1"), type=list[tuple[str, int]])
        await _confirm(saga_id, items)
    elif action == "cancel":
        items_raw = fields.get("items", "")
        if items_raw:
            items = msgpack.decode(items_raw.encode("latin-1"), type=list[tuple[str, int]])
        else:
            items = []
        await _cancel(saga_id, items)
    else:
        log.warning("Unknown action", action=action, saga_id=saga_id)


async def _try_reserve(saga_id: str, items: list[tuple[str, int]], ttl: int = RESERVATION_TTL):
    n = len(items)
    keys = [f"item:{item_id}" for item_id, _ in items]
    keys += [f"reservation:{saga_id}:{item_id}" for item_id, _ in items]
    keys += [STREAM_OUTBOX, f"saga:{saga_id}:stock:status"]
    args = [saga_id, str(ttl)]
    for item_id, amount in items:
        args += [item_id, str(amount)]
    await db.fcall("stock_try_reserve_batch", len(keys), *keys, *args)


async def _confirm(saga_id: str, items: list[tuple[str, int]]):
    n = len(items)
    keys = [f"item:{item_id}" for item_id, _ in items]
    keys += [f"reservation:{saga_id}:{item_id}" for item_id, _ in items]
    keys += [STREAM_OUTBOX, f"saga:{saga_id}:stock:status"]
    await db.fcall("stock_confirm_batch", len(keys), *keys, saga_id, str(n))


async def _cancel(saga_id: str, items: list[tuple[str, int]]):
    if not items:
        # Nothing to cancel — still publish cancelled event to unblock any waiters
        await db.xadd(STREAM_OUTBOX, {
            "saga_id": saga_id, "event": "cancelled", "reason": "no_items",
        }, maxlen=10000, approximate=True)
        return
    n = len(items)
    keys = [f"item:{item_id}" for item_id, _ in items]
    keys += [f"reservation:{saga_id}:{item_id}" for item_id, _ in items]
    keys += [STREAM_OUTBOX, f"saga:{saga_id}:stock:status"]
    await db.fcall("stock_cancel_batch", len(keys), *keys, saga_id, str(n))


# ============================================================================
# HTTP API endpoints
# ============================================================================

async def get_item_from_db(item_id: str, use_replica: bool = False) -> dict:
    key = f"item:{item_id}"
    conn = db_read if use_replica else db
    try:
        entry = await conn.hgetall(key)
    except Exception:
        try:
            entry = await db.hgetall(key)
        except aioredis.RedisError:
            abort(400, DB_ERROR_STR)
    if not entry:
        abort(400, f"Item: {item_id} not found!")
    return entry


@app.post('/item/create/<price>')
async def create_item(price: int):
    key = str(uuid.uuid4())
    try:
        await db.hset(f"item:{key}", mapping={
            "available_stock": 0,
            "reserved_stock": 0,
            "price": int(price),
        })
    except aioredis.RedisError:
        abort(400, DB_ERROR_STR)
    return jsonify({'item_id': key})


@app.post('/batch_init/<n>/<starting_stock>/<item_price>')
async def batch_init_users(n: int, starting_stock: int, item_price: int):
    n = int(n)
    starting_stock = int(starting_stock)
    item_price = int(item_price)
    try:
        async with db.pipeline(transaction=False) as pipe:
            for i in range(n):
                pipe.hset(f"item:{i}", mapping={
                    "available_stock": starting_stock,
                    "reserved_stock": 0,
                    "price": item_price,
                })
            await pipe.execute()
    except aioredis.RedisError:
        abort(400, DB_ERROR_STR)
    return jsonify({"msg": "Batch init for stock successful"})


@app.get('/find/<item_id>')
async def find_item(item_id: str):
    # Read-only: use replica for reduced master load
    entry = await get_item_from_db(item_id, use_replica=True)
    available = int(entry["available_stock"])
    reserved = int(entry["reserved_stock"])
    return jsonify({
        "stock": available + reserved,
        "price": int(entry["price"]),
    })


@app.post('/add/<item_id>/<amount>')
async def add_stock(item_id: str, amount: int):
    amount = int(amount)
    key = f"item:{item_id}"
    try:
        new_stock = await db.fcall("stock_add_direct", 1, key, amount)
    except aioredis.ResponseError as e:
        if "ITEM_NOT_FOUND" in str(e):
            abort(400, f"Item: {item_id} not found!")
        abort(400, DB_ERROR_STR)
    except aioredis.RedisError:
        abort(400, DB_ERROR_STR)
    return Response(f"Item: {item_id} stock updated to: {new_stock}", status=200)


@app.post('/subtract/<item_id>/<amount>')
async def remove_stock(item_id: str, amount: int):
    amount = int(amount)
    key = f"item:{item_id}"
    try:
        new_stock = await db.fcall("stock_subtract_direct", 1, key, amount)
    except aioredis.ResponseError as e:
        err_msg = str(e)
        if "ITEM_NOT_FOUND" in err_msg:
            abort(400, f"Item: {item_id} not found!")
        if "INSUFFICIENT_STOCK" in err_msg:
            abort(400, f"Item: {item_id} stock cannot get reduced below zero!")
        abort(400, DB_ERROR_STR)
    except aioredis.RedisError:
        abort(400, DB_ERROR_STR)
    return Response(f"Item: {item_id} stock updated to: {new_stock}", status=200)


@app.get('/health')
async def health():
    try:
        await db.ping()
    except Exception:
        abort(503, "Redis unavailable")
    return jsonify({"status": "healthy"})


if __name__ == '__main__':
    app.run(host="0.0.0.0", port=8000, debug=True)
