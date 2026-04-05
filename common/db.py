import os

from redis.asyncio.cluster import RedisCluster, ClusterNode


async def create_redis_connection(
    host: str | None = None,
    port: int | None = None,
    password: str | None = None,
    db: int | None = None,
) -> RedisCluster:
    """Create an async RedisCluster connection using environment variables or explicit params.

    Used primarily by tests. Connects to a single-node cluster (or standalone
    Redis with cluster mode enabled) using the provided or env-var coordinates.
    """
    _host = host or os.environ.get("REDIS_HOST", "localhost")
    _port = port or int(os.environ.get("REDIS_PORT", 6379))
    _password = password or os.environ.get("REDIS_PASSWORD", "redis")
    return RedisCluster(
        startup_nodes=[ClusterNode(_host, _port)],
        password=_password,
        decode_responses=True,
    )
