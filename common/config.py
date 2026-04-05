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
                         retries: int = 60, delay: float = 2.0):
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
            # Disconnect pool so the next attempt re-queries Sentinel for the
            # current master (handles Sentinel failover and IP reassignment).
            try:
                await db.connection_pool.disconnect()
            except Exception:
                pass
            await asyncio.sleep(delay)


async def subscribe_failover_invalidation(
    *pools: aioredis.Redis,
    service_name: str = "",
) -> asyncio.Task | None:
    """Subscribe to Sentinel +switch-master events and invalidate connection pools.

    Two complementary mechanisms run concurrently:

    1. PubSub listener (fast path): subscribes to Sentinel's +switch-master
       channel and disconnects pools immediately when a failover is published.

    2. Reconciler (slow path / fallback): every 10 s, queries Sentinel directly
       for the current master IP.  If the IP has changed since the last check,
       disconnects pools regardless of whether the PubSub event was received.
       This catches failovers that occur during the 2 s listener reconnect window
       (the race condition where +switch-master is published while the PubSub
       connection is being re-established after an error).

    Disconnecting the pool forces redis-py to re-resolve the master via Sentinel
    on the next command — the standard reconnection pattern for Sentinel clients.

    Returns the background task (caller should cancel it on shutdown) or None
    if Sentinel is not configured.
    """
    sentinel_hosts = get_sentinel_hosts()
    if not sentinel_hosts:
        return None

    import redis.asyncio as _aioredis

    async def _invalidate(reason: str) -> None:
        logger.warning("Sentinel failover detected, invalidating pools",
                       service=service_name, reason=reason)
        for pool in pools:
            try:
                await pool.connection_pool.disconnect()
            except Exception:
                pass

    async def _pubsub_listener():
        """Fast path: event-driven pool invalidation via Sentinel PubSub."""
        while True:
            try:
                sentinel_conn = _aioredis.Redis(
                    host=sentinel_hosts[0][0],
                    port=sentinel_hosts[0][1],
                    decode_responses=True,
                )
                pubsub = sentinel_conn.pubsub()
                await pubsub.psubscribe("*")
                logger.info("Sentinel failover listener started", service=service_name)

                async for message in pubsub.listen():
                    if message["type"] != "pmessage":
                        continue
                    if "+switch-master" not in message.get("channel", ""):
                        continue
                    await _invalidate(f"pubsub: {message.get('data', '')}")

            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("Sentinel listener error, reconnecting",
                               service=service_name, error=str(e))
                await asyncio.sleep(2)

    async def _reconciler():
        """Slow path: periodic Sentinel query to catch missed +switch-master events."""
        sentinel_svc = os.environ.get("REDIS_SENTINEL_SERVICE", "")
        if not sentinel_svc:
            return  # Cannot reconcile without knowing which service to monitor

        last_ip: str | None = None
        while True:
            await asyncio.sleep(10)
            try:
                for host, port in sentinel_hosts:
                    r = _aioredis.Redis(
                        host=host, port=port, decode_responses=True,
                        socket_connect_timeout=3, socket_timeout=3,
                    )
                    try:
                        result = await r.execute_command(
                            "SENTINEL", "get-master-addr-by-name", sentinel_svc
                        )
                    finally:
                        try:
                            await r.aclose()
                        except Exception:
                            pass
                    if result:
                        current_ip = result[0]
                        if last_ip is not None and current_ip != last_ip:
                            await _invalidate(
                                f"reconciler: master IP changed {last_ip} → {current_ip}"
                            )
                        last_ip = current_ip
                        break  # Got a valid response from this sentinel, no need to try others
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("Sentinel reconciler error",
                               service=service_name, error=str(e))

    async def _watcher():
        await asyncio.gather(_pubsub_listener(), _reconciler())

    return asyncio.create_task(_watcher())
