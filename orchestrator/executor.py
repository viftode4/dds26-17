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
    """Issue WAITAOF on each db in parallel — best-effort AOF durability check.

    10ms budget: fast enough to not dominate latency, still catches the common
    case where the AOF buffer was just flushed. Falls back to appendfsync everysec
    guarantee if the budget expires. Non-fatal on any error.
    """
    async def _one(db):
        try:
            await db.execute_command("WAITAOF", 1, 0, 10)
        except Exception:
            pass

    await asyncio.gather(*[_one(db) for db in dbs])


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

        # Phase 1: Create all waiters, then send all prepare commands in parallel
        prepare_items: list[tuple[Step, asyncio.Future]] = [
            (step, self.outbox_reader.create_waiter(saga_id, step.service))
            for step in tx_def.steps
        ]
        await asyncio.gather(*[
            self._send_command(step, saga_id, "try_reserve", context)
            for step, _ in prepare_items
        ])

        # Collect all votes in parallel
        raw = await asyncio.gather(
            *[asyncio.wait_for(fut, timeout=STEP_TIMEOUT) for _, fut in prepare_items],
            return_exceptions=True,
        )
        votes: list[tuple[Step, dict]] = []
        for (step, _), result in zip(prepare_items, raw):
            if isinstance(result, Exception):
                self.circuit_breakers.get(step.service, CircuitBreaker()).record_failure()
                votes.append((step, {"event": "failed", "reason": "timeout"}))
            else:
                votes.append((step, result))

        all_reserved = all(v[1].get("event") == "reserved" for v in votes)

        if all_reserved:
            # Phase 2: COMMIT — send all confirms in parallel, wait in parallel
            await self.wal.log(saga_id, "COMMITTING")
            confirm_items: list[tuple[Step, asyncio.Future]] = [
                (step, self.outbox_reader.create_waiter(saga_id, step.service))
                for step in tx_def.steps
            ]
            await asyncio.gather(*[
                self._send_command(step, saga_id, "confirm", context)
                for step, _ in confirm_items
            ])
            confirm_raw = await asyncio.gather(
                *[asyncio.wait_for(fut, timeout=STEP_TIMEOUT) for _, fut in confirm_items],
                return_exceptions=True,
            )
            for (step, _), result in zip(confirm_items, confirm_raw):
                cb = self.circuit_breakers.get(step.service, CircuitBreaker())
                if isinstance(result, Exception):
                    logger.error("2PC commit timeout", saga_id=saga_id, step=step.name)
                    cb.record_failure()
                else:
                    cb.record_success()

            await _waitaof(list(self.service_dbs.values()))
            await self.wal.log(saga_id, "COMPLETED")
            return {"status": "success"}
        else:
            # Phase 2: ABORT — cancel all in parallel
            await self.wal.log(saga_id, "ABORTING")
            abort_items: list[tuple[Step, asyncio.Future]] = [
                (step, self.outbox_reader.create_waiter(saga_id, step.service))
                for step in tx_def.steps
            ]
            await asyncio.gather(*[
                self._send_command(step, saga_id, "cancel", context)
                for step, _ in abort_items
            ])
            await asyncio.gather(
                *[asyncio.wait_for(fut, timeout=STEP_TIMEOUT) for _, fut in abort_items],
                return_exceptions=True,
            )

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

    Parallel try: send try_reserve to all services simultaneously, collect votes.
    On all-reserved: confirm all in parallel.
    On any failure: compensate all in parallel (Lua cancel is idempotent).

    WAL states: TRYING → CONFIRMING/COMPENSATING → COMPLETED/FAILED
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
        """Execute the full Saga+TCC protocol with parallel try phase."""
        # Check all circuit breakers upfront
        for step in tx_def.steps:
            cb = self.circuit_breakers.get(step.service)
            if cb and cb.is_open():
                logger.warning("Circuit breaker open — fast fail", service=step.service, saga_id=saga_id)
                await self.wal.log(saga_id, "FAILED")
                return {"status": "failed", "error": f"service_{step.service}_unavailable"}

        # Try phase: send all try_reserve in parallel
        await self.wal.log(saga_id, "TRYING")
        try_items: list[tuple[Step, asyncio.Future]] = [
            (step, self.outbox_reader.create_waiter(saga_id, step.service))
            for step in tx_def.steps
        ]
        await asyncio.gather(*[
            self._send_command(step, saga_id, "try_reserve", context)
            for step, _ in try_items
        ])

        raw = await asyncio.gather(
            *[asyncio.wait_for(fut, timeout=STEP_TIMEOUT) for _, fut in try_items],
            return_exceptions=True,
        )
        votes: list[tuple[Step, dict]] = []
        for (step, _), result in zip(try_items, raw):
            if isinstance(result, Exception):
                self.circuit_breakers.get(step.service, CircuitBreaker()).record_failure()
                votes.append((step, {"event": "failed", "reason": "timeout"}))
            else:
                votes.append((step, result))

        all_reserved = all(v[1].get("event") == "reserved" for v in votes)

        if not all_reserved:
            # Compensate ALL participants — Lua cancel is idempotent for un-reserved items
            await self.wal.log(saga_id, "COMPENSATING")
            cancel_items: list[tuple[Step, asyncio.Future]] = [
                (step, self.outbox_reader.create_waiter(saga_id, step.service))
                for step in tx_def.steps
            ]
            await asyncio.gather(*[
                self._send_command(step, saga_id, "cancel", context)
                for step, _ in cancel_items
            ])
            await asyncio.gather(
                *[asyncio.wait_for(fut, timeout=STEP_TIMEOUT) for _, fut in cancel_items],
                return_exceptions=True,
            )
            await self.wal.log(saga_id, "FAILED")
            reason = "insufficient_resources"
            for _, vote in votes:
                if vote.get("event") == "failed":
                    reason = vote.get("reason", reason)
                    break
            return {"status": "failed", "error": reason}

        # All reserved — confirm all in parallel
        await self.wal.log(saga_id, "CONFIRMING")
        confirm_items: list[tuple[Step, asyncio.Future]] = [
            (step, self.outbox_reader.create_waiter(saga_id, step.service))
            for step in tx_def.steps
        ]
        await asyncio.gather(*[
            self._send_command(step, saga_id, "confirm", context)
            for step, _ in confirm_items
        ])
        confirm_raw = await asyncio.gather(
            *[asyncio.wait_for(fut, timeout=STEP_TIMEOUT) for _, fut in confirm_items],
            return_exceptions=True,
        )
        for (step, _), result in zip(confirm_items, confirm_raw):
            cb = self.circuit_breakers.get(step.service, CircuitBreaker())
            if isinstance(result, Exception):
                logger.error("Saga confirm timeout", saga_id=saga_id, step=step.name)
                cb.record_failure()
            else:
                cb.record_success()

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
