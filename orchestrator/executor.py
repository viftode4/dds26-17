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

    States: closed (normal) -> open (failing fast) -> half-open (probing).
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
        key = f"{saga_id}:{service}"
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        self._waiters[key] = fut
        return fut

    def resolve(self, saga_id: str, service: str, event_data: dict):
        key = f"{saga_id}:{service}"
        fut = self._waiters.pop(key, None)
        if fut and not fut.done():
            fut.set_result(event_data)

    def cancel_all(self, saga_id: str):
        to_remove = [k for k in self._waiters if k.startswith(f"{saga_id}:")]
        for key in to_remove:
            fut = self._waiters.pop(key, None)
            if fut and not fut.done():
                fut.cancel()

    def cleanup(self, saga_id: str):
        """Remove stale waiters for a completed saga (prevents memory leak)."""
        to_remove = [k for k in self._waiters if k.startswith(f"{saga_id}:")]
        for key in to_remove:
            fut = self._waiters.pop(key, None)
            if fut and not fut.done():
                fut.cancel()


class _BaseExecutor:
    """Shared infrastructure for TwoPCExecutor and SagaExecutor."""

    def __init__(self, service_dbs: dict[str, aioredis.Redis],
                 outbox_reader: OutboxReader, wal: WALEngine,
                 circuit_breakers: dict[str, CircuitBreaker] | None = None,
                 metrics=None):
        self.service_dbs = service_dbs
        self.outbox_reader = outbox_reader
        self.wal = wal
        self.circuit_breakers = circuit_breakers or {}
        self.metrics = metrics

    def _check_circuit_breakers(self, steps: list[Step], saga_id: str) -> str | None:
        """Check all circuit breakers. Returns error string or None."""
        for step in steps:
            cb = self.circuit_breakers.get(step.service)
            if cb and cb.is_open():
                logger.warning("Circuit breaker open — fast fail",
                               service=step.service, saga_id=saga_id)
                return f"service_{step.service}_unavailable"
        return None

    async def _send_command(self, step: Step, saga_id: str, action: str, context: dict):
        """Send a command to a service's command stream."""
        service = step.service
        db = self.service_dbs[service]
        stream = f"{service}-commands"
        cmd = {"saga_id": saga_id, "action": action}
        if step.payload_builder:
            cmd.update(step.payload_builder(saga_id, action, context))
        await db.xadd(stream, cmd, maxlen=10000, approximate=True)

    async def _try_step_direct(self, step: Step, saga_id: str, action: str,
                                context: dict) -> dict:
        """Execute a single step via direct executor. Returns result dict."""
        try:
            return await step.direct_executor(
                saga_id, action, context, self.service_dbs[step.service]
            )
        except Exception as e:
            return {"event": "failed", "reason": str(e)}

    async def _try_step_stream(self, step: Step, saga_id: str, action: str,
                                context: dict) -> dict:
        """Execute a single step via stream command + outbox waiter."""
        fut = self.outbox_reader.create_waiter(saga_id, step.service)
        await self._send_command(step, saga_id, action, context)
        try:
            return await asyncio.wait_for(fut, timeout=STEP_TIMEOUT)
        except asyncio.TimeoutError:
            return {"event": "failed", "reason": "timeout"}

    async def _try_step(self, step: Step, saga_id: str, action: str,
                         context: dict) -> dict:
        """Execute a single step via best available method."""
        if step.direct_executor:
            return await self._try_step_direct(step, saga_id, action, context)
        return await self._try_step_stream(step, saga_id, action, context)

    async def _broadcast(
        self, action: str, saga_id: str, steps: list[Step], context: dict
    ) -> list[tuple[Step, dict]]:
        """Send action to all steps in parallel and collect responses."""
        results = await asyncio.gather(*[
            self._try_step(step, saga_id, action, context)
            for step in steps
        ], return_exceptions=True)
        out = []
        for step, result in zip(steps, results):
            if isinstance(result, BaseException):
                out.append((step, {"event": "failed", "reason": str(result)}))
            else:
                out.append((step, result))
        return out

    async def _retry_confirm_step(self, step: Step, saga_id: str, context: dict,
                                   retries: int = CONFIRM_MAX_RETRIES) -> bool:
        """Retry a single step's confirm with exponential backoff.

        Returns True if confirm eventually succeeded, False if exhausted.
        """
        for attempt in range(retries):
            result = await self._try_step(step, saga_id, "confirm", context)
            if isinstance(result, dict) and result.get("event") == "confirmed":
                return True
            logger.warning("Confirm retry", step=step.name, saga_id=saga_id,
                           attempt=attempt + 1, result=result)
            await asyncio.sleep(0.5 * (2 ** attempt))
        logger.error("Confirm exhausted retries", step=step.name, saga_id=saga_id)
        return False

    async def _verified_confirm(
        self, saga_id: str, steps: list[Step], context: dict
    ) -> None:
        """Confirm all steps with verification and retry on failure.

        Sends confirms in parallel, then retries any that failed.
        """
        results = await self._broadcast("confirm", saga_id, steps, context)
        for step, result in results:
            if isinstance(result, dict) and result.get("event") == "confirmed":
                cb = self.circuit_breakers.get(step.service)
                if cb:
                    cb.record_success()
            else:
                # Retry this step
                success = await self._retry_confirm_step(step, saga_id, context)
                cb = self.circuit_breakers.get(step.service)
                if cb:
                    if success:
                        cb.record_success()
                    else:
                        cb.record_failure()

    def _inject_ttl(self, context: dict) -> None:
        """Compute adaptive reservation TTL from p99 latency and store in context."""
        p99_ms = self.metrics.get_percentile(99) if self.metrics else 5000.0
        context["_reservation_ttl"] = max(30, min(120, int(p99_ms / 1000 * 3) + 10))


