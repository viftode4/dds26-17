import asyncio
import time

from msgspec import msgpack
import redis.asyncio as aioredis

from common.logging import get_logger
from orchestrator.definition import TransactionDefinition, Step
from orchestrator.wal import WALEngine

logger = get_logger("orchestrator.executor")

STEP_TIMEOUT = 10.0

# Stream names per service
COMMAND_STREAMS = {
    "stock": "stock-commands",
    "payment": "payment-commands",
}

OUTBOX_STREAMS = {
    "stock": "stock-outbox",
    "payment": "payment-outbox",
}


class CircuitBreaker:
    """Per-service circuit breaker — fail-fast when a service is degraded.

    States: closed (normal) → open (failing fast) → half-open (probing).
    Prevents cascading timeouts under partial failure scenarios.
    """

    def __init__(self, failure_threshold: int = 5, recovery_timeout: float = 30.0):
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self._failure_count = 0
        self._last_failure_time: float | None = None
        self._state = "closed"

    def record_failure(self):
        self._failure_count += 1
        self._last_failure_time = time.monotonic()
        if self._failure_count >= self.failure_threshold:
            if self._state != "open":
                logger.warning("Circuit breaker OPEN", failures=self._failure_count)
            self._state = "open"

    def record_success(self):
        if self._state == "half-open":
            logger.info("Circuit breaker CLOSED (recovered)")
        self._failure_count = 0
        self._state = "closed"

    def is_open(self) -> bool:
        if self._state == "closed":
            return False
        if self._state == "open":
            elapsed = time.monotonic() - (self._last_failure_time or 0)
            if elapsed >= self.recovery_timeout:
                self._state = "half-open"
                return False  # Allow one probe request through
            return True
        return False  # half-open: allow probe


class OutboxReader:
    """Reads outbox stream events and dispatches them to per-saga asyncio Futures."""

    def __init__(self):
        self._waiters: dict[str, asyncio.Future] = {}

    def create_waiter(self, saga_id: str, service: str) -> asyncio.Future:
        """Create a future that resolves when the outbox event for this step arrives."""
        key = f"{saga_id}:{service}"
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        self._waiters[key] = fut
        return fut

    def resolve(self, saga_id: str, service: str, event_data: dict):
        """Resolve a waiting future with the event data."""
        key = f"{saga_id}:{service}"
        fut = self._waiters.pop(key, None)
        if fut and not fut.done():
            fut.set_result(event_data)

    def cancel_all(self, saga_id: str):
        """Cancel all pending futures for a saga (cleanup on abort)."""
        to_remove = [k for k in self._waiters if k.startswith(f"{saga_id}:")]
        for key in to_remove:
            fut = self._waiters.pop(key, None)
            if fut and not fut.done():
                fut.cancel()


async def _waitaof(dbs: list[aioredis.Redis]):
    """Issue WAITAOF on each db to ensure writes are durable in local AOF.

    Called after the commit phase resolves — guarantees that confirmed
    state is synced to disk before marking the saga COMPLETED in WAL.
    200ms budget is well within STEP_TIMEOUT (10s).
    """
    for db in dbs:
        try:
            await db.execute_command("WAITAOF", 1, 0, 200)
        except Exception:
            pass  # Non-fatal — AOF everysec is still the floor guarantee


