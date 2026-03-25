import asyncio
import json
import time
import uuid

import redis.asyncio as aioredis

import structlog
from orchestrator.definition import TransactionDefinition
from orchestrator.executor import TwoPCExecutor, SagaExecutor, CircuitBreaker
from orchestrator.wal import WALEngine
from orchestrator.metrics import MetricsCollector

logger = structlog.get_logger("orchestrator.core")


MAX_CONCURRENT_CHECKOUTS = 200


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
                 transport,
                 definitions: list[TransactionDefinition],
                 protocol: str = "auto"):
        self.order_db = order_db
        self.definitions = {d.name: d for d in definitions}
        self.protocol_mode = protocol  # "auto", "2pc", or "saga"

        self.wal = WALEngine(order_db)
        self.metrics = MetricsCollector()

        # Backpressure: cap concurrent in-flight checkouts to prevent
        # cascading contention on shared Redis keys under load
        self._checkout_sem = asyncio.Semaphore(MAX_CONCURRENT_CHECKOUTS)

        # Shared circuit breakers — one per downstream service
        services = {s.service for d in definitions for s in d.steps}
        self.circuit_breakers = {
            service: CircuitBreaker(failure_threshold=5, recovery_timeout=30.0)
            for service in services
        }

        self.two_pc = TwoPCExecutor(transport, self.wal,
                                    self.circuit_breakers, self.metrics)
        self.saga = SagaExecutor(transport, self.wal,
                                 self.circuit_breakers, self.metrics)

    async def start(self):
        """No-op — transport lifecycle managed externally."""
        pass

    async def stop(self):
        """No-op — transport lifecycle managed externally."""
        pass

    async def execute(self, tx_name: str, context: dict,
                      saga_id_override: str | None = None) -> dict:
        """Execute a distributed transaction. Returns result dict directly.

        This is called in-process from the HTTP handler — no stream hop,
        no pub/sub round-trip on the happy path. Both order instances can
        call this concurrently for different sagas.

        Backpressure: bounded by _checkout_sem to prevent cascading
        contention under extreme load.
        """
        tx_def = self.definitions.get(tx_name)
        if not tx_def:
            return {"status": "failed", "error": f"Unknown transaction: {tx_name}"}

        async with self._checkout_sem:
            return await self._execute_inner(tx_def, tx_name, context, saga_id_override)

    async def _execute_inner(self, tx_def: TransactionDefinition, tx_name: str,
                              context: dict, saga_id_override: str | None) -> dict:
        saga_id = saga_id_override or str(uuid.uuid4())
        protocol = self._select_protocol()

        # Execute and measure latency
        start = time.monotonic()
        if protocol == "2pc":
            result = await self.two_pc.execute(tx_def, saga_id, context, tx_name=tx_name)
        else:
            result = await self.saga.execute(tx_def, saga_id, context, tx_name=tx_name)

        duration = time.monotonic() - start

        self.metrics.record(
            success=(result.get("status") == "success"),
            protocol=protocol,
            duration=duration,
        )

        result["saga_id"] = saga_id
        result["protocol"] = protocol

        # Publish result via pub/sub — fire-and-forget, needed only for
        # cross-instance recovery retries (rare fallback path)
        asyncio.create_task(self._publish_result(saga_id, result))

        return result

    def _select_protocol(self) -> str:
        """Select protocol based on mode and abort-rate metrics.

        Safety override: force 2PC when any circuit breaker is open (suspected
        partition).  SAGA execute is an irrevocable mutation — if the response
        is lost during a partition, the orchestrator cannot reliably compensate.
        2PC prepare is reversible: the abort path restores the deduction, and
        the poison-pill mechanism blocks late prepares after abort.
        """
        if self.protocol_mode != "auto":
            return self.protocol_mode

        # Force 2PC when any downstream service is degraded.  This prevents
        # irrevocable SAGA mutations during suspected network partitions.
        any_breaker_open = any(
            cb.is_open() for cb in self.circuit_breakers.values()
        )
        if any_breaker_open:
            if self.metrics.current_protocol != "2pc":
                logger.warning("Forcing 2PC — circuit breaker open (suspected partition)")
                self.metrics.current_protocol = "2pc"
            return "2pc"

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
        result_data = json.dumps(result)
        result_key = f"saga-result:{saga_id}"
        notify_channel = f"saga-notify:{saga_id}"
        async with self.order_db.pipeline(transaction=False) as pipe:
            pipe.set(result_key, result_data, ex=60)
            pipe.publish(notify_channel, "done")
            await pipe.execute()
