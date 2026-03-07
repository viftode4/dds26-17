"""NATS-based transport for orchestrator-to-service and service-side communication.

Replaces Redis Streams (XADD/XREADGROUP polling) with NATS Core request-reply
for sub-millisecond push-based messaging.
"""
import asyncio
import json

import nats
from nats.aio.client import Client as NatsClient, Msg

from common.logging import get_logger

log = get_logger("nats_transport")


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
        data = json.dumps(payload).encode()
        msg = await self._nc.request(subject, data, timeout=timeout)
        return json.loads(msg.data.decode())

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

    def __init__(self, nats_transport: NatsTransport):
        self._transport = nats_transport

    async def send_and_wait(self, service: str, action: str,
                            payload: dict, timeout: float = 10.0) -> dict:
        subject = f"svc.{service}.{action}"
        msg = {**payload, "action": action}
        try:
            return await self._transport.request(subject, msg, timeout=timeout)
        except asyncio.TimeoutError:
            return {"event": "failed", "reason": "timeout"}
        except Exception as e:
            return {"event": "failed", "reason": str(e)}
