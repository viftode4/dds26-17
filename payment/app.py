import asyncio
import os
import socket
import uuid
from pathlib import Path

import redis.asyncio as aioredis

from quart import Quart, jsonify, abort, Response

from common.config import create_redis_connection, create_replica_connection, wait_for_redis
from common.logging import setup_logging, get_logger


DB_ERROR_STR = "DB error"
STREAM_COMMANDS = "payment-commands"
STREAM_OUTBOX = "payment-outbox"
CONSUMER_GROUP = "payment-workers"
CONSUMER_NAME = f"worker-{socket.gethostname()}-{os.getpid()}"
RESERVATION_TTL = 60  # 1 minute
MAX_RETRIES = 5
DLQ_STREAM = f"dead-letter:{STREAM_COMMANDS}"

app = Quart("payment-service")
log = get_logger("payment")

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
    setup_logging("payment-service")

    db = create_redis_connection(prefix="", decode_responses=True)
    await wait_for_redis(db, "payment-db")

    # Replica connection for read-only find endpoints
    db_read = create_replica_connection(prefix="", decode_responses=True)

    # Load Lua function library
    lua_path = Path(__file__).parent / "lua" / "payment_lib.lua"
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
    log.info("Payment service started", consumer=CONSUMER_NAME)


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
    """Read commands from payment-commands stream and dispatch to Lua functions."""
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
    user_id = fields.get("user_id", "")
    amount = fields.get("amount", "0")

    if action == "try_reserve":
        ttl = int(fields.get("ttl", RESERVATION_TTL))
        await _try_reserve(saga_id, user_id, int(amount), ttl)
    elif action == "confirm":
        await _confirm(saga_id, user_id)
    elif action == "cancel":
        await _cancel(saga_id, user_id)
    else:
        log.warning("Unknown action", action=action, saga_id=saga_id)


async def _try_reserve(saga_id: str, user_id: str, amount: int, ttl: int = RESERVATION_TTL):
    keys = [
        f"user:{user_id}",
        f"reservation:{saga_id}:{user_id}",
        f"saga:{saga_id}:payment:status",
        STREAM_OUTBOX,
    ]
    args = [str(amount), saga_id, user_id, str(ttl)]
    await db.fcall("payment_try_reserve", len(keys), *keys, *args)


async def _confirm(saga_id: str, user_id: str):
    keys = [
        f"user:{user_id}",
        f"reservation:{saga_id}:{user_id}",
        f"saga:{saga_id}:payment:status",
        STREAM_OUTBOX,
    ]
    await db.fcall("payment_confirm", len(keys), *keys, saga_id)


async def _cancel(saga_id: str, user_id: str):
    if not user_id:
        # Nothing to cancel — publish cancelled event to unblock any waiters
        await db.xadd(STREAM_OUTBOX, {
            "saga_id": saga_id, "event": "cancelled", "reason": "no_user",
        }, maxlen=10000, approximate=True)
        return
    keys = [
        f"user:{user_id}",
        f"reservation:{saga_id}:{user_id}",
        f"saga:{saga_id}:payment:status",
        STREAM_OUTBOX,
    ]
    await db.fcall("payment_cancel", len(keys), *keys, saga_id)


# ============================================================================
# HTTP API endpoints
# ============================================================================

async def get_user_from_db(user_id: str, use_replica: bool = False) -> dict:
    key = f"user:{user_id}"
    conn = db_read if use_replica else db
    try:
        entry = await conn.hgetall(key)
    except Exception:
        try:
            entry = await db.hgetall(key)
        except aioredis.RedisError:
            abort(400, DB_ERROR_STR)
    if not entry:
        abort(400, f"User: {user_id} not found!")
    return entry


@app.post('/create_user')
async def create_user():
    key = str(uuid.uuid4())
    try:
        await db.hset(f"user:{key}", mapping={
            "available_credit": 0,
            "held_credit": 0,
        })
    except aioredis.RedisError:
        abort(400, DB_ERROR_STR)
    return jsonify({'user_id': key})


@app.post('/batch_init/<n>/<starting_money>')
async def batch_init_users(n: int, starting_money: int):
    n = int(n)
    starting_money = int(starting_money)
    try:
        async with db.pipeline(transaction=False) as pipe:
            for i in range(n):
                pipe.hset(f"user:{i}", mapping={
                    "available_credit": starting_money,
                    "held_credit": 0,
                })
            await pipe.execute()
    except aioredis.RedisError:
        abort(400, DB_ERROR_STR)
    return jsonify({"msg": "Batch init for users successful"})


@app.get('/find_user/<user_id>')
async def find_user(user_id: str):
    # Read-only: use replica for reduced master load
    entry = await get_user_from_db(user_id, use_replica=True)
    available = int(entry["available_credit"])
    held = int(entry["held_credit"])
    return jsonify({
        "user_id": user_id,
        "credit": available + held,
    })


@app.post('/add_funds/<user_id>/<amount>')
async def add_credit(user_id: str, amount: int):
    amount = int(amount)
    key = f"user:{user_id}"
    try:
        new_credit = await db.fcall("payment_add_direct", 1, key, amount)
    except aioredis.ResponseError as e:
        if "USER_NOT_FOUND" in str(e):
            abort(400, f"User: {user_id} not found!")
        abort(400, DB_ERROR_STR)
    except aioredis.RedisError:
        abort(400, DB_ERROR_STR)
    return Response(f"User: {user_id} credit updated to: {new_credit}", status=200)


@app.post('/pay/<user_id>/<amount>')
async def remove_credit(user_id: str, amount: int):
    amount = int(amount)
    key = f"user:{user_id}"
    try:
        new_credit = await db.fcall("payment_subtract_direct", 1, key, amount)
    except aioredis.ResponseError as e:
        err_msg = str(e)
        if "USER_NOT_FOUND" in err_msg:
            abort(400, f"User: {user_id} not found!")
        if "INSUFFICIENT_CREDIT" in err_msg:
            abort(400, f"User: {user_id} credit cannot get reduced below zero!")
        abort(400, DB_ERROR_STR)
    except aioredis.RedisError:
        abort(400, DB_ERROR_STR)
    return Response(f"User: {user_id} credit updated to: {new_credit}", status=200)


@app.get('/health')
async def health():
    try:
        await db.ping()
    except Exception:
        abort(503, "Redis unavailable")
    return jsonify({"status": "healthy"})


if __name__ == '__main__':
    app.run(host="0.0.0.0", port=8000, debug=True)
