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
    Write a WAL entry BEFORE taking the action.

    Args:
        db: order-db Redis connection
        saga_id: order_id serving as saga identifier
        step: state machine step name
        **extra: additional fields (item_id, items, reason, etc.)
    """
    entry = {"saga_id": saga_id, "step": step}
    for k, v in extra.items():
        entry[k] = json.dumps(v) if isinstance(v, (list, dict)) else str(v)
    await db.xadd(WAL_STREAM, entry, maxlen=50000)


async def get_saga_state(db, saga_id: str) -> dict | None:
    """
    Read the latest WAL entry for a given saga_id.
    Returns the parsed entry dict or None if no entries found.
    """
    # Scan backwards through the WAL to find this saga's last entry.
    # For efficiency we read recent entries and filter.
    # In production, a secondary index (saga_id -> last_msg_id) would be better.
    entries = await db.xrevrange(WAL_STREAM, count=500)
    for msg_id, data in entries:
        sid = data.get(b'saga_id', b'').decode()
        if sid == saga_id:
            return {
                k.decode(): v.decode() for k, v in data.items()
            }
    return None


async def get_incomplete_sagas(db) -> list[dict]:
    """
    Find all sagas that are NOT in a terminal state (completed/failed/abandoned).
    Used during crash recovery to resume or compensate in-flight sagas.
    """
    terminal_steps = {"completed", "failed", "abandoned", "recovered"}
    # Track latest state per saga_id
    sagas = {}  # saga_id -> latest entry

    # Read entire WAL (bounded by maxlen)
    entries = await db.xrange(WAL_STREAM)
    for msg_id, data in entries:
        saga_id = data.get(b'saga_id', b'').decode()
        step = data.get(b'step', b'').decode()
        sagas[saga_id] = {k.decode(): v.decode() for k, v in data.items()}

    # Filter to non-terminal
    return [
        entry for saga_id, entry in sagas.items()
        if entry.get('step', '') not in terminal_steps
    ]