class TwoPCExecutor(_BaseExecutor):
    """Two-Phase Commit executor.

    Phase 1: Parallel prepare — send try_reserve to ALL participants, collect votes.
    Phase 2a (all reserved): Verified confirm all in parallel.
    Phase 2b (any failed): Cancel all in parallel.

    Differs from Saga in that ALL participants are queried in parallel before
    any decision is made. Optimal for low-contention workloads where most
    transactions succeed.
    """

    async def execute(self, tx_def: TransactionDefinition, saga_id: str,
                      context: dict) -> dict:
        # Check circuit breakers before any work
        err = self._check_circuit_breakers(tx_def.steps, saga_id)
        if err:
            await self.wal.log(saga_id, "FAILED")
            return {"status": "failed", "error": err}

        self._inject_ttl(context)

        # WAL: durable BEFORE sending any reservations
        await self.wal.log(saga_id, "PREPARING")

        # Phase 1: parallel prepare — all participants vote simultaneously
        votes = await self._broadcast("try_reserve", saga_id, tx_def.steps, context)

        # Record circuit breaker outcomes
        for step, result in votes:
            cb = self.circuit_breakers.get(step.service)
            if cb and result.get("event") == "failed" and result.get("reason") == "timeout":
                cb.record_failure()

        all_reserved = all(v[1].get("event") == "reserved" for v in votes)

        if all_reserved:
            # Phase 2a: COMMIT — WAL durable BEFORE confirming
            await self.wal.log(saga_id, "COMMITTING")
            await self._verified_confirm(saga_id, tx_def.steps, context)
            return {"status": "success"}
        else:
            # Phase 2b: ABORT — cancel all (idempotent, fire-and-forget WAL ok)
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
    """Saga + TCC executor with sequential try and early abort.

    Sequential try: reserve steps one by one, abort immediately on first
    failure. Under high contention this avoids wasting resources on steps
    that would need to be cancelled anyway.

    Differs from 2PC in the failure profile: Saga aborts faster (fails on
    first step) but has higher happy-path latency (sequential vs parallel).
    The adaptive protocol selector uses this: 2PC for low contention,
    Saga for high contention.
    """

    async def execute(self, tx_def: TransactionDefinition, saga_id: str,
                      context: dict) -> dict:
        # Check circuit breakers before any work
        err = self._check_circuit_breakers(tx_def.steps, saga_id)
        if err:
            await self.wal.log(saga_id, "FAILED")
            return {"status": "failed", "error": err}

        self._inject_ttl(context)

        # WAL: durable BEFORE sending any reservations
        await self.wal.log(saga_id, "TRYING")

        # Sequential try: reserve one step at a time, abort early on failure
        reserved_steps: list[Step] = []
        failure_reason = ""

        for step in tx_def.steps:
            result = await self._try_step(step, saga_id, "try_reserve", context)

            if result.get("event") == "reserved":
                reserved_steps.append(step)
                cb = self.circuit_breakers.get(step.service)
                if cb:
                    cb.record_success()
            else:
                # Record failure for circuit breaker
                if result.get("reason") == "timeout":
                    cb = self.circuit_breakers.get(step.service)
                    if cb:
                        cb.record_failure()
                failure_reason = result.get("reason", "insufficient_resources")
                break  # Early abort — don't try remaining steps

        if len(reserved_steps) < len(tx_def.steps):
            # Compensate: cancel ALL steps (cancel is idempotent for unreserved)
            asyncio.create_task(self.wal.log(saga_id, "COMPENSATING"))
            await self._broadcast("cancel", saga_id, tx_def.steps, context)
            await self.wal.log(saga_id, "FAILED")
            return {"status": "failed", "error": failure_reason}

        # All reserved — WAL durable BEFORE confirming
        await self.wal.log(saga_id, "CONFIRMING")
        await self._verified_confirm(saga_id, tx_def.steps, context)
        return {"status": "success"}
