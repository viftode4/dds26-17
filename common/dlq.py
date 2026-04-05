"""Dead Letter Queue — Redis Stream for permanently failed saga operations.

When retry loops in the executor or recovery worker exhaust their max attempts,
the failed operation is written here for audit, alerting, and manual resolution.
"""

from __future__ import annotations

import json
import time

import structlog

log = structlog.get_logger("dlq")

DLQ_STREAM = "{order-wal}:dlq:saga"
DLQ_MAX_LEN = 10000


async def write_to_dlq(
    db,
    saga_id: str,
    action: str,
    step_name: str,
    reason: str,
    attempts: int,
    context: dict | None = None,
) -> None:
    entry = {
        "saga_id": saga_id,
        "action": action,
        "step": step_name,
        "reason": reason,
        "attempts": str(attempts),
        "context": json.dumps(context or {}),
        "timestamp": str(time.time()),
    }
    try:
        await db.xadd(DLQ_STREAM, entry, maxlen=DLQ_MAX_LEN, approximate=True)
        log.error("Saga operation sent to DLQ", saga_id=saga_id,
                  action=action, step=step_name, attempts=attempts, reason=reason)
    except Exception as e:
        log.error("Failed to write to DLQ", saga_id=saga_id, error=str(e))
