import json
import time

import redis.asyncio as aioredis


class WALEngine:
    """Write-Ahead Log for saga state transitions.

    Every state transition is logged BEFORE the action is taken.
    Enables crash recovery by replaying incomplete sagas.
    """

    STREAM_KEY = "saga-wal"
    MAX_LEN = 50000

    def __init__(self, db: aioredis.Redis):
        self.db = db

    async def log(self, saga_id: str, step: str, data: dict | None = None):
        """Log a state transition to the WAL stream."""
        entry = {
            "saga_id": saga_id,
            "step": step,
            "timestamp": str(time.time()),
        }
        if data:
            entry["data"] = json.dumps(data)
        await self.db.xadd(self.STREAM_KEY, entry, maxlen=self.MAX_LEN, approximate=True)

    async def get_incomplete_sagas(self) -> dict[str, dict]:
        """Scan WAL for sagas that didn't reach COMPLETED or FAILED.

        Returns dict mapping saga_id to their last WAL entry.
        """
        # Read all WAL entries (bounded by MAX_LEN)
        entries = await self.db.xrange(self.STREAM_KEY, "-", "+")

        saga_states: dict[str, dict] = {}
        for msg_id, fields in entries:
            saga_id = fields["saga_id"]
            step = fields["step"]
            data = None
            if "data" in fields and fields["data"]:
                try:
                    data = json.loads(fields["data"])
                except Exception:
                    data = {}

            saga_states[saga_id] = {
                "saga_id": saga_id,
                "last_step": step,
                "data": data,
                "msg_id": msg_id,
            }

        # Filter to incomplete sagas (not COMPLETED or FAILED)
        terminal_states = {"COMPLETED", "FAILED", "ABANDONED"}
        incomplete = {
            sid: state for sid, state in saga_states.items()
            if state["last_step"] not in terminal_states
        }
        return incomplete
