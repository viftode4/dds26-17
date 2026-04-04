import asyncio
import os

import structlog

import redis.asyncio as aioredis
from redis.asyncio.sentinel import Sentinel
from redis.exceptions import ConnectionError, TimeoutError

logger = structlog.get_logger(__name__)

_MASTER_POOL = int(os.environ.get("REDIS_MASTER_POOL_SIZE", "512"))
_REPLICA_POOL = int(os.environ.get("REDIS_REPLICA_POOL_SIZE", "256"))


def get_redis_config(prefix: str = "") -> dict:
    """Get Redis connection config from environment variables.

    Args:
        prefix: Optional prefix for env vars (e.g., "STOCK_" for STOCK_REDIS_HOST).
    """
    return {
        "host": os.environ.get(f"{prefix}REDIS_HOST", "localhost"),
        "port": int(os.environ.get(f"{prefix}REDIS_PORT", 6379)),
        "password": os.environ.get(f"{prefix}REDIS_PASSWORD", "redis"),
        "db": int(os.environ.get(f"{prefix}REDIS_DB", 0)),
    }


def get_sentinel_hosts() -> list[tuple[str, int]] | None:
    """Parse SENTINEL_HOSTS env var. Returns list of (host, port) or None."""
    raw = os.environ.get("SENTINEL_HOSTS", "")
    if not raw:
        return None
    hosts = []
    for entry in raw.split(","):
        entry = entry.strip()
        if ":" in entry:
            host, port = entry.rsplit(":", 1)
            hosts.append((host, int(port)))
    return hosts or None


def create_redis_connection(
    sentinel_service_name: str | None = None,
    prefix: str = "",
    **kwargs,
) -> aioredis.Redis:
    """Create a Redis connection, using Sentinel if configured.

    Args:
        sentinel_service_name: Sentinel service name (e.g., "order-master").
            If None, reads from {prefix}REDIS_SENTINEL_SERVICE env var.
        prefix: Env var prefix for direct connection fallback.
        **kwargs: Extra kwargs passed to Redis client (e.g., decode_responses).
    """
    # Default timeout/retry kwargs
    default_kwargs = {
        "socket_timeout": 5,
        "socket_connect_timeout": 5,
        "health_check_interval": 30,
        # retry_on_error intentionally omitted — redis-py internal retries
        # cause late Lua executions after the orchestrator has already
        # aborted/compensated, leading to conservation violations.
    }
    # Merge with caller's kwargs (caller can override)
    merged = {**default_kwargs, **kwargs}

    sentinel_hosts = get_sentinel_hosts()

    if sentinel_hosts:
        if sentinel_service_name is None:
            sentinel_service_name = os.environ.get(
                f"{prefix}REDIS_SENTINEL_SERVICE", ""
            )
        if not sentinel_service_name:
            # Fall back to direct connection
            config = get_redis_config(prefix)
            return aioredis.Redis(**config, max_connections=_MASTER_POOL, **merged)

        password = os.environ.get(f"{prefix}REDIS_PASSWORD", "redis")
        sentinel = Sentinel(
            sentinel_hosts,
            # sentinel_kwargs: auth for the sentinel nodes themselves (no password)
            sentinel_kwargs={},
            # password: auth for the actual Redis master/replica connections
            password=password,
            db=int(os.environ.get(f"{prefix}REDIS_DB", 0)),
            max_connections=_MASTER_POOL,
            **merged,
        )
        return sentinel.master_for(sentinel_service_name)

    # Direct connection fallback
    config = get_redis_config(prefix)
    return aioredis.Redis(**config, max_connections=_MASTER_POOL, **merged)


def create_replica_connection(
    sentinel_service_name: str | None = None,
    prefix: str = "",
    **kwargs,
) -> aioredis.Redis:
    """Create a Redis connection routed to a replica for read-only endpoints.

    Uses sentinel.slave_for() when Sentinel is configured, falling back
    to the master if no replica is available (transparent failover).
    Falls back to master connection if Sentinel is not configured.
    """
    # Default timeout/retry kwargs
    default_kwargs = {
        "socket_timeout": 5,
        "socket_connect_timeout": 5,
        "health_check_interval": 30,
        # retry_on_error intentionally omitted — redis-py internal retries
        # cause late Lua executions after the orchestrator has already
        # aborted/compensated, leading to conservation violations.
    }
    # Merge with caller's kwargs (caller can override)
    merged = {**default_kwargs, **kwargs}

    sentinel_hosts = get_sentinel_hosts()

    if sentinel_hosts:
        if sentinel_service_name is None:
            sentinel_service_name = os.environ.get(
                f"{prefix}REDIS_SENTINEL_SERVICE", ""
            )
        if sentinel_service_name:
            password = os.environ.get(f"{prefix}REDIS_PASSWORD", "redis")
            sentinel = Sentinel(
                sentinel_hosts,
                sentinel_kwargs={},
                password=password,
                db=int(os.environ.get(f"{prefix}REDIS_DB", 0)),
                max_connections=_REPLICA_POOL,
                **merged,
            )
            return sentinel.slave_for(sentinel_service_name)

    # No Sentinel or no service name — fall back to master
    return create_redis_connection(prefix=prefix, **merged)


async def wait_for_redis(db: aioredis.Redis, name: str = "Redis",
                         retries: int = 30, delay: float = 1.0):
    """Wait for Redis to become available with retry + backoff."""
    for attempt in range(1, retries + 1):
        try:
            await db.ping()
            logger.info("Redis ready", name=name, attempt=attempt)
            return
        except Exception as e:
            if attempt == retries:
                raise RuntimeError(
                    f"{name} not available after {retries} attempts: {e}"
                ) from e
            logger.warning("Redis not ready", name=name, attempt=attempt,
                           retries=retries, error=str(e))
            await asyncio.sleep(delay)


async def subscribe_failover_invalidation(
    *pools: aioredis.Redis,
    service_name: str = "",
) -> asyncio.Task | None:
    """Subscribe to Sentinel +switch-master events and invalidate connection pools.

    When Sentinel promotes a replica, all existing connections in the pool may
    still point to the demoted old master.  Disconnecting the pool forces
    redis-py to re-resolve the master via Sentinel on the next command.

    Returns the background task (caller should cancel it on shutdown) or None
    if Sentinel is not configured.
    """
    sentinel_hosts = get_sentinel_hosts()
    if not sentinel_hosts:
        return None

    import redis.asyncio as _aioredis

    async def _listener():
        while True:
            try:
                sentinel_conn = _aioredis.Redis(
                    host=sentinel_hosts[0][0],
                    port=sentinel_hosts[0][1],
                    decode_responses=True,
                )
                pubsub = sentinel_conn.pubsub()
                await pubsub.psubscribe("*")
                logger.info("Sentinel failover listener started",
                            service=service_name)

                async for message in pubsub.listen():
                    if message["type"] != "pmessage":
                        continue
                    channel = message.get("channel", "")
                    if "+switch-master" not in channel:
                        continue

                    data = message.get("data", "")
                    logger.warning("Sentinel failover detected, invalidating pools",
                                   service=service_name, switch_master=data)
                    for pool in pools:
                        try:
                            await pool.connection_pool.disconnect()
                        except Exception:
                            pass

            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("Sentinel listener error, reconnecting",
                               error=str(e))
                await asyncio.sleep(2)

    return asyncio.create_task(_listener())
