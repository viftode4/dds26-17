import asyncio
import json

import redis.asyncio as aioredis


async def wait_for_result(db: aioredis.Redis, saga_id: str,
                          timeout: float = 30.0,
                          pubsub_db: aioredis.Redis | None = None) -> dict:
    """Wait for saga result using pub/sub + key fallback.

    pubsub_db must be a plain redis.asyncio.Redis connection (not RedisCluster)
    since RedisCluster does not support pubsub(). Falls back to a single
    blocking wait if pubsub_db is not provided.
    """
    result_key = f"saga-result:{saga_id}"

    # Fast path: result already available
    raw = await db.get(result_key)
    if raw:
        return json.loads(raw)

    if pubsub_db is None:
        await asyncio.sleep(timeout)
        raw = await db.get(result_key)
        return json.loads(raw) if raw else {"status": "failed", "error": "timeout"}

    pubsub = pubsub_db.pubsub()
    await pubsub.subscribe(f"saga-notify:{saga_id}")
    try:
        # Check again after subscribe to close the race window
        raw = await db.get(result_key)
        if raw:
            return json.loads(raw)

        try:
            async with asyncio.timeout(timeout):
                while True:
                    msg = await pubsub.get_message(
                        ignore_subscribe_messages=True, timeout=1.0
                    )
                    if msg:
                        raw = await db.get(result_key)
                        if raw:
                            return json.loads(raw)
        except asyncio.TimeoutError:
            pass

        # Final fallback check
        raw = await db.get(result_key)
        if raw:
            return json.loads(raw)
        return {"status": "failed", "error": "timeout"}
    finally:
        await pubsub.unsubscribe()
        await pubsub.aclose()
