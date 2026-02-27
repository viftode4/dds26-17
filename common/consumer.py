"""Shared Redis Streams consumer infrastructure for stock and payment services.

Provides a concurrent command dispatcher (consumer_loop) and a periodic
dead-letter queue sweep (dlq_sweep_loop) that are identical across services.
Services inject only their service-specific ``handle_command`` callback.
"""
import asyncio
from typing import Awaitable, Callable

import redis.asyncio as aioredis

from common.logging import get_logger

log = get_logger("consumer")

MAX_INFLIGHT = 50  # concurrent handler tasks (bounded by connection pool)


async def _process_and_ack(
    msg_id: str,
    fields: dict,
    handle_command: Callable[[dict], Awaitable[None]],
    db: aioredis.Redis,
    stream: str,
    consumer_group: str,
    sem: asyncio.Semaphore,
):
    """Process one command and ACK — runs as a concurrent task."""
    try:
        await handle_command(fields)
    except Exception as e:
        log.error("Error processing message", msg_id=msg_id, error=str(e))
    finally:
        await db.xack(stream, consumer_group, msg_id)
        sem.release()


async def consumer_loop(
    db: aioredis.Redis,
    stream: str,
    consumer_group: str,
    consumer_name: str,
    handle_command: Callable[[dict], Awaitable[None]],
    max_inflight: int = MAX_INFLIGHT,
):
    """Read commands from a Redis Stream and dispatch them concurrently.

    Each message is processed in its own asyncio task. A semaphore caps
    in-flight tasks to avoid connection pool exhaustion. XACK is safe
    out-of-order (PEL is a set). Lua scripts are idempotent for re-delivery.
    """
    sem = asyncio.Semaphore(max_inflight)
    pending: set[asyncio.Task] = set()

    while True:
        try:
            messages = await db.xreadgroup(
                consumer_group, consumer_name,
                {stream: ">"},
                count=10, block=100,
            )

            if not messages:
                continue

            for _stream_name, entries in messages:
                for msg_id, fields in entries:
                    await sem.acquire()
                    task = asyncio.create_task(
                        _process_and_ack(msg_id, fields, handle_command,
                                         db, stream, consumer_group, sem)
                    )
                    pending.add(task)
                    task.add_done_callback(pending.discard)

        except asyncio.CancelledError:
            for t in pending:
                t.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            raise
        except Exception as e:
            log.error("Consumer loop error", error=str(e))
            await asyncio.sleep(1)


async def _sweep_dlq(
    db: aioredis.Redis,
    stream: str,
    consumer_group: str,
    dlq_stream: str,
    max_retries: int,
):
    """Move messages that exceeded max_retries to the dead-letter queue."""
    try:
        pending = await db.xpending_range(stream, consumer_group, "-", "+", count=100)
        for entry in pending:
            if entry.get("times_delivered", 0) > max_retries:
                msg_id = entry["message_id"]
                msgs = await db.xrange(stream, msg_id, msg_id)
                if msgs:
                    _, fields = msgs[0]
                    fields["original_stream"] = stream
                    fields["original_id"] = msg_id
                    fields["reason"] = "max_retries_exceeded"
                    await db.xadd(dlq_stream, fields, maxlen=5000, approximate=True)
                await db.xack(stream, consumer_group, msg_id)
                log.warning("Moved to DLQ", msg_id=msg_id, stream=stream)
    except Exception as e:
        log.error("DLQ sweep error", error=str(e))


async def dlq_sweep_loop(
    db: aioredis.Redis,
    stream: str,
    consumer_group: str,
    dlq_stream: str,
    max_retries: int = 5,
):
    """Periodic DLQ sweep — runs independently so it never blocks the hot path."""
    while True:
        try:
            await asyncio.sleep(10)
            await _sweep_dlq(db, stream, consumer_group, dlq_stream, max_retries)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.error("DLQ sweep loop error", error=str(e))
