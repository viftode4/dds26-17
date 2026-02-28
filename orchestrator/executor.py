import asyncio
import time

import redis.asyncio as aioredis

import structlog
from orchestrator.definition import TransactionDefinition, Step
from orchestrator.wal import WALEngine

logger = structlog.get_logger("orchestrator.executor")

STEP_TIMEOUT = 10.0
CONFIRM_MAX_RETRIES = 3


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


class _BaseExecutor:
    """Shared infrastructure for TwoPCExecutor and SagaExecutor.

    Holds the common service connections, outbox reader, WAL engine,
    circuit breakers, and metrics — plus the generic helpers used by both
    protocol executors: _send_command, _retry_confirm, _broadcast, _inject_ttl.
    """

    def __init__(self, service_dbs: dict[str, aioredis.Redis],
                 outbox_reader: OutboxReader, wal: WALEngine,
                 circuit_breakers: dict[str, CircuitBreaker] | None = None,
                 metrics=None):
        self.service_dbs = service_dbs
        self.outbox_reader = outbox_reader
        self.wal = wal
        self.circuit_breakers = circuit_breakers or {}
        self.metrics = metrics

    async def _send_command(self, step: Step, saga_id: str, action: str, context: dict):
        """Send a command to a service's command stream — fully generic.

        Stream name derived by convention: ``{service}-commands``.
        Domain-specific payload fields injected via ``step.payload_builder``.
        """
        service = step.service
        db = self.service_dbs[service]
        stream = f"{service}-commands"
        cmd = {"saga_id": saga_id, "action": action}
        if step.payload_builder:
            cmd.update(step.payload_builder(saga_id, action, context))
        await db.xadd(stream, cmd, maxlen=10000, approximate=True)

    async def _retry_confirm(self, step: Step, saga_id: str, context: dict,
                              retries: int = CONFIRM_MAX_RETRIES) -> dict | None:
        """Forward recovery: retry confirm before giving up (reservation_expired is transient).

        Uses exponential backoff: 0.5s, 1s, 2s between attempts.
        Returns the result dict on success, or None if all retries exhausted.
        """
        for attempt in range(retries):
            fut = self.outbox_reader.create_waiter(saga_id, step.service)
            await self._send_command(step, saga_id, "confirm", context)
            try:
                result = await asyncio.wait_for(fut, timeout=STEP_TIMEOUT)
                if result.get("event") != "confirm_failed":
                    return result
            except asyncio.TimeoutError:
                pass
            await asyncio.sleep(0.5 * (2 ** attempt))  # 0.5s, 1s, 2s
        return None  # exhausted retries

    async def _direct_broadcast(
        self, action: str, saga_id: str, steps: list[Step], context: dict
    ) -> list[tuple[Step, dict | BaseException]]:
        """Call ``direct_executor`` on each step in parallel — bypasses streams."""
        results = await asyncio.gather(*[
            step.direct_executor(saga_id, action, context, self.service_dbs[step.service])
            for step in steps
        ], return_exceptions=True)
        return list(zip(steps, results))

    async def _broadcast(
        self, action: str, saga_id: str, steps: list[Step], context: dict
    ) -> list[tuple[Step, dict | BaseException]]:
        """Send ``action`` to all steps in parallel and collect outbox responses.

        Returns ``(step, result)`` pairs where *result* is the event dict on
        success, or a ``BaseException`` (e.g. ``TimeoutError``) if the step
        didn't respond within ``STEP_TIMEOUT``.

        When all steps have a ``direct_executor``, bypasses streams entirely
        for a ~20-30ms latency saving per checkout.
        """
        if all(step.direct_executor for step in steps):
            return await self._direct_broadcast(action, saga_id, steps, context)
        pairs = [
            (step, self.outbox_reader.create_waiter(saga_id, step.service))
            for step in steps
        ]
        await asyncio.gather(*[
            self._send_command(step, saga_id, action, context)
            for step, _ in pairs
        ])
        raw = await asyncio.gather(
            *[asyncio.wait_for(fut, timeout=STEP_TIMEOUT) for _, fut in pairs],
            return_exceptions=True,
        )
        return [(step, result) for (step, _), result in zip(pairs, raw)]

    async def _fire_and_forget(
        self, action: str, saga_id: str, steps: list[Step], context: dict
    ) -> None:
        """Send ``action`` to all steps in parallel — do NOT wait for responses.

        Used for the presumed-commit optimisation: once every participant has
        reserved successfully, the confirms are guaranteed to succeed (Lua is
        idempotent, TTL >> confirm latency). The WAL stays in CONFIRMING so
        the recovery worker will complete any that don't land.

        When all steps have a ``direct_executor``, fires them directly as
        background tasks (still fire-and-forget semantics).
        """
        if all(step.direct_executor for step in steps):
            for step in steps:
                asyncio.create_task(
                    step.direct_executor(saga_id, action, context, self.service_dbs[step.service])
                )
            return
        await asyncio.gather(*[
            self._send_command(step, saga_id, action, context)
            for step in steps
        ])

    def _inject_ttl(self, context: dict) -> None:
        """Compute adaptive reservation TTL from p99 latency and store in context."""
        p99_ms = self.metrics.get_percentile(99) if self.metrics else 5000.0
        context["_reservation_ttl"] = max(30, min(120, int(p99_ms / 1000 * 3) + 10))


