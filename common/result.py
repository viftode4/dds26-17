import asyncio
import json

import redis.asyncio as aioredis


async def wait_for_result(db: aioredis.Redis, saga_id: str,
                          timeout: float = 30.0) -> dict:
    """Wait for saga result by polling the result key.

    RedisCluster does not support pubsub() — polling is used instead.
    Poll interval starts at 50 ms and backs off to 500 ms to balance
    latency vs. Redis load on the (rare) cross-instance fallback path.
    """
    result_key = f"saga-result:{saga_id}"

    # Fast path: result already available
    raw = await db.get(result_key)
    if raw:
        return json.loads(raw)

    deadline = asyncio.get_event_loop().time() + timeout
    interval = 0.05  # start at 50 ms
    while asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(interval)
        raw = await db.get(result_key)
        if raw:
            return json.loads(raw)
        interval = min(interval * 1.5, 0.5)  # back off up to 500 ms

    return {"status": "failed", "error": "timeout"}
