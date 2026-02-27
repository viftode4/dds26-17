import asyncio
import json

import redis.asyncio as aioredis


async def wait_for_result(db: aioredis.Redis, saga_id: str,
                          timeout: float = 30.0) -> dict:
    """Wait for saga result using pub/sub + key pattern.

    1. Check if result already exists (fast path)
    2. Subscribe to notification channel
    3. Check again (prevent race between GET and SUBSCRIBE)
    4. Block on notification with timeout
    5. Fall back to key check on timeout
    """
    result_key = f"saga-result:{saga_id}"

    # Fast path: result already available
    raw = await db.get(result_key)
    if raw:
        return json.loads(raw)

    # Subscribe BEFORE checking key (prevents race condition)
    pubsub = db.pubsub()
    await pubsub.subscribe(f"saga-notify:{saga_id}")

    try:
        # Check again after subscribe
        raw = await db.get(result_key)
        if raw:
            return json.loads(raw)

        # Block on notification
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
