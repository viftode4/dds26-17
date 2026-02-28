"""Shared fixtures for orchestrator unit tests (no Docker required)."""
from __future__ import annotations

import asyncio
import json
import subprocess
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

from orchestrator.definition import Step, TransactionDefinition
from orchestrator.executor import CircuitBreaker, OutboxReader
from orchestrator.wal import WALEngine
from orchestrator.metrics import MetricsCollector


# ---------------------------------------------------------------------------
# pytest-asyncio config
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def event_loop_policy():
    return asyncio.DefaultEventLoopPolicy()


# ---------------------------------------------------------------------------
# Mock Redis
# ---------------------------------------------------------------------------

def _make_mock_redis() -> AsyncMock:
    """Create a mock redis.asyncio.Redis with the methods used by the orchestrator."""
    db = AsyncMock()
    db.xadd = AsyncMock(return_value="1-0")
    db.execute_command = AsyncMock(return_value=None)
    db.xrange = AsyncMock(return_value=[])
    return db


@pytest.fixture
def mock_redis():
    return _make_mock_redis()


@pytest.fixture
def service_dbs():
    return {"stock": _make_mock_redis(), "payment": _make_mock_redis()}


# ---------------------------------------------------------------------------
# WAL
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def mock_wal(mock_redis):
    """WALEngine backed by a mock Redis — log() is a no-op, get_incomplete_sagas() returns empty."""
    wal = WALEngine(mock_redis)
    # Spy on log calls while keeping them cheap
    wal.log = AsyncMock()
    wal.get_incomplete_sagas = AsyncMock(return_value={})
    return wal


# ---------------------------------------------------------------------------
# OutboxReader
# ---------------------------------------------------------------------------

@pytest.fixture
def outbox_reader():
    return OutboxReader()


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

@pytest.fixture
def metrics():
    return MetricsCollector()


# ---------------------------------------------------------------------------
# Circuit breakers
# ---------------------------------------------------------------------------

@pytest.fixture
def circuit_breakers():
    return {"stock": CircuitBreaker(), "payment": CircuitBreaker()}


# ---------------------------------------------------------------------------
# Transaction definitions
# ---------------------------------------------------------------------------

def _stock_payload(saga_id: str, action: str, ctx: dict) -> dict:
    return {"items": ctx.get("items", "[]")}


def _payment_payload(saga_id: str, action: str, ctx: dict) -> dict:
    return {"user_id": ctx.get("user_id", "u1"), "total_cost": ctx.get("total_cost", "0")}


@pytest.fixture
def checkout_steps() -> list[Step]:
    return [
        Step(name="stock", service="stock", action="try_reserve",
             compensate="cancel", confirm="confirm", payload_builder=_stock_payload),
        Step(name="payment", service="payment", action="try_reserve",
             compensate="cancel", confirm="confirm", payload_builder=_payment_payload),
    ]


@pytest.fixture
def checkout_tx(checkout_steps) -> TransactionDefinition:
    return TransactionDefinition(name="checkout", steps=checkout_steps)


@pytest.fixture
def tx_definitions(checkout_tx) -> dict[str, TransactionDefinition]:
    return {"checkout": checkout_tx}


# ---------------------------------------------------------------------------
# Docker Compose helper (for integration tests)
# ---------------------------------------------------------------------------

def docker_compose(*args, check=False):
    """Run a docker compose command, returning CompletedProcess."""
    return subprocess.run(
        ["docker", "compose", *args],
        capture_output=True, text=True, check=check, timeout=60,
    )
