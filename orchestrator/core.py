import asyncio
import time
import uuid

from msgspec import msgpack
import redis.asyncio as aioredis

from common.logging import get_logger
from orchestrator.definition import TransactionDefinition
from orchestrator.executor import TwoPCExecutor, SagaExecutor, OutboxReader, CircuitBreaker, OUTBOX_STREAMS
from orchestrator.wal import WALEngine
from orchestrator.metrics import MetricsCollector

logger = get_logger("orchestrator.core")

import socket

# Per-instance consumer group: each instance must see ALL outbox events (broadcast).
# Hostname is stable across restarts within the same Docker container, so
# XAUTOCLAIM on startup correctly reclaims this instance's stale PEL entries.
OUTBOX_CONSUMER_GROUP = f"orchestrator-{socket.gethostname()}"


class Orchestrator:
    """Hybrid 2PC/Saga orchestrator with adaptive protocol selection.

    Active-active design: every order instance runs sagas directly.
    No checkout-log stream indirection — results delivered in-process
    via asyncio.Future. pub/sub is retained as fallback for cross-instance
    recovery retries (when a different instance finishes your saga).

    The leader election governs background-only work:
    XAUTOCLAIM, reconciliation, orphan saga abort.
    """

    def __init__(self, order_db: aioredis.Redis,
                 service_dbs: dict[str, aioredis.Redis],
                 definitions: list[TransactionDefinition],
                 protocol: str = "auto"):
        self.order_db = order_db
        self.service_dbs = service_dbs
        self.definitions = {d.name: d for d in definitions}
        self.protocol_mode = protocol  # "auto", "2pc", or "saga"

        self.wal = WALEngine(order_db)
        self.metrics = MetricsCollector()
        self.outbox_reader = OutboxReader()

        # Shared circuit breakers — one per downstream service
        self.circuit_breakers = {
            service: CircuitBreaker(failure_threshold=5, recovery_timeout=30.0)
            for service in service_dbs
        }

        self.two_pc = TwoPCExecutor(service_dbs, self.outbox_reader, self.wal,
                                    self.circuit_breakers, self.metrics)
        self.saga = SagaExecutor(service_dbs, self.outbox_reader, self.wal,
                                 self.circuit_breakers, self.metrics)

        self._outbox_tasks: list[asyncio.Task] = []

    async def start(self):
        """Start background outbox reader tasks (runs on all instances)."""
        for service, stream in OUTBOX_STREAMS.items():
            db = self.service_dbs[service]
            # Create consumer group (idempotent). Use id="$" so historical events
            # with no matching waiters are not replayed on fresh startup.
            try:
                await db.xgroup_create(stream, OUTBOX_CONSUMER_GROUP, id="$", mkstream=True)
                logger.info("Outbox consumer group created", stream=stream)
            except aioredis.ResponseError as e:
                if "BUSYGROUP" not in str(e):
                    raise
            task = asyncio.create_task(
                self._outbox_consumer(db, stream, service)
            )
            self._outbox_tasks.append(task)
        logger.info("Orchestrator outbox readers started")

    async def stop(self):
        """Stop background tasks."""
        for task in self._outbox_tasks:
            task.cancel()
        for task in self._outbox_tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._outbox_tasks.clear()

    async def execute(self, tx_name: str, context: dict,
                      saga_id_override: str | None = None) -> dict:
        """Execute a distributed transaction. Returns result dict directly.

        This is called in-process from the HTTP handler — no stream hop,
        no pub/sub round-trip on the happy path. Both order instances can
        call this concurrently for different sagas.
        """
        tx_def = self.definitions.get(tx_name)
        if not tx_def:
            return {"status": "failed", "error": f"Unknown transaction: {tx_name}"}

        saga_id = saga_id_override or str(uuid.uuid4())
        protocol = self._select_protocol()

        # WAL: log transaction start — includes items so recovery can send correct commands
        await self.wal.log(saga_id, "STARTED", {
            "protocol": protocol,
            "tx_name": tx_name,
            **context,  # includes items — required for recovery cancel/confirm
        })

        # Execute and measure latency
        start = time.monotonic()
        if protocol == "2pc":
            result = await self.two_pc.execute(tx_def, saga_id, context)
        else:
            result = await self.saga.execute(tx_def, saga_id, context)
        duration = time.monotonic() - start

        self.metrics.record(
            success=(result.get("status") == "success"),
            protocol=protocol,
            duration=duration,
        )

        result["saga_id"] = saga_id
        result["protocol"] = protocol

        # Publish result via pub/sub — needed for cross-instance recovery retries
        # (when another instance needs to wait for this saga's result)
        await self._publish_result(saga_id, result)

        return result

    def _select_protocol(self) -> str:
        """Select protocol based on mode and abort-rate metrics."""
        if self.protocol_mode != "auto":
            return self.protocol_mode

        abort_rate = self.metrics.sliding_abort_rate()
        current = self.metrics.current_protocol

        # Hysteresis: switch to saga at 10% abort rate, back to 2pc at 5%
        if current == "2pc" and abort_rate >= 0.10:
            self.metrics.current_protocol = "saga"
            logger.info("Switching to SAGA protocol", abort_rate=f"{abort_rate:.2%}")
        elif current == "saga" and abort_rate <= 0.05:
            self.metrics.current_protocol = "2pc"
            logger.info("Switching to 2PC protocol", abort_rate=f"{abort_rate:.2%}")

        return self.metrics.current_protocol

    async def _publish_result(self, saga_id: str, result: dict):
        """Publish saga result for cross-instance recovery retries (pub/sub fallback)."""
        import json
        result_data = json.dumps(result)
        result_key = f"saga-result:{saga_id}"
        notify_channel = f"saga-notify:{saga_id}"
        await self.order_db.set(result_key, result_data, ex=60)
        await self.order_db.publish(notify_channel, "done")

    async def _outbox_consumer(self, db: aioredis.Redis, stream: str, service: str):
        """Background task reading outbox events via XREADGROUP for exactly-once delivery.

        On startup, XAUTOCLAIM reclaims any stale PEL entries from crashed instances
        so they are not permanently lost. New messages are read with XREADGROUP using
        '>' (only undelivered entries). Each message is ACKed after resolving its Future.
        """
        consumer_name = f"orchestrator-{uuid.uuid4().hex[:8]}"

        # Reclaim stale pending entries from any crashed previous instance (min idle 5s)
        try:
            claimed = await db.xautoclaim(
                stream, OUTBOX_CONSUMER_GROUP, consumer_name,
                min_idle_time=5000, start_id="0-0",
            )
            pending_entries = claimed[1] if claimed else []
            for msg_id, fields in (pending_entries or []):
                if fields:
                    saga_id = fields.get("saga_id", "")
                    if saga_id:
                        self.outbox_reader.resolve(saga_id, service, fields)
                await db.xack(stream, OUTBOX_CONSUMER_GROUP, msg_id)
            if pending_entries:
                logger.info("Reclaimed stale outbox entries", stream=stream,
                            count=len(pending_entries))
        except Exception as e:
            logger.warning("XAUTOCLAIM failed (non-fatal)", stream=stream, error=str(e))

        # Main loop: read new undelivered messages
        while True:
            try:
                messages = await db.xreadgroup(
                    OUTBOX_CONSUMER_GROUP, consumer_name,
                    {stream: ">"},
                    count=50, block=2000,
                )
                if not messages:
                    continue
                for _stream_name, entries in messages:
                    for msg_id, fields in entries:
                        saga_id = fields.get("saga_id", "")
                        if saga_id:
                            self.outbox_reader.resolve(saga_id, service, fields)
                        await db.xack(stream, OUTBOX_CONSUMER_GROUP, msg_id)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("Outbox consumer error", service=service, error=str(e))
                await asyncio.sleep(1)
