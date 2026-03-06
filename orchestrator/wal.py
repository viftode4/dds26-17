import json
import time

import redis.asyncio as aioredis


class WALEngine:
    """Write-Ahead Log for saga state transitions.

    Every state transition is logged BEFORE the action is taken.
    Enables crash recovery by replaying incomplete sagas.

    Uses a dual-structure approach:
    - Stream (saga-wal): append-only audit trail, bounded by MAX_LEN
    - SET (active_sagas) + HASH (saga_state:{id}): O(1) lookup for recovery
      instead of O(n) stream scan
    """

    STREAM_KEY = "saga-wal"
    ACTIVE_SET = "active_sagas"
    MAX_LEN = 50000
    TERMINAL_STATES = frozenset({"COMPLETED", "FAILED", "ABANDONED"})

    def __init__(self, db: aioredis.Redis):
        self.db = db

    async def log(self, saga_id: str, step: str, data: dict | None = None):
        """Log a state transition to the WAL stream + index."""
        entry = {
            "saga_id": saga_id,
            "step": step,
            "timestamp": str(time.time()),
        }
        if data:
            entry["data"] = json.dumps(data)

        async with self.db.pipeline(transaction=False) as pipe:
            pipe.xadd(self.STREAM_KEY, entry, maxlen=self.MAX_LEN, approximate=True)
            if step in self.TERMINAL_STATES:
                pipe.srem(self.ACTIVE_SET, saga_id)
                pipe.delete(f"saga_state:{saga_id}")
            else:
                pipe.sadd(self.ACTIVE_SET, saga_id)
                pipe.hset(f"saga_state:{saga_id}", mapping=entry)
            await pipe.execute()

    async def get_incomplete_sagas(self) -> dict[str, dict]:
        """Find all sagas not in a terminal state.

        Uses the active_sagas SET for O(1) membership + per-saga HASH
        for state lookup. No full stream scan required.
        """
        incomplete: dict[str, dict] = {}
        cursor = "0"
        while True:
            cursor, saga_ids = await self.db.sscan(
                self.ACTIVE_SET, cursor=cursor, count=100
            )
            for saga_id in saga_ids:
                state = await self.db.hgetall(f"saga_state:{saga_id}")
                if not state:
                    # Stale entry in SET — clean up
                    await self.db.srem(self.ACTIVE_SET, saga_id)
                    continue
                step = state.get("step", "")
                if step in self.TERMINAL_STATES:
                    # Terminal but not yet cleaned — clean up
                    await self.db.srem(self.ACTIVE_SET, saga_id)
                    await self.db.delete(f"saga_state:{saga_id}")
                    continue
                data = None
                raw_data = state.get("data")
                if raw_data:
                    try:
                        data = json.loads(raw_data)
                    except Exception:
                        data = {}
                incomplete[saga_id] = {
                    "saga_id": saga_id,
                    "last_step": step,
                    "data": data,
                    "created_at": float(state.get("timestamp", 0)),
                }
            if cursor == "0" or cursor == 0:
                break
        return incomplete
