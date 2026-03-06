from dataclasses import dataclass, field
from typing import Awaitable, Callable

import redis.asyncio as aioredis


@dataclass
class Step:
    """A single step in a distributed transaction.

    The optional ``payload_builder`` callback lets the application inject
    domain-specific fields into stream commands.
    Signature: (saga_id: str, action: str, context: dict) -> dict

    The optional ``direct_executor`` callback bypasses streams entirely.
    Signature: (saga_id, action, context, db) -> {"event": ...}
    """
    name: str
    service: str
    payload_builder: Callable[[str, str, dict], dict] | None = None
    direct_executor: Callable[[str, str, dict, aioredis.Redis], Awaitable[dict]] | None = None


@dataclass
class TransactionDefinition:
    """Declarative definition of a distributed transaction."""
    name: str
    steps: list[Step] = field(default_factory=list)
