"""Shared fixtures for orchestrator unit and integration tests."""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio


# ---------------------------------------------------------------------------
# Auto-detect active Docker Compose file — ensures all docker compose
# subprocess calls target the correct running stack (small vs full).
# ---------------------------------------------------------------------------

def _detect_compose_file() -> str | None:
    """Detect which compose file the running stack was started with.

    Checks COMPOSE_FILE env var first, then inspects running containers
    to infer which compose file is active.
    """
    # If already set (e.g., by the user or CI), respect it
    if os.environ.get("COMPOSE_FILE"):
        return os.environ["COMPOSE_FILE"]

    # Infer from running container names: if *-service-1-1 exists,
    # we're using docker-compose-small.yml (services named *-service-1).
    # If *-service-1 exists (no double suffix), it's docker-compose.yml.
    try:
        result = subprocess.run(
            ["docker", "compose", "-f", "docker-compose-small.yml", "ps", "-q", "--status", "running"],
            capture_output=True, text=True, timeout=10, check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            return "docker-compose-small.yml"
    except Exception:
        pass

    return None


def _get_compose_services() -> list[str]:
    """Get list of service names from the active compose file."""
    try:
        result = subprocess.run(
            ["docker", "compose", "config", "--services"],
            capture_output=True, text=True, timeout=10, check=False,
        )
        if result.returncode == 0:
            return [s.strip() for s in result.stdout.strip().splitlines() if s.strip()]
    except Exception:
        pass
    return []


# Set COMPOSE_FILE early so all subprocess calls use the right file
_compose_file = _detect_compose_file()
if _compose_file:
    os.environ["COMPOSE_FILE"] = _compose_file


# Build app service list from the active compose file
_all_services = _get_compose_services()
APP_SERVICES = [s for s in _all_services if s.endswith("-service-1") or s.endswith("-service-2")
                or s in ("stock-service", "payment-service")]


# ---------------------------------------------------------------------------
# Docker availability check — skip integration tests if stack is not up
# ---------------------------------------------------------------------------

def _docker_stack_is_up() -> bool:
    """Return True if the Docker Compose stack has at least one running container."""
    try:
        result = subprocess.run(
            ["docker", "compose", "ps", "-q", "--status", "running"],
            capture_output=True, text=True, timeout=10, check=False,
        )
        return bool(result.stdout.strip())
    except Exception:
        return False


def pytest_collection_modifyitems(config, items):
    """Auto-skip integration-marked tests when the Docker stack is not running."""
    if not any(item.get_closest_marker("integration") for item in items):
        return
    if _docker_stack_is_up():
        return
    skip_marker = pytest.mark.skip(reason="Docker Compose stack not running (start with: docker compose up -d)")
    for item in items:
        if item.get_closest_marker("integration"):
            item.add_marker(skip_marker)

from orchestrator.definition import Step, TransactionDefinition
from orchestrator.executor import CircuitBreaker
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


# ---------------------------------------------------------------------------
# Mock Transport
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_transport():
    """A mock Transport whose send_and_wait can be configured per test."""
    transport = AsyncMock()
    transport.send_and_wait = AsyncMock(return_value={"event": "ok"})
    return transport


# ---------------------------------------------------------------------------
# WAL
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def mock_wal(mock_redis):
    """WALEngine backed by a mock Redis — log() is a no-op, get_incomplete_sagas() returns empty."""
    wal = WALEngine(mock_redis)
    # Spy on log calls while keeping them cheap
    wal.log = AsyncMock()
    wal.log_terminal = AsyncMock()
    wal.get_incomplete_sagas = AsyncMock(return_value={})
    return wal


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
        Step(name="stock", service="stock", payload_builder=_stock_payload),
        Step(name="payment", service="payment", payload_builder=_payment_payload),
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


# ---------------------------------------------------------------------------
# Integration test isolation
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _ensure_clean_topology(request):
    """Ensure all cluster containers AND app services are running before each integration test.

    After chaos tests that kill cluster nodes or app services, this fixture
    restarts any stopped containers and waits for a stable state.
    """
    if "integration" not in request.keywords:
        yield
        return

    from topology import ensure_containers_running, wait_cluster_stable

    ensure_containers_running(app_services=APP_SERVICES)
    wait_cluster_stable(timeout=30)
    yield


@pytest.fixture(autouse=True)
def _flush_databases_between_integration_tests(request, _ensure_clean_topology):
    """Flush all Redis cluster shards before each integration test.

    Only runs when the test is marked with @pytest.mark.integration.
    Issues FLUSHALL on each cluster master (replicates automatically to replicas).
    Set SKIP_DB_FLUSH=1 to disable (e.g. when running against a remote stack).
    """
    import os
    if "integration" not in request.keywords:
        yield
        return
    if os.environ.get("SKIP_DB_FLUSH", "").lower() in ("1", "true", "yes"):
        yield
        return

    # Flush each master shard — FLUSHALL in cluster mode flushes the local
    # node's slots and is replicated to its replica automatically.
    db_containers = [
        "order-cluster-1", "order-cluster-2", "order-cluster-3",
        "stock-cluster-1", "stock-cluster-2", "stock-cluster-3",
        "payment-cluster-1", "payment-cluster-2", "payment-cluster-3",
    ]
    for container in db_containers:
        subprocess.run(
            ["docker", "compose", "exec", "-T", container,
             "redis-cli", "-a", "redis", "--no-auth-warning", "FLUSHALL"],
            capture_output=True, text=True, timeout=15, check=False,
        )
    yield
