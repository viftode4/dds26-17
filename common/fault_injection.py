"""App-level fault injection for chaos engineering.

Provides a singleton FaultInjector that service handlers call at specific
injection points (before/after each saga phase). Faults can be set via:
  - Environment variable: FAULT_INJECT=after_execute:crash
  - HTTP API: POST /fault/set {"point": "after_execute", "action": "crash"}

Supported actions:
  - crash:      os._exit(1) — hard kill, no cleanup
  - delay:<ms>: asyncio.sleep(ms) — simulate slow service
  - error:      raise RuntimeError — simulate handler failure
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass

import structlog

log = structlog.get_logger("fault_injection")


@dataclass(frozen=True)
class FaultRule:
    point: str
    action: str
    value: int = 0  # delay ms for "delay" action


class FaultInjector:
    __slots__ = ("_rules",)

    def __init__(self) -> None:
        self._rules: dict[str, FaultRule] = {}
        self._load_from_env()

    def _load_from_env(self) -> None:
        raw = os.environ.get("FAULT_INJECT", "")
        if not raw:
            return
        parts = raw.split(":")
        if len(parts) >= 2:
            point, action = parts[0], parts[1]
            value = int(parts[2]) if len(parts) > 2 else 0
            self._rules[point] = FaultRule(point=point, action=action, value=value)
            log.warning("Fault rule loaded from env", point=point, action=action, value=value)

    def set_fault(self, point: str, action: str, value: int = 0) -> None:
        self._rules[point] = FaultRule(point=point, action=action, value=value)
        log.warning("Fault rule SET", point=point, action=action, value=value)

    def clear_fault(self, point: str | None = None) -> None:
        if point:
            self._rules.pop(point, None)
        else:
            self._rules.clear()
        log.info("Fault rule CLEARED", point=point or "all")

    def get_rules(self) -> dict[str, FaultRule]:
        return dict(self._rules)

    async def maybe_inject(self, point: str, saga_id: str = "") -> None:
        rule = self._rules.get(point)
        if not rule:
            return
        log.warning("INJECTING FAULT", point=point, action=rule.action,
                    value=rule.value, saga_id=saga_id)
        if rule.action == "crash":
            os._exit(1)
        elif rule.action == "delay":
            await asyncio.sleep(rule.value / 1000.0)
        elif rule.action == "error":
            raise RuntimeError(f"Injected fault at {point}")


_injector = FaultInjector()


def get_injector() -> FaultInjector:
    return _injector
