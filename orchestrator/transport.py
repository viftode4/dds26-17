from typing import Protocol


class Transport(Protocol):
    """Transport abstraction for orchestrator-to-service communication."""

    async def send_and_wait(self, service: str, action: str,
                            payload: dict, timeout: float) -> dict: ...