class TwoPCExecutor:
    """Two-Phase Commit executor.

    Phase 1: Send prepare to ALL participants in parallel, collect votes.
    Phase 2a (all commit): Confirm all in parallel.
    Phase 2b (any abort): Cancel all in parallel.
    """

    def __init__(self, service_dbs: dict[str, aioredis.Redis],
                 outbox_reader: OutboxReader, wal: WALEngine,
                 circuit_breakers: dict[str, CircuitBreaker] | None = None):
        self.service_dbs = service_dbs
        self.outbox_reader = outbox_reader
        self.wal = wal
        self.circuit_breakers = circuit_breakers or {}

    async def execute(self, tx_def: TransactionDefinition, saga_id: str,
                      context: dict) -> dict:
        """Execute the full 2PC protocol."""
        # Check circuit breakers before committing any resources
        for step in tx_def.steps:
            cb = self.circuit_breakers.get(step.service)
            if cb and cb.is_open():
                logger.warning("Circuit breaker open — fast fail", service=step.service, saga_id=saga_id)
                await self.wal.log(saga_id, "FAILED")
                return {"status": "failed", "error": f"service_{step.service}_unavailable"}

        await self.wal.log(saga_id, "PREPARING")

        # Phase 1: Send prepare to all participants in parallel
        prepare_futures = []
        for step in tx_def.steps:
            fut = self.outbox_reader.create_waiter(saga_id, step.service)
            await self._send_command(step, saga_id, "try_reserve", context)
            prepare_futures.append((step, fut))

        votes = []
        try:
            for step, fut in prepare_futures:
                result = await asyncio.wait_for(fut, timeout=STEP_TIMEOUT)
                votes.append((step, result))
        except asyncio.TimeoutError:
            logger.warning("2PC prepare timeout", saga_id=saga_id)
            if step:
                self.circuit_breakers.get(step.service, CircuitBreaker()).record_failure()
            votes.append((step, {"event": "failed", "reason": "timeout"}))

        all_reserved = all(v[1].get("event") == "reserved" for v in votes)

        if all_reserved:
            # Phase 2: COMMIT
            await self.wal.log(saga_id, "COMMITTING")
            confirm_futures = []
            for step in tx_def.steps:
                fut = self.outbox_reader.create_waiter(saga_id, step.service)
                await self._send_command(step, saga_id, "confirm", context)
                confirm_futures.append((step, fut))

            for step, fut in confirm_futures:
                try:
                    await asyncio.wait_for(fut, timeout=STEP_TIMEOUT)
                    self.circuit_breakers.get(step.service, CircuitBreaker()).record_success()
                except asyncio.TimeoutError:
                    logger.error("2PC commit timeout", saga_id=saga_id, step=step.name)
                    self.circuit_breakers.get(step.service, CircuitBreaker()).record_failure()

            # Ensure confirmed writes are durable before marking complete
            await _waitaof(list(self.service_dbs.values()))
            await self.wal.log(saga_id, "COMPLETED")
            return {"status": "success"}
        else:
            # Phase 2: ABORT
            await self.wal.log(saga_id, "ABORTING")
            abort_futures = []
            for step in tx_def.steps:
                fut = self.outbox_reader.create_waiter(saga_id, step.service)
                await self._send_command(step, saga_id, "cancel", context)
                abort_futures.append(fut)

            for fut in abort_futures:
                try:
                    await asyncio.wait_for(fut, timeout=STEP_TIMEOUT)
                except asyncio.TimeoutError:
                    pass

            await self.wal.log(saga_id, "FAILED")
            reason = "insufficient_resources"
            for _, vote in votes:
                if vote.get("event") == "failed":
                    reason = vote.get("reason", reason)
                    break
            return {"status": "failed", "error": reason}

    async def _send_command(self, step: Step, saga_id: str, action: str, context: dict):
        service = step.service
        db = self.service_dbs[service]
        stream = COMMAND_STREAMS[service]
        cmd = {"saga_id": saga_id, "action": action}
        if service == "stock":
            items = context.get("items", [])
            cmd["items"] = msgpack.encode(items).decode("latin-1")
        elif service == "payment":
            cmd["user_id"] = context.get("user_id", "")
            cmd["amount"] = str(context.get("total_cost", 0))
        await db.xadd(stream, cmd, maxlen=10000, approximate=True)


