import asyncio
import os
import socket

import redis.asyncio as aioredis

import structlog

logger = structlog.get_logger("orchestrator.leader")

# Atomic Lua scripts — eliminate the GET+EXPIRE and GET+DEL race windows
_RENEW_SCRIPT = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('EXPIRE', KEYS[1], ARGV[2])
else
    return 0
end
"""

_RELEASE_SCRIPT = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
else
    return 0
end
"""


class LeaderElection:
    """Redis-based leader election using SET NX + TTL.

    Heartbeat and release use atomic Lua scripts to eliminate the
    GET+EXPIRE / GET+DEL race condition (split-brain window).

    After the active-active redesign, the leader only runs background
    maintenance: XAUTOCLAIM, reconciliation, orphan saga abort.
    Checkout processing runs on ALL instances directly.
    """

    LOCK_KEY = "{order-wal}:leader"
    LOCK_TTL = 5  # seconds
    HEARTBEAT_INTERVAL = 2  # seconds (must be < LOCK_TTL)

    def __init__(self, db: aioredis.Redis):
        self.db = db
        self.instance_id = f"{socket.gethostname()}-{os.getpid()}"
        self._is_leader = False
        self._heartbeat_task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()

    @property
    def is_leader(self) -> bool:
        return self._is_leader

    async def acquire(self) -> bool:
        """Try to acquire leadership. Returns True if successful."""
        acquired = await self.db.set(
            self.LOCK_KEY, self.instance_id,
            nx=True, ex=self.LOCK_TTL,
        )
        if acquired:
            self._is_leader = True
            self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
            logger.info("Leadership acquired", instance_id=self.instance_id)
            return True
        return False

    async def wait_for_leadership(self) -> None:
        """Block until leadership is acquired."""
        while not self._stop_event.is_set():
            if await self.acquire():
                return
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                pass

    async def release(self):
        """Release leadership using atomic check-and-delete."""
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass
            self._heartbeat_task = None

        if self._is_leader:
            await self.db.eval(_RELEASE_SCRIPT, 1, self.LOCK_KEY, self.instance_id)
            self._is_leader = False
            logger.info("Leadership released", instance_id=self.instance_id)

    async def stop(self):
        """Stop the leader election (used during shutdown)."""
        self._stop_event.set()
        await self.release()

    async def _heartbeat_loop(self):
        """Renew lock TTL atomically — single Lua call prevents split-brain."""
        while True:
            try:
                await asyncio.sleep(self.HEARTBEAT_INTERVAL)
                result = await self.db.eval(
                    _RENEW_SCRIPT, 1,
                    self.LOCK_KEY, self.instance_id, str(self.LOCK_TTL),
                )
                if not result:
                    # Lua returned 0: key gone or owned by someone else
                    self._is_leader = False
                    logger.warning(
                        "Lost leadership (lock expired or stolen)",
                        instance_id=self.instance_id,
                    )
                    return
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("Heartbeat error", error=str(e))
                self._is_leader = False
                return
