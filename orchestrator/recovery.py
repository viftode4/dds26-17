import asyncio

import redis.asyncio as aioredis
from msgspec import msgpack

from common.logging import get_logger
from orchestrator.wal import WALEngine
from orchestrator.executor import COMMAND_STREAMS, OutboxReader

logger = get_logger("orchestrator.recovery")

# Stream consumer groups for XAUTOCLAIM
CONSUMER_GROUPS = {
    "stock": ("stock-commands", "stock-workers"),
    "payment": ("payment-commands", "payment-workers"),
}

MAX_RETRIES = 5
IDLE_THRESHOLD_MS = 15000  # 15 seconds

RECONCILIATION_INTERVAL = 60   # seconds
ORPHAN_SAGA_TIMEOUT = 120       # seconds


class RecoveryWorker:
    """Handles crash recovery for the orchestrator (leader-only background tasks).

    On leadership acquisition:
    1. Scan WAL for incomplete sagas and resume/abort
    2. Periodically XAUTOCLAIM stuck consumer-group messages
    3. Periodically verify data invariants (reconciliation)
    4. Periodically abort orphaned sagas > ORPHAN_SAGA_TIMEOUT

    Checkout processing no longer runs here — both order instances execute
    sagas directly (active-active). This worker is maintenance-only.
    """

    def __init__(self, wal: WALEngine, service_dbs: dict[str, aioredis.Redis],
                 outbox_reader: OutboxReader):
        self.wal = wal
        self.service_dbs = service_dbs
        self.outbox_reader = outbox_reader
        self._claim_task: asyncio.Task | None = None
        self._reconciliation_task: asyncio.Task | None = None

    async def recover_incomplete_sagas(self):
        """Scan WAL and handle incomplete sagas after leadership acquisition."""
        incomplete = await self.wal.get_incomplete_sagas()
        if not incomplete:
            logger.info("No incomplete sagas found in WAL")
            return

        logger.info("Found incomplete sagas, recovering", count=len(incomplete))

        for saga_id, state in incomplete.items():
            last_step = state["last_step"]
            try:
                await self._recover_saga(saga_id, last_step, state.get("data", {}))
            except Exception as e:
                logger.error("Recovery failed for saga", saga_id=saga_id, error=str(e))
                await self.wal.log(saga_id, "FAILED")

    async def _recover_saga(self, saga_id: str, last_step: str, data: dict):
        """Resume a single saga based on its last WAL state."""
        logger.info("Recovering saga", saga_id=saga_id, last_step=last_step)

        if last_step == "STARTED":
            # Never reached prepare — saga context is in WAL data, abort
            await self.wal.log(saga_id, "FAILED")

        elif last_step == "PREPARING":
            # 2PC: sent prepares but didn't collect all votes — abort all
            await self._abort_all(saga_id, data)

        elif last_step == "TRYING":
            # Saga: sent try_reserve to all services in parallel — outcome unknown, abort all
            # Lua cancel is idempotent: safe even if reservation was never made
            await self._abort_all(saga_id, data)

        elif last_step in ("COMMITTING", "CONFIRMING"):
            # Was in commit phase — retry confirms (all Lua ops are idempotent)
            await self._confirm_all(saga_id, data)

        elif last_step in ("ABORTING", "COMPENSATING"):
            # Was in abort phase — retry cancels (idempotent)
            await self._abort_all(saga_id, data)

        else:
            logger.warning("Unknown WAL state for saga", saga_id=saga_id, last_step=last_step)
            await self.wal.log(saga_id, "FAILED")

    def _encode_items(self, data: dict) -> str:
        """Encode items list from WAL data as msgpack for stream commands."""
        items = data.get("items", [])
        if not items:
            return ""
        # Items may be list[list] after msgpack round-trip — that's fine,
        # stock service decodes with type=list[tuple[str,int]] which coerces it
        return msgpack.encode(items).decode("latin-1")

    async def _abort_all(self, saga_id: str, data: dict):
        """Send cancel to all services (idempotent)."""
        items_encoded = self._encode_items(data)
        for service, db in self.service_dbs.items():
            stream = COMMAND_STREAMS[service]
            cmd = {"saga_id": saga_id, "action": "cancel"}
            if service == "stock" and items_encoded:
                cmd["items"] = items_encoded
            elif service == "payment":
                cmd["user_id"] = data.get("user_id", "")
                cmd["amount"] = str(data.get("total_cost", 0))
            await db.xadd(stream, cmd, maxlen=10000, approximate=True)
        await self.wal.log(saga_id, "FAILED")

    async def _confirm_all(self, saga_id: str, data: dict):
        """Send confirm to all services (idempotent retry)."""
        items_encoded = self._encode_items(data)
        for service, db in self.service_dbs.items():
            stream = COMMAND_STREAMS[service]
            cmd = {"saga_id": saga_id, "action": "confirm"}
            if service == "stock" and items_encoded:
                cmd["items"] = items_encoded
            elif service == "payment":
                cmd["user_id"] = data.get("user_id", "")
                cmd["amount"] = str(data.get("total_cost", 0))
            await db.xadd(stream, cmd, maxlen=10000, approximate=True)
        await self.wal.log(saga_id, "COMPLETED")

    async def _cancel_stock(self, saga_id: str, data: dict):
        """Send cancel to stock service only."""
        db = self.service_dbs.get("stock")
        if not db:
            return
        items_encoded = self._encode_items(data)
        cmd = {"saga_id": saga_id, "action": "cancel"}
        if items_encoded:
            cmd["items"] = items_encoded
        await db.xadd(COMMAND_STREAMS["stock"], cmd, maxlen=10000, approximate=True)

    async def _cancel_payment(self, saga_id: str, data: dict):
        """Send cancel to payment service only."""
        db = self.service_dbs.get("payment")
        if not db:
            return
        await db.xadd(COMMAND_STREAMS["payment"], {
            "saga_id": saga_id,
            "action": "cancel",
            "user_id": data.get("user_id", ""),
        }, maxlen=10000, approximate=True)

    async def start_claim_worker(self):
        """Start periodic XAUTOCLAIM worker for stuck messages."""
        self._claim_task = asyncio.create_task(self._claim_loop())

    async def start_reconciliation(self):
        """Start periodic reconciliation worker (leader only)."""
        if self._reconciliation_task is not None:
            return
        self._reconciliation_task = asyncio.create_task(self._reconciliation_loop())
        logger.info("Reconciliation worker started")

    async def stop(self):
        """Stop all recovery workers."""
        for task in (self._claim_task, self._reconciliation_task):
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._claim_task = None
        self._reconciliation_task = None

    async def _claim_loop(self):
        """Periodically reclaim stuck messages in consumer groups."""
        while True:
            try:
                await asyncio.sleep(10)
                for service, (stream, group) in CONSUMER_GROUPS.items():
                    db = self.service_dbs.get(service)
                    if not db:
                        continue
                    try:
                        claimed = await db.xautoclaim(
                            stream, group, "recovery-worker",
                            min_idle_time=IDLE_THRESHOLD_MS,
                            start_id="0-0",
                        )
                        if claimed and claimed[1]:
                            logger.info(
                                "XAUTOCLAIM reclaimed messages",
                                count=len(claimed[1]),
                                stream=stream,
                            )
                    except aioredis.ResponseError:
                        pass  # Consumer group may not exist yet
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("XAUTOCLAIM error", error=str(e))
                await asyncio.sleep(5)

    async def _reconciliation_loop(self):
        """Periodically check data invariants and abort orphaned sagas."""
        while True:
            try:
                await asyncio.sleep(RECONCILIATION_INTERVAL)
                await self._reconcile()
                await self._abort_orphaned_sagas()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("Reconciliation error", error=str(e))
                await asyncio.sleep(10)

    async def _reconcile(self):
        """Verify data invariants across stock and payment services."""
        stock_db = self.service_dbs.get("stock")
        if stock_db:
            cursor = "0"
            while True:
                cursor, keys = await stock_db.scan(cursor=cursor, match="item:*", count=100)
                for key in keys:
                    data = await stock_db.hgetall(key)
                    avail = int(data.get("available_stock", 0))
                    reserved = int(data.get("reserved_stock", 0))
                    if avail < 0 or reserved < 0:
                        logger.critical(
                            "INVARIANT VIOLATION: stock",
                            key=key, available_stock=avail, reserved_stock=reserved,
                        )
                if cursor == "0" or cursor == 0:
                    break

        payment_db = self.service_dbs.get("payment")
        if payment_db:
            cursor = "0"
            while True:
                cursor, keys = await payment_db.scan(cursor=cursor, match="user:*", count=100)
                for key in keys:
                    data = await payment_db.hgetall(key)
                    avail = int(data.get("available_credit", 0))
                    held = int(data.get("held_credit", 0))
                    if avail < 0 or held < 0:
                        logger.critical(
                            "INVARIANT VIOLATION: payment",
                            key=key, available_credit=avail, held_credit=held,
                        )
                if cursor == "0" or cursor == 0:
                    break

    async def _abort_orphaned_sagas(self):
        """Find and abort sagas stuck for longer than ORPHAN_SAGA_TIMEOUT."""
        import time
        incomplete = await self.wal.get_incomplete_sagas()
        now = time.time()
        for saga_id, state in incomplete.items():
            data = state.get("data", {}) or {}
            msg_id = state.get("msg_id", "0-0")
            try:
                ts_ms = int(msg_id.split("-")[0])
                age = now - (ts_ms / 1000.0)
            except (ValueError, IndexError):
                age = 0

            if age > ORPHAN_SAGA_TIMEOUT:
                logger.warning("Aborting orphaned saga", saga_id=saga_id, age_seconds=age)
                await self._abort_all(saga_id, data)
