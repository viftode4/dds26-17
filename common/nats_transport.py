"""NATS-based transport for orchestrator-to-service and service-side communication.

Replaces Redis Streams (XADD/XREADGROUP polling) with NATS Core request-reply
for sub-millisecond push-based messaging.
"""
import asyncio
import json

import nats
from nats.aio.client import Client as NatsClient, Msg
from opentelemetry import trace
from opentelemetry.trace import SpanKind, Status, StatusCode

from common.logging import get_logger
from common.tracing import inject_trace_context

log = get_logger("nats_transport")
_tracer = trace.get_tracer("nats_transport")


class NatsTransport:
    """Low-level NATS connection wrapper used by both orchestrator and services."""

    def __init__(self, url: str = "nats://nats:4222"):
        self._url = url
        self._nc: NatsClient | None = None
        self._subs: list = []

    async def connect(self, retries: int = 30, delay: float = 1.0):
        for attempt in range(1, retries + 1):
            try:
                self._nc = await nats.connect(
                    self._url,
                    max_reconnect_attempts=-1,
                    reconnect_time_wait=0.5,
                    error_cb=self._error_cb,
                    reconnected_cb=self._reconnected_cb,
                    disconnected_cb=self._disconnected_cb,
                )
                log.info("NATS connected", url=self._url)
                return
            except Exception as e:
                if attempt == retries:
                    raise RuntimeError(f"NATS not available after {retries} attempts: {e}") from e
                log.warning("NATS not ready, retrying", attempt=attempt, error=str(e))
                await asyncio.sleep(delay)

    async def request(self, subject: str, payload: dict, timeout: float = 10.0) -> dict:
        with _tracer.start_as_current_span(
            f"nats send {subject}",
            kind=SpanKind.CLIENT,
        ) as span:
            span.set_attribute("messaging.system", "nats")
            span.set_attribute("messaging.destination.name", subject)
            span.set_attribute("messaging.operation", "send")

            enriched = inject_trace_context(payload)
            data = json.dumps(enriched).encode()
            msg = await self._nc.request(subject, data, timeout=timeout)
            response = json.loads(msg.data.decode())

            if isinstance(response, dict) and response.get("event") == "failed":
                span.set_status(Status(StatusCode.ERROR, response.get("reason", "failed")))

            return response

    async def subscribe(self, subject: str, queue: str, handler):
        """Subscribe with a queue group for load-balanced delivery.

        handler signature: async def handler(msg: Msg) -> None
        """
        sub = await self._nc.subscribe(subject, queue=queue, cb=handler)
        self._subs.append(sub)
        return sub

    async def close(self):
        for sub in self._subs:
            try:
                await sub.unsubscribe()
            except Exception:
                pass
        self._subs.clear()
        if self._nc:
            await self._nc.drain()
            self._nc = None

    async def _error_cb(self, e):
        log.error("NATS error", error=str(e))

    async def _reconnected_cb(self):
        log.info("NATS reconnected", url=self._nc.connected_url.netloc if self._nc else "")

    async def _disconnected_cb(self):
        log.warning("NATS disconnected")


class NatsOrchestratorTransport:
    """Implements orchestrator.transport.Transport using NATS request-reply."""

    TRANSIENT_RETRIES = 6       # up to ~15s of retrying (0.5+1+2+4+4+4)
    INITIAL_BACKOFF = 0.5
    MAX_BACKOFF = 4.0

    def __init__(self, nats_transport: NatsTransport):
        self._transport = nats_transport

    async def send_and_wait(self, service: str, action: str,
                            payload: dict, timeout: float = 10.0) -> dict:
        subject = f"svc.{service}.{action}"
        msg = {**payload, "action": action}
        # Mutations (prepare/execute) are NOT safely retryable across Sentinel
        # failover — the idempotency status key lives on the old master and may
        # not replicate before promotion.  Retrying can deduct stock/credit twice.
        # Only commit/abort/compensate are idempotent by design (they check status
        # and handle missing prepare data via re-deduction).
        max_retries = 1 if action in ("prepare", "execute") else self.TRANSIENT_RETRIES
        backoff = self.INITIAL_BACKOFF
        last_error = ""
        for attempt in range(1, max_retries + 1):
            try:
                return await self._transport.request(subject, msg, timeout=timeout)
            except asyncio.TimeoutError:
                last_error = "timeout"
                span = trace.get_current_span()
                span.add_event("nats.timeout", {"subject": subject, "attempt": attempt})
            except Exception as e:
                err = str(e)
                if "no responders" in err.lower() or "disconnected" in err.lower() or "connection" in err.lower():
                    last_error = err
                else:
                    return {"event": "failed", "reason": err}
            if attempt < max_retries:
                log.warning("NATS transient error, retrying",
                            subject=subject, attempt=attempt, error=last_error)
                await asyncio.sleep(min(backoff, self.MAX_BACKOFF))
                backoff *= 2
        return {"event": "failed", "reason": last_error}
