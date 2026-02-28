from dataclasses import dataclass, field
from typing import Awaitable, Callable

import redis.asyncio as aioredis


@dataclass
class Step:
    """A single step in a distributed transaction.

    The optional ``payload_builder`` callback lets the *application* inject
    domain-specific fields (encoded items, user IDs, amounts …) into stream
    commands without the orchestrator knowing anything about the services.

    Signature: ``(saga_id: str, action: str, context: dict) -> dict``
    The returned dict is merged into the base command ``{saga_id, action}``.

    The optional ``direct_executor`` callback bypasses streams entirely,
    calling the Lua FCALL directly on the target Redis.  When all steps in a
    transaction have a ``direct_executor``, the orchestrator uses it instead
    of XADD → poll-outbox, eliminating two network hops per step.

    Signature: ``(saga_id, action, context, db) -> {"event": ...}``
    """
    name: str
    service: str
    action: str            # "try_reserve" (saga) / "prepare" (2pc)
    compensate: str        # "cancel" (saga compensation)
    confirm: str           # "confirm" (saga/2pc commit)
    payload_builder: Callable[[str, str, dict], dict] | None = None
    direct_executor: Callable[[str, str, dict, aioredis.Redis], Awaitable[dict]] | None = None

    def build_command(self, saga_id: str, action_override: str | None = None,
                      **extra_fields) -> dict:
        """Build a stream command dict for this step."""
        cmd = {
            "saga_id": saga_id,
            "action": action_override or self.action,
        }
        cmd.update(extra_fields)
        return cmd


@dataclass
class TransactionDefinition:
    """Declarative definition of a distributed transaction."""
    name: str
    steps: list[Step] = field(default_factory=list)
