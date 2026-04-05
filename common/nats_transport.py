"""NATS-based transport for orchestrator-to-service and service-side communication.

Command delivery: NATS JetStream (persistent, deduplicated, durable consumers).
Response delivery: Core NATS inbox (ephemeral, one-shot, lowest latency).

The COMMANDS stream uses memory storage (~28µs ack vs ~280µs hop — ~10% overhead)
and WorkQueue retention (messages deleted after consumer acks, no buildup).
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

_NS = 1_000_000_000  # nanoseconds per second


class _JsMsgAdapter:
    """Makes a JetStream Msg behave like a Core NATS Msg for service handlers.

    Service handlers call ``await msg.respond(data)`` — this publishes the
    response back to the orchestrator's Core NATS inbox (via the Reply-To
    header) and explicitly acks the JetStream message.
    """

    __slots__ = ("_msg", "_nc", "data", "subject")

    def __init__(self, msg: Msg, nc: NatsClient) -> None:
        self._msg = msg
        self._nc = nc
        self.data: bytes = msg.data
        self.subject: str = msg.subject

    async def respond(self, data: bytes) -> None:
        reply_to = (self._msg.headers or {}).get("Reply-To")
        if reply_to:
            await self._nc.publish(reply_to, data)
        await self._msg.ack()


class NatsTransport:
    """NATS connection wrapper used by both orchestrator and services.

    Commands flow via JetStream (durable, deduplicated).
    Replies flow via Core NATS ephemeral inbox (lowest latency).
    """

    def __init__(self, url: str = "nats://nats:4222") -> None:
        self._url = url
        self._nc: NatsClient | None = None
        self._js = None
        self._subs: list = []

    async def connect(self, retries: int = 30, delay: float = 1.0) -> None:
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
                self._js = self._nc.jetstream()
                await self._ensure_stream()
                log.info("NATS connected (JetStream)", url=self._url)
                return
            except Exception as e:
                if attempt == retries:
                    raise RuntimeError(f"NATS not available after {retries} attempts: {e}") from e
                log.warning("NATS not ready, retrying", attempt=attempt, error=str(e))
                await asyncio.sleep(delay)

    async def _ensure_stream(self) -> None:
        """Create the COMMANDS stream (idempotent — same config is a no-op on the server)."""
        from nats.js.api import RetentionPolicy, StorageType, StreamConfig
        await self._js.add_stream(StreamConfig(
            name="COMMANDS",
            subjects=["svc.>"],
            retention=RetentionPolicy.WORK_QUEUE,
            storage=StorageType.MEMORY,
            max_age=5 * 60 * _NS,        # 5 min — well beyond any retry window
            duplicate_window=120 * _NS,  # 2-min dedup window for Msg-Id
        ))
        log.info("JetStream COMMANDS stream ready")

    async def request(self, subject: str, payload: dict, timeout: float = 10.0) -> dict:
        with _tracer.start_as_current_span(
            f"nats send {subject}",
            kind=SpanKind.CLIENT,
        ) as span:
            span.set_attribute("messaging.system", "nats")
            span.set_attribute("messaging.destination.name", subject)
            span.set_attribute("messaging.operation", "send")

            inbox = self._nc.new_inbox()
            loop = asyncio.get_running_loop()
            fut: asyncio.Future[bytes] = loop.create_future()

            async def on_reply(msg: Msg) -> None:
                if not fut.done():
                    fut.set_result(msg.data)

            sub = await self._nc.subscribe(inbox, cb=on_reply)
            try:
                enriched = inject_trace_context(payload)
                data = json.dumps(enriched).encode()
                # Dedup key: unique per (saga, action) pair — protects against retry
                # double-delivery within the 2-minute duplicate_window.
                msg_id = f"{payload.get('saga_id', '')}-{subject.split('.')[-1]}"

                # Run JetStream publish (~28µs stream ack) concurrently with inbox
                # wait. The reply arrives after service processing (~280µs total),
                # which is always after the stream ack — no sequential penalty.
                _, reply_raw = await asyncio.gather(
                    self._js.publish(
                        subject, data,
                        headers={"Nats-Msg-Id": msg_id, "Reply-To": inbox},
                    ),
                    asyncio.wait_for(fut, timeout=timeout),
                )

                response = json.loads(reply_raw)
                if isinstance(response, dict) and response.get("event") == "failed":
                    span.set_status(Status(StatusCode.ERROR, response.get("reason", "failed")))
                return response
            finally:
                await sub.unsubscribe()

    async def subscribe(self, subject: str, queue: str, handler) -> None:
        """Subscribe with a durable JetStream push consumer (replaces Core NATS queue group).

        handler signature: async def handler(msg) -> None  (unchanged from before)
        msg.data and msg.respond() work exactly as with Core NATS messages.
        """
        async def js_handler(msg: Msg) -> None:
            await handler(_JsMsgAdapter(msg, self._nc))

        sub = await self._js.subscribe(
            subject,
            queue=queue,        # delivery group — load-balanced across instances
            durable=queue,      # persistent consumer — survives service restart
            cb=js_handler,
            manual_ack=True,    # _JsMsgAdapter.respond() calls msg.ack() explicitly
        )
        self._subs.append(sub)
        return sub

    async def close(self) -> None:
        for sub in self._subs:
            try:
                await sub.unsubscribe()
            except Exception:
                pass
        self._subs.clear()
        if self._nc:
            await self._nc.drain()
            self._nc = None

    async def _error_cb(self, e: Exception) -> None:
        log.error("NATS error", error=str(e))

    async def _reconnected_cb(self) -> None:
        log.info("NATS reconnected", url=self._nc.connected_url.netloc if self._nc else "")

    async def _disconnected_cb(self) -> None:
        log.warning("NATS disconnected")


class NatsOrchestratorTransport:
    """Implements orchestrator.transport.Transport using NATS JetStream commands."""

    TRANSIENT_RETRIES = 6       # up to ~15s of retrying (0.5+1+2+4+4+4)
    INITIAL_BACKOFF = 0.5
    MAX_BACKOFF = 4.0

    def __init__(self, nats_transport: NatsTransport) -> None:
        self._transport = nats_transport

    async def send_and_wait(self, service: str, action: str,
                            payload: dict, timeout: float = 10.0) -> dict:
        subject = f"svc.{service}.{action}"
        msg = {**payload, "action": action}
        # Mutations (prepare/execute) are NOT safely retryable across Sentinel
        # failover — the idempotency status key lives on the old master and may
        # not replicate before promotion. Retrying can deduct stock/credit twice.
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
