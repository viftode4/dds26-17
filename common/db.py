import os
import redis.asyncio as aioredis


async def create_redis_connection(
    host: str | None = None,
    port: int | None = None,
    password: str | None = None,
    db: int | None = None,
) -> aioredis.Redis:
    """Create an async Redis connection using environment variables or explicit params."""
    return aioredis.Redis(
        host=host or os.environ.get("REDIS_HOST", "localhost"),
        port=port or int(os.environ.get("REDIS_PORT", 6379)),
        password=password or os.environ.get("REDIS_PASSWORD", "redis"),
        db=db or int(os.environ.get("REDIS_DB", 0)),
        decode_responses=True,
    )
