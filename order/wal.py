"""
Write-Ahead Log (WAL) for Saga state using Redis Streams.

Every saga state transition is logged BEFORE the action is taken.
On crash recovery, the WAL is replayed to determine where each saga
left off and what compensating actions are needed.

WAL entries are stored in the 'saga-wal' stream on order-db.
"""
import json
import logging

logger = logging.getLogger("order-service")

WAL_STREAM = "saga-wal"
WAL_TTL = 86400  # Entries auto-trimmed after 24h worth of entries


async def log_step(db, saga_id: str, step: str, **extra):
    """
    Write a WAL entry BEFORE taking the action, and maintain saga_state.
    """
    entry = {"saga_id": saga_id, "step": step}
    for k, v in extra.items():
        entry[k] = json.dumps(v) if isinstance(v, (list, dict)) else str(v)
        
    async with db.pipeline() as pipe:
        pipe.xadd(WAL_STREAM, entry, maxlen=50000)
        
        terminal_steps = {"completed", "failed", "abandoned", "recovered"}
        if step in terminal_steps:
            pipe.srem("active_sagas", saga_id)
            pipe.delete(f"saga_state:{saga_id}")
        else:
            pipe.sadd("active_sagas", saga_id)
            pipe.hset(f"saga_state:{saga_id}", mapping=entry)
            
        await pipe.execute()


async def get_saga_state(db, saga_id: str) -> dict | None:
    """
    Read the latest WAL entry for a given saga_id.
    """
    state = await db.hgetall(f"saga_state:{saga_id}")
    if state:
        return {k.decode(): v.decode() for k, v in state.items()}
    return None


async def get_incomplete_sagas(db) -> list[dict]:
    """
    Find all sagas that are NOT in a terminal state (completed/failed/abandoned).
    Uses the active_sagas set to avoid O(N) memory expansion over the entire WAL log.
    """
    incomplete = []
    cursor = 0
    while True:
        cursor, keys = await db.sscan("active_sagas", cursor, count=100)
        for saga_id in keys:
            if isinstance(saga_id, bytes): 
                saga_id = saga_id.decode()
            state = await db.hgetall(f"saga_state:{saga_id}")
            if state:
                incomplete.append({k.decode(): v.decode() for k, v in state.items()})
        if cursor == 0:
            break
            
    return incomplete
