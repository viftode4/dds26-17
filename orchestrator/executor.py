import asyncio
import time

import structlog
from opentelemetry import trace
from opentelemetry.trace import SpanKind, Status, StatusCode

from common.dlq import write_to_dlq
from orchestrator.definition import TransactionDefinition, Step
from orchestrator.wal import WALEngine

logger = structlog.get_logger("orchestrator.executor")
_tracer = trace.get_tracer("orchestrator.executor")

STEP_TIMEOUT = 10.0
MAX_VERIFIED_RETRIES = 15  # ~30s with exponential backoff capped at 5s


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
        self._probe_in_flight = False

    def record_failure(self):
        self._failure_count += 1
        self._last_failure_time = time.monotonic()
        self._probe_in_flight = False
        if self._state == "half-open" or self._failure_count >= self.failure_threshold:
            if self._state != "open":
                logger.warning("Circuit breaker OPEN", failures=self._failure_count)
                trace.get_current_span().add_event(
                    "circuit_breaker.opened",
                    {"failure_count": self._failure_count},
                )
            self._state = "open"

    def record_success(self):
        if self._state == "half-open":
            logger.info("Circuit breaker CLOSED (recovered)")
            trace.get_current_span().add_event("circuit_breaker.closed")
        self._failure_count = 0
        self._state = "closed"
        self._probe_in_flight = False

    def is_open(self) -> bool:
        if self._state == "closed":
            return False
        if self._state == "open":
            elapsed = time.monotonic() - (self._last_failure_time or 0)
            if elapsed >= self.recovery_timeout:
                self._state = "half-open"
                if not self._probe_in_flight:
                    self._probe_in_flight = True
                    return False
                return True  # probe already in flight
            return True
        # half-open
        if not self._probe_in_flight:
            self._probe_in_flight = True
            return False
        return True


class _BaseExecutor:
    """Shared infrastructure for TwoPCExecutor and SagaExecutor."""

    _FAILURE_REASON_PRIORITY = {
        "": 0,
        "failed": 0,
        "unknown": 0,
        "insufficient_resources": 0,
        "timeout": 3,
        "insufficient_stock": 2,
        "insufficient_credit": 2,
    }

    def __init__(self, transport, wal: WALEngine,
                 circuit_breakers: dict[str, CircuitBreaker] | None = None,
                 metrics=None):
        self.transport = transport
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
                trace.get_current_span().add_event(
                    "circuit_breaker.fast_fail",
                    {"service": step.service, "saga_id": saga_id},
                )
                return f"service_{step.service}_unavailable"
        return None

    def _choose_failure_reason(self, current: str, candidate: str) -> str:
        """Keep the most informative failure reason across parallel step results."""
        current_priority = self._FAILURE_REASON_PRIORITY.get(current, 1 if current else 0)
        candidate_priority = self._FAILURE_REASON_PRIORITY.get(candidate, 1 if candidate else 0)
        if candidate_priority > current_priority:
            return candidate
        return current

    async def _try_step(self, step: Step, saga_id: str, action: str,
                         context: dict) -> dict:
        """Execute a single step via transport send_and_wait."""
        with _tracer.start_as_current_span(
            f"step.{action} {step.service}",
            kind=SpanKind.CLIENT,
        ) as span:
            span.set_attribute("step.name", step.name)
            span.set_attribute("step.service", step.service)
            span.set_attribute("step.action", action)
            span.set_attribute("saga_id", saga_id)

            payload = {"saga_id": saga_id}
            if step.payload_builder:
                payload.update(step.payload_builder(saga_id, action, context))
            try:
                result = await self.transport.send_and_wait(step.service, action, payload, STEP_TIMEOUT)
            except asyncio.TimeoutError:
                result = {"event": "failed", "reason": "timeout"}

            event = result.get("event", "") if isinstance(result, dict) else ""
            span.set_attribute("step.event", event)
            if event == "failed":
                span.set_status(Status(StatusCode.ERROR, result.get("reason", "failed")))
            return result

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

    async def _verified_action(self, action: str, expected_event: str,
                                saga_id: str, steps: list[Step], context: dict):
        """Execute action on all steps with retry up to MAX_VERIFIED_RETRIES.

        If retries are exhausted, remaining steps are written to the DLQ.
        """
        with _tracer.start_as_current_span(
            f"verified.{action}",
            kind=SpanKind.INTERNAL,
        ) as span:
            span.set_attribute("saga_id", saga_id)
            span.set_attribute("step.action", action)
            span.set_attribute("step.count", len(steps))

            pending = list(steps)
            backoff = 0.2
            max_backoff = 5.0
            attempt = 0
            while pending and attempt < MAX_VERIFIED_RETRIES:
                attempt += 1
                results = await asyncio.gather(*[
                    self._try_step(step, saga_id, action, context) for step in pending
                ], return_exceptions=True)
                still_pending = []
                for step, result in zip(pending, results):
                    if isinstance(result, BaseException):
                        still_pending.append(step)
                        cb = self.circuit_breakers.get(step.service)
                        if cb:
                            cb.record_failure()
                    elif isinstance(result, dict) and result.get("event") == expected_event:
                        cb = self.circuit_breakers.get(step.service)
                        if cb:
                            cb.record_success()
                    else:
                        still_pending.append(step)
                        logger.warning(f"{action} retry needed", step=step.name,
                                     saga_id=saga_id, result=result)
                pending = still_pending
                if pending:
                    span.add_event(f"{action}.retry", {"attempt": attempt, "pending": len(pending)})
                    await asyncio.sleep(min(backoff, max_backoff))
                    backoff *= 2

            if pending:
                span.add_event(f"{action}.dlq", {"steps": [s.name for s in pending]})
                logger.error("Max retries exhausted, sending to DLQ",
                             action=action, saga_id=saga_id,
                             steps=[s.name for s in pending], attempts=attempt)
                for step in pending:
                    await write_to_dlq(
                        self.wal.db, saga_id, action, step.name,
                        "max_retries_exhausted", attempt, context,
                    )