class SagaExecutor:
    """Saga + TCC protocol executor.

    Sequential: try stock → try payment → confirm both in parallel.
    On any failure: compensate completed steps in reverse order.
    """

    def __init__(self, service_dbs: dict[str, aioredis.Redis],
                 outbox_reader: OutboxReader, wal: WALEngine,
                 circuit_breakers: dict[str, CircuitBreaker] | None = None):
        self.service_dbs = service_dbs
        self.outbox_reader = outbox_reader
        self.wal = wal
        self.circuit_breakers = circuit_breakers or {}

    async def execute(self, tx_def: TransactionDefinition, saga_id: str,
                      context: dict) -> dict:
        """Execute the full Saga+TCC protocol."""
        completed_steps: list[Step] = []

        for step in tx_def.steps:
            # Check circuit breaker before trying this step
            cb = self.circuit_breakers.get(step.service)
            if cb and cb.is_open():
                logger.warning("Circuit breaker open — fast fail", service=step.service, saga_id=saga_id)
                # Compensate already-completed steps
                await self.wal.log(saga_id, "COMPENSATING")
                for prev_step in reversed(completed_steps):
                    comp_fut = self.outbox_reader.create_waiter(saga_id, prev_step.service)
                    await self._send_command(prev_step, saga_id, "cancel", context)
                    try:
                        await asyncio.wait_for(comp_fut, timeout=STEP_TIMEOUT)
                    except asyncio.TimeoutError:
                        pass
                await self.wal.log(saga_id, "FAILED")
                return {"status": "failed", "error": f"service_{step.service}_unavailable"}

            await self.wal.log(saga_id, f"{step.name}_TRYING")
            fut = self.outbox_reader.create_waiter(saga_id, step.service)
            await self._send_command(step, saga_id, "try_reserve", context)

            try:
                result = await asyncio.wait_for(fut, timeout=STEP_TIMEOUT)
            except asyncio.TimeoutError:
                logger.warning("Saga try timeout", step=step.name, saga_id=saga_id)
                self.circuit_breakers.get(step.service, CircuitBreaker()).record_failure()
                result = {"event": "failed", "reason": "timeout"}

            if result.get("event") != "reserved":
                await self.wal.log(saga_id, "COMPENSATING")
                for prev_step in reversed(completed_steps):
                    comp_fut = self.outbox_reader.create_waiter(saga_id, prev_step.service)
                    await self._send_command(prev_step, saga_id, "cancel", context)
                    try:
                        await asyncio.wait_for(comp_fut, timeout=STEP_TIMEOUT)
                    except asyncio.TimeoutError:
                        logger.error("Compensation timeout", step=prev_step.name, saga_id=saga_id)
                await self.wal.log(saga_id, "FAILED")
                return {"status": "failed", "error": result.get("reason", "insufficient_resources")}

            self.circuit_breakers.get(step.service, CircuitBreaker()).record_success()
            completed_steps.append(step)

        # All steps reserved — confirm all in parallel
        await self.wal.log(saga_id, "CONFIRMING")
        confirm_futures = []
        for step in tx_def.steps:
            fut = self.outbox_reader.create_waiter(saga_id, step.service)
            await self._send_command(step, saga_id, "confirm", context)
            confirm_futures.append((step, fut))

        for step, fut in confirm_futures:
            try:
                await asyncio.wait_for(fut, timeout=STEP_TIMEOUT)
                self.circuit_breakers.get(step.service, CircuitBreaker()).record_success()
            except asyncio.TimeoutError:
                logger.error("Saga confirm timeout", saga_id=saga_id)
                self.circuit_breakers.get(step.service, CircuitBreaker()).record_failure()

        # Ensure confirmed writes are durable before marking complete
        await _waitaof(list(self.service_dbs.values()))
        await self.wal.log(saga_id, "COMPLETED")
        return {"status": "success"}

    async def _send_command(self, step: Step, saga_id: str, action: str, context: dict):
        service = step.service
        db = self.service_dbs[service]
        stream = COMMAND_STREAMS[service]
        cmd = {"saga_id": saga_id, "action": action}
        if service == "stock":
            items = context.get("items", [])
            cmd["items"] = msgpack.encode(items).decode("latin-1")
        elif service == "payment":
            cmd["user_id"] = context.get("user_id", "")
            cmd["amount"] = str(context.get("total_cost", 0))
        await db.xadd(stream, cmd, maxlen=10000, approximate=True)