class TwoPCExecutor(_BaseExecutor):
    """Two-Phase Commit executor.

    Phase 1: Send prepare to ALL participants in parallel, collect votes.
    Phase 2a (all commit): Confirm all in parallel.
    Phase 2b (any abort): Cancel all in parallel.
    """

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

        asyncio.create_task(self.wal.log(saga_id, "PREPARING"))
        self._inject_ttl(context)

        # Phase 1: prepare all participants in parallel, collect votes
        votes: list[tuple[Step, dict]] = []
        for step, result in await self._broadcast("try_reserve", saga_id, tx_def.steps, context):
            if isinstance(result, Exception):
                self.circuit_breakers.get(step.service, CircuitBreaker()).record_failure()
                votes.append((step, {"event": "failed", "reason": "timeout"}))
            else:
                votes.append((step, result))

        all_reserved = all(v[1].get("event") == "reserved" for v in votes)

        if all_reserved:
            # Phase 2: COMMIT — presumed commit optimisation.
            # All participants reserved → confirms are guaranteed to succeed
            # (Lua idempotent, TTL >> confirm latency). Fire confirms without
            # waiting for responses. WAL stays COMMITTING so recovery worker
            # will complete any stragglers.
            await self.wal.log(saga_id, "COMMITTING")
            await self._fire_and_forget("confirm", saga_id, tx_def.steps, context)
            for step in tx_def.steps:
                self.circuit_breakers.get(step.service, CircuitBreaker()).record_success()
            return {"status": "success"}
        else:
            # Phase 2: ABORT — cancel all in parallel
            asyncio.create_task(self.wal.log(saga_id, "ABORTING"))
            await self._broadcast("cancel", saga_id, tx_def.steps, context)
            await self.wal.log(saga_id, "FAILED")
            reason = "insufficient_resources"
            for _, vote in votes:
                if vote.get("event") == "failed":
                    reason = vote.get("reason", reason)
                    break
            return {"status": "failed", "error": reason}


class SagaExecutor(_BaseExecutor):
    """Saga + TCC protocol executor.

    Parallel try: send try_reserve to all services simultaneously, collect votes.
    On all-reserved: confirm all in parallel.
    On any failure: compensate all in parallel (Lua cancel is idempotent).

    WAL states: TRYING → CONFIRMING/COMPENSATING → COMPLETED/FAILED
    """

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

        self._inject_ttl(context)

        # Try phase: send all try_reserve in parallel, collect votes
        asyncio.create_task(self.wal.log(saga_id, "TRYING"))
        votes: list[tuple[Step, dict]] = []
        for step, result in await self._broadcast("try_reserve", saga_id, tx_def.steps, context):
            if isinstance(result, Exception):
                self.circuit_breakers.get(step.service, CircuitBreaker()).record_failure()
                votes.append((step, {"event": "failed", "reason": "timeout"}))
            else:
                votes.append((step, result))

        all_reserved = all(v[1].get("event") == "reserved" for v in votes)

        if not all_reserved:
            # Compensate ALL participants — Lua cancel is idempotent for un-reserved items
            asyncio.create_task(self.wal.log(saga_id, "COMPENSATING"))
            await self._broadcast("cancel", saga_id, tx_def.steps, context)
            await self.wal.log(saga_id, "FAILED")
            reason = "insufficient_resources"
            for _, vote in votes:
                if vote.get("event") == "failed":
                    reason = vote.get("reason", reason)
                    break
            return {"status": "failed", "error": reason}

        # All reserved — presumed commit: fire confirms, don't wait for responses.
        # Lua confirm is idempotent, TTL >> confirm latency. WAL stays
        # CONFIRMING so recovery worker completes any stragglers.
        await self.wal.log(saga_id, "CONFIRMING")
        await self._fire_and_forget("confirm", saga_id, tx_def.steps, context)
        for step in tx_def.steps:
            self.circuit_breakers.get(step.service, CircuitBreaker()).record_success()
        return {"status": "success"}
