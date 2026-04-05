import asyncio
import os

import structlog

import redis.asyncio as aioredis
from redis.asyncio.cluster import RedisCluster, ClusterNode
from redis.exceptions import ConnectionError, TimeoutError

logger = structlog.get_logger(__name__)

_MASTER_POOL = int(os.environ.get("REDIS_MASTER_POOL_SIZE", "512"))
_REPLICA_POOL = int(os.environ.get("REDIS_REPLICA_POOL_SIZE", "256"))


def get_cluster_nodes(env_var: str) -> list[tuple[str, int]]:
    """Parse a comma-separated host:port list from an env var.

    E.g. ORDER_CLUSTER_NODES=order-cluster-1:6379,order-cluster-2:6379
    Returns list of (host, port) tuples. Falls back to [("localhost", 6379)] if unset.
    """
    raw = os.environ.get(env_var, "")
    if not raw:
        return [("localhost", 6379)]
    nodes = []
    for entry in raw.split(","):
        entry = entry.strip()
        if ":" in entry:
            host, port = entry.rsplit(":", 1)
            nodes.append((host, int(port)))
    return nodes or [("localhost", 6379)]


def create_redis_cluster_connection(
    startup_nodes: list[tuple[str, int]],
    pool_size: int = 512,
    read_from_replicas: bool = False,
    **kwargs,
) -> RedisCluster:
    """Create an async RedisCluster connection.

    Args:
        startup_nodes: List of (host, port) seed nodes. RedisCluster will
            discover the full topology from these.
        pool_size: Max connections **per cluster node** (not total).
        read_from_replicas: If True, read commands are routed to replicas.
        **kwargs: Extra kwargs passed to RedisCluster (e.g. decode_responses).
    """
    default_kwargs = {
        "socket_timeout": 5,
        "socket_connect_timeout": 5,
        "health_check_interval": 30,
        # retry_on_error intentionally omitted — redis-py internal retries
        # cause late Lua executions after the orchestrator has already
        # aborted/compensated, leading to conservation violations.
        "decode_responses": True,
    }
    merged = {**default_kwargs, **kwargs}
    nodes = [ClusterNode(host, port) for host, port in startup_nodes]
    return RedisCluster(
        startup_nodes=nodes,
        max_connections=pool_size,
        read_from_replicas=read_from_replicas,
        password=os.environ.get("REDIS_PASSWORD", "redis"),
        **merged,
    )


def create_redis_pubsub_connection(startup_nodes: list[tuple[str, int]]) -> aioredis.Redis:
    """Standalone single-node Redis connection for pub/sub.

    RedisCluster does not expose pubsub(). Redis Cluster gossips PUBLISH
    messages to all nodes via the cluster bus, so subscribing on any one
    node receives all publishes cluster-wide.
    """
    host, port = startup_nodes[0]
    return aioredis.Redis(
        host=host,
        port=port,
        password=os.environ.get("REDIS_PASSWORD", "redis"),
        decode_responses=True,
        socket_timeout=5,
        socket_connect_timeout=5,
    )


async def wait_for_redis(db: RedisCluster, name: str = "Redis",
                         retries: int = 30, delay: float = 1.0):
    """Wait for the Redis cluster to become available with retry + backoff."""
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
