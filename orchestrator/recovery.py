import asyncio
import time
from typing import Callable

import structlog
from common.dlq import write_to_dlq
from orchestrator.definition import TransactionDefinition
from orchestrator.wal import WALEngine
from orchestrator.executor import STEP_TIMEOUT

logger = structlog.get_logger("orchestrator.recovery")

IDLE_THRESHOLD_MS = 15000  # 15 seconds

RECONCILIATION_INTERVAL = 60   # seconds
ORPHAN_SAGA_TIMEOUT = 120       # seconds
MAX_RECOVERY_RETRIES = 30       # ~10 min with exponential backoff capped at 60s


class RecoveryWorker:
    """Handles crash recovery for the orchestrator (leader-only background tasks).

    On leadership acquisition:
    1. Scan WAL for incomplete sagas and resume/abort/compensate
    2. Periodically verify data invariants (reconciliation) via injectable callback
    3. Periodically handle orphaned sagas > ORPHAN_SAGA_TIMEOUT

    Checkout processing no longer runs here — both order instances execute
    sagas directly (active-active). This worker is maintenance-only.
    """

    def __init__(self, wal: WALEngine, transport,
                 definitions: dict[str, TransactionDefinition] | None = None,
                 reconcile_fn: Callable[..., object] | None = None):
        self.wal = wal
        self.transport = transport
        self.definitions = definitions or {}
        self.reconcile_fn = reconcile_fn
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

        if last_step == "PREPARING":
            # 2PC: sent prepares but crashed before commit/abort decision
            # Safe default: abort (release locks, no mutations were applied)
            await self._abort_all(saga_id, data)

        elif last_step == "COMMITTING":
            # 2PC: commit decision made, MUST complete (irrevocable)
            # NEVER fall back to abort — that would cause data loss
            await self._commit_all(saga_id, data)

        elif last_step == "ABORTING":
            # 2PC: abort decision made, release locks (idempotent)
            await self._abort_all(saga_id, data)

        elif last_step == "EXECUTING":
            # Saga: was executing steps sequentially, crashed mid-way
            # We don't know which steps completed, compensate ALL (idempotent)
            await self._compensate_all(saga_id, data)

        elif last_step == "COMPENSATING":
            # Saga: was compensating, retry compensation
            await self._compensate_all(saga_id, data)

        else:
            logger.warning("Unknown WAL state", saga_id=saga_id, last_step=last_step)
            await self.wal.log(saga_id, "FAILED")

    async def _send_action_to_all(self, action: str, saga_id: str, data: dict):
        """Send ``action`` to all services via transport."""
        tx_def = self.definitions.get(data.get("tx_name", ""))
        if tx_def:
            for step in tx_def.steps:
                payload = {"saga_id": saga_id, "action": action}
                if step.payload_builder:
                    payload.update(step.payload_builder(saga_id, action, data))
                try:
                    await self.transport.send_and_wait(step.service, action, payload, STEP_TIMEOUT)
                except Exception as e:
                    logger.warning("send_action_to_all error", service=step.service, error=str(e))

    async def _abort_all(self, saga_id: str, data: dict):
        """Abort all services with retry up to MAX_RECOVERY_RETRIES, then log FAILED."""
        tx_def = self.definitions.get(data.get("tx_name", ""))
        if not tx_def:
            await self._send_action_to_all("abort", saga_id, data)
            await self.wal.log(saga_id, "FAILED")
            return

        backoff = 0.5
        max_backoff = 60.0
        attempt = 0
        pending_steps = list(tx_def.steps)

        while pending_steps and attempt < MAX_RECOVERY_RETRIES:
            attempt += 1
            still_pending = []
            for step in pending_steps:
                try:
                    payload = {"saga_id": saga_id, "action": "abort"}
                    if step.payload_builder:
                        payload.update(step.payload_builder(saga_id, "abort", data))
                    result = await self.transport.send_and_wait(step.service, "abort", payload, STEP_TIMEOUT)
                except Exception as e:
                    logger.warning("Recovery abort error", service=step.service,
                                   saga_id=saga_id, error=str(e))
                    still_pending.append(step)
                    continue

                if isinstance(result, dict) and result.get("event") == "aborted":
                    pass  # success
                else:
                    still_pending.append(step)

            pending_steps = still_pending
            if pending_steps:
                logger.warning("Recovery abort retry", saga_id=saga_id,
                               pending=[s.name for s in pending_steps],
                               backoff=backoff, attempt=attempt)
                await asyncio.sleep(min(backoff, max_backoff))
                backoff *= 2

        if pending_steps:
            logger.error("Recovery abort exhausted retries, sending to DLQ",
                         saga_id=saga_id, steps=[s.name for s in pending_steps])
            for step in pending_steps:
                await write_to_dlq(self.wal.db, saga_id, "abort", step.name,
                                   "recovery_retries_exhausted", attempt, data)

        await self.wal.log(saga_id, "FAILED")

    async def _commit_all(self, saga_id: str, data: dict):
        """Commit all services with retry up to MAX_RECOVERY_RETRIES.

        Commits are irrevocable — if retries exhaust, DLQ entries are flagged
        as requiring manual resolution. The saga is still logged COMPLETED
        (optimistic) since prepares already reserved the resources.
        """
        tx_def = self.definitions.get(data.get("tx_name", ""))
        if not tx_def:
            await self._send_action_to_all("commit", saga_id, data)
            await self.wal.log(saga_id, "COMPLETED")
            return

        backoff = 0.5
        max_backoff = 60.0
        attempt = 0
        pending_steps = list(tx_def.steps)

        while pending_steps and attempt < MAX_RECOVERY_RETRIES:
            attempt += 1
            still_pending = []
            for step in pending_steps:
                try:
                    payload = {"saga_id": saga_id, "action": "commit"}
                    if step.payload_builder:
                        payload.update(step.payload_builder(saga_id, "commit", data))
                    result = await self.transport.send_and_wait(step.service, "commit", payload, STEP_TIMEOUT)
                except Exception as e:
                    logger.warning("Recovery commit error", service=step.service,
                                   saga_id=saga_id, error=str(e))
                    still_pending.append(step)
                    continue

                if isinstance(result, dict) and result.get("event") == "committed":
                    pass  # success
                else:
                    still_pending.append(step)

            pending_steps = still_pending
            if pending_steps:
                logger.warning("Recovery commit retry", saga_id=saga_id,
                               pending=[s.name for s in pending_steps],
                               backoff=backoff, attempt=attempt)
                await asyncio.sleep(min(backoff, max_backoff))
                backoff *= 2

        if pending_steps:
            logger.error("Recovery commit exhausted retries, sending to DLQ (MANUAL RESOLUTION REQUIRED)",
                         saga_id=saga_id, steps=[s.name for s in pending_steps])
            for step in pending_steps:
                ctx = {**data, "requires_manual_resolution": True}
                await write_to_dlq(self.wal.db, saga_id, "commit", step.name,
                                   "recovery_retries_exhausted", attempt, ctx)

        await self.wal.log(saga_id, "COMPLETED")

    async def _compensate_all(self, saga_id: str, data: dict):
        """Compensate completed saga steps in reverse order with bounded retry."""
        tx_def = self.definitions.get(data.get("tx_name", ""))
        if not tx_def:
            await self._send_action_to_all("compensate", saga_id, data)
            await self.wal.log(saga_id, "FAILED")
            return

        # Determine which steps to compensate
        completed_names = data.get("completed_steps")
        if completed_names:
            steps_to_compensate = [s for s in tx_def.steps if s.name in completed_names]
        else:
            # Unknown which completed — compensate all (idempotent)
            steps_to_compensate = list(tx_def.steps)

        # Compensate in reverse order, bounded retry per step
        for step in reversed(steps_to_compensate):
            backoff = 0.5
            max_backoff = 60.0
            attempt = 0
            succeeded = False
            while attempt < MAX_RECOVERY_RETRIES:
                attempt += 1
                try:
                    payload = {"saga_id": saga_id, "action": "compensate"}
                    if step.payload_builder:
                        payload.update(step.payload_builder(saga_id, "compensate", data))
                    result = await self.transport.send_and_wait(step.service, "compensate", payload, STEP_TIMEOUT)
                except Exception as e:
                    logger.warning("Recovery compensate error", service=step.service,
                                   saga_id=saga_id, error=str(e), attempt=attempt)
                    await asyncio.sleep(min(backoff, max_backoff))
                    backoff *= 2
                    continue

                if isinstance(result, dict) and result.get("event") == "compensated":
                    succeeded = True
                    break
                else:
                    logger.warning("Recovery compensate retry", step=step.name,
                                   saga_id=saga_id, result=result, attempt=attempt)
                    await asyncio.sleep(min(backoff, max_backoff))
                    backoff *= 2

            if not succeeded:
                logger.error("Recovery compensate exhausted retries, sending to DLQ",
                             saga_id=saga_id, step=step.name, attempts=attempt)
                await write_to_dlq(self.wal.db, saga_id, "compensate", step.name,
                                   "recovery_retries_exhausted", attempt, data)

        await self.wal.log(saga_id, "FAILED")

    async def start_reconciliation(self):
        """Start periodic reconciliation worker (leader only)."""
        if self._reconciliation_task is not None:
            return
        self._reconciliation_task = asyncio.create_task(self._reconciliation_loop())
        logger.info("Reconciliation worker started")

    async def stop(self):
        """Stop all recovery workers."""
        if self._reconciliation_task:
            self._reconciliation_task.cancel()
            try:
                await self._reconciliation_task
            except asyncio.CancelledError:
                pass
        self._reconciliation_task = None

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
            result = self.reconcile_fn()
            if asyncio.iscoroutine(result):
                await result

    async def _abort_orphaned_sagas(self):
        """Find and handle sagas stuck for longer than ORPHAN_SAGA_TIMEOUT."""
        incomplete = await self.wal.get_incomplete_sagas()
        now = time.time()
        for saga_id, state in incomplete.items():
            data = state.get("data", {}) or {}
            created_at = state.get("created_at", 0)
            age = now - created_at if created_at else 0

            if age > ORPHAN_SAGA_TIMEOUT:
                last_step = state.get("last_step", "")
                if last_step == "COMMITTING":
                    logger.warning("Completing orphaned committed saga", saga_id=saga_id)
                    await self._commit_all(saga_id, data)
                else:
                    logger.warning("Aborting orphaned saga", saga_id=saga_id, age_seconds=age)
                    await self._abort_all(saga_id, data)