class TwoPCExecutor(_BaseExecutor):
    """Real Two-Phase Commit: prepare (lock, no mutations) -> commit (apply) or abort (release)."""

    async def execute(self, tx_def: TransactionDefinition, saga_id: str,
                      context: dict, tx_name: str = "") -> dict:
        with _tracer.start_as_current_span("2pc.execute", kind=SpanKind.INTERNAL) as span:
            span.set_attribute("saga_id", saga_id)
            span.set_attribute("tx.name", tx_name)
            span.set_attribute("protocol", "2pc")

            err = self._check_circuit_breakers(tx_def.steps, saga_id)
            if err:
                await self.wal.log_terminal(saga_id, "FAILED")
                span.set_status(Status(StatusCode.ERROR, err))
                return {"status": "failed", "error": err}

            # Phase 1: Prepare
            await self.wal.log(saga_id, "PREPARING", {**context, "protocol": "2pc", "tx_name": tx_name})
            span.add_event("2pc.prepare.start")
            votes = await self._broadcast("prepare", saga_id, tx_def.steps, context)

            # Record circuit breaker outcomes
            for step, result in votes:
                cb = self.circuit_breakers.get(step.service)
                if cb:
                    if isinstance(result, dict) and result.get("event") == "prepared":
                        cb.record_success()
                    elif isinstance(result, dict) and result.get("reason") == "timeout":
                        cb.record_failure()

            all_prepared = all(
                isinstance(v[1], dict) and v[1].get("event") == "prepared"
                for v in votes
            )

            if all_prepared:
                # Phase 2a: COMMIT (irrevocable)
                span.add_event("2pc.commit.start")
                await self.wal.log(
                    saga_id,
                    "COMMITTING",
                    {**context, "protocol": "2pc", "tx_name": tx_name},
                )
                await self._verified_action("commit", "committed", saga_id, tx_def.steps, context)
                await self.wal.log_terminal(saga_id, "COMPLETED")
                span.set_attribute("outcome", "success")
                return {"status": "success"}
            else:
                # Phase 2b: ABORT (release locks)
                span.add_event("2pc.abort.start")
                await self.wal.log(
                    saga_id,
                    "ABORTING",
                    {**context, "protocol": "2pc", "tx_name": tx_name},
                )
                await self._verified_action("abort", "aborted", saga_id, tx_def.steps, context)
                await self.wal.log_terminal(saga_id, "FAILED")
                reason = "insufficient_resources"
                for _, vote in votes:
                    if isinstance(vote, dict) and vote.get("event") == "failed":
                        reason = vote.get("reason", reason)
                        break
                span.set_attribute("outcome", "failed")
                span.set_attribute("failure_reason", reason)
                span.set_status(Status(StatusCode.ERROR, reason))
                return {"status": "failed", "error": reason}


class SagaExecutor(_BaseExecutor):
    """Real Saga: execute (direct mutation) -> compensate in reverse on failure."""

    async def execute(self, tx_def: TransactionDefinition, saga_id: str,
                      context: dict, tx_name: str = "") -> dict:
        with _tracer.start_as_current_span("saga.execute", kind=SpanKind.INTERNAL) as span:
            span.set_attribute("saga_id", saga_id)
            span.set_attribute("tx.name", tx_name)
            span.set_attribute("protocol", "saga")

            err = self._check_circuit_breakers(tx_def.steps, saga_id)
            if err:
                await self.wal.log_terminal(saga_id, "FAILED")
                span.set_status(Status(StatusCode.ERROR, err))
                return {"status": "failed", "error": err}

            await self.wal.log(saga_id, "EXECUTING", {**context, "protocol": "saga", "tx_name": tx_name})

            # Parallel execute — stock & payment are independent
            completed_steps = []
            failure_reason = ""
            all_succeeded = True
            step_results = await self._broadcast("execute", saga_id, tx_def.steps, context)
            for step, result in step_results:
                if isinstance(result, dict) and result.get("event") == "executed":
                    completed_steps.append(step)
                    cb = self.circuit_breakers.get(step.service)
                    if cb:
                        cb.record_success()
                else:
                    reason = result.get("reason", "insufficient_resources") if isinstance(result, dict) else str(result)
                    if isinstance(result, dict) and result.get("reason") == "timeout":
                        cb = self.circuit_breakers.get(step.service)
                        if cb:
                            cb.record_failure()
                        completed_steps.append(step)
                    failure_reason = self._choose_failure_reason(failure_reason, reason)
                    all_succeeded = False
                    span.add_event("saga.step.failed", {"step": step.name, "reason": reason})

            if all_succeeded:
                await self.wal.log_terminal(saga_id, "COMPLETED")
                span.set_attribute("outcome", "success")
                return {"status": "success"}
            else:
                # Compensate completed steps in REVERSE order
                span.add_event("saga.compensate.start", {"steps": len(completed_steps)})
                comp_context = {**context, "completed_steps": [s.name for s in completed_steps]}
                await self.wal.log(saga_id, "COMPENSATING", comp_context)
                for step in reversed(completed_steps):
                    await self._verified_action("compensate", "compensated", saga_id, [step], context)
                await self.wal.log_terminal(saga_id, "FAILED")
                span.set_attribute("outcome", "failed")
                span.set_attribute("failure_reason", failure_reason)
                span.set_status(Status(StatusCode.ERROR, failure_reason))
                return {"status": "failed", "error": failure_reason}
