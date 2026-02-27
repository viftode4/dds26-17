import asyncio
from typing import Callable

import redis.asyncio as aioredis

import structlog
from orchestrator.definition import TransactionDefinition
from orchestrator.wal import WALEngine
from orchestrator.executor import OutboxReader

logger = structlog.get_logger("orchestrator.recovery")

MAX_RETRIES = 5
IDLE_THRESHOLD_MS = 15000  # 15 seconds

RECONCILIATION_INTERVAL = 60   # seconds
ORPHAN_SAGA_TIMEOUT = 120       # seconds


class RecoveryWorker:
    """Handles crash recovery for the orchestrator (leader-only background tasks).

    On leadership acquisition:
    1. Scan WAL for incomplete sagas and resume/abort
    2. Periodically XAUTOCLAIM stuck consumer-group messages
    3. Periodically verify data invariants (reconciliation) via injectable callback
    4. Periodically abort orphaned sagas > ORPHAN_SAGA_TIMEOUT

    Checkout processing no longer runs here — both order instances execute
    sagas directly (active-active). This worker is maintenance-only.
    """

    def __init__(self, wal: WALEngine, service_dbs: dict[str, aioredis.Redis],
                 outbox_reader: OutboxReader,
                 definitions: dict[str, TransactionDefinition] | None = None,
                 reconcile_fn: Callable[..., object] | None = None):
        self.wal = wal
        self.service_dbs = service_dbs
        self.outbox_reader = outbox_reader
        self.definitions = definitions or {}
        self.reconcile_fn = reconcile_fn
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

    async def _abort_all(self, saga_id: str, data: dict):
        """Send cancel to all services using transaction definition's payload builders."""
        tx_name = data.get("tx_name", "")
        tx_def = self.definitions.get(tx_name)

        if tx_def:
            for step in tx_def.steps:
                db = self.service_dbs.get(step.service)
                if not db:
                    continue
                stream = f"{step.service}-commands"
                cmd = {"saga_id": saga_id, "action": "cancel"}
                if step.payload_builder:
                    cmd.update(step.payload_builder(saga_id, "cancel", data))
                await db.xadd(stream, cmd, maxlen=10000, approximate=True)
        else:
            # Fallback: send bare cancel to all known services
            for service, db in self.service_dbs.items():
                stream = f"{service}-commands"
                await db.xadd(stream, {"saga_id": saga_id, "action": "cancel"},
                              maxlen=10000, approximate=True)
        await self.wal.log(saga_id, "FAILED")

    async def _confirm_all(self, saga_id: str, data: dict):
        """Send confirm to all services using transaction definition's payload builders."""
        tx_name = data.get("tx_name", "")
        tx_def = self.definitions.get(tx_name)

        if tx_def:
            for step in tx_def.steps:
                db = self.service_dbs.get(step.service)
                if not db:
                    continue
                stream = f"{step.service}-commands"
                cmd = {"saga_id": saga_id, "action": "confirm"}
                if step.payload_builder:
                    cmd.update(step.payload_builder(saga_id, "confirm", data))
                await db.xadd(stream, cmd, maxlen=10000, approximate=True)
        else:
            # Fallback: send bare confirm to all known services
            for service, db in self.service_dbs.items():
                stream = f"{service}-commands"
                await db.xadd(stream, {"saga_id": saga_id, "action": "confirm"},
                              maxlen=10000, approximate=True)
        await self.wal.log(saga_id, "COMPLETED")

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
        """Periodically reclaim stuck messages in consumer groups.

        Stream/group names derived by convention: ``{service}-commands`` / ``{service}-workers``.
        """
        while True:
            try:
                await asyncio.sleep(10)
                for service in self.service_dbs:
                    db = self.service_dbs[service]
                    stream = f"{service}-commands"
                    group = f"{service}-workers"
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
        """Run application-provided reconciliation callback, if any."""
        if self.reconcile_fn:
            result = self.reconcile_fn(self.service_dbs)
            if asyncio.iscoroutine(result):
                await result

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
