"""Sentinel topology detection and restoration for test isolation.

After chaos tests kill Redis masters, Sentinel promotes replicas. This leaves
the topology inverted for subsequent tests. These utilities detect drift and
restore the original master/replica roles so each test starts with a clean stack.
"""
from __future__ import annotations

import subprocess
import time


# Expected topology: service_name -> (master_container, replica_container)
MASTER_MAP = {
    "stock-master": ("stock-db", "stock-db-replica"),
    "order-master": ("order-db", "order-db-replica"),
    "payment-master": ("payment-db", "payment-db-replica"),
    "checkout-master": ("checkout-db", "checkout-db-replica"),
}

ALL_APP_SERVICES = [
    "stock-service", "stock-service-2",
    "payment-service", "payment-service-2",
    "order-service-1", "order-service-2",
    "checkout-service-1", "checkout-service-2",
    "gateway",
]

DB_CONTAINERS = [
    "stock-db", "stock-db-replica",
    "order-db", "order-db-replica",
    "payment-db", "payment-db-replica",
    "checkout-db", "checkout-db-replica",
]

SENTINEL_CONTAINERS = ["sentinel-1", "sentinel-2", "sentinel-3"]


def _docker_compose(*args: str, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", "compose", *args],
        capture_output=True, text=True, check=False, timeout=timeout,
    )


def _redis_cli(container: str, *args: str, port: int = 6379) -> str:
    """Run redis-cli inside a container and return stdout."""
    result = subprocess.run(
        ["docker", "compose", "exec", "-T", container,
         "redis-cli", "-a", "redis", "--no-auth-warning", "-p", str(port), *args],
        capture_output=True, text=True, check=False, timeout=15,
    )
    return result.stdout.strip()


def get_container_ip(container: str) -> str | None:
    """Get the IP address of a Docker Compose container."""
    result = subprocess.run(
        ["docker", "compose", "exec", "-T", container, "hostname", "-i"],
        capture_output=True, text=True, check=False, timeout=10,
    )
    ip = result.stdout.strip()
    return ip if ip else None


def get_sentinel_master_ip(service_name: str) -> str | None:
    """Ask sentinel-1 for the current master IP of a monitored service."""
    result = _redis_cli("sentinel-1", "SENTINEL", "get-master-addr-by-name", service_name, port=26379)
    # Response is two lines: IP then port
    lines = result.splitlines()
    return lines[0] if lines else None


def is_topology_drifted() -> dict[str, bool]:
    """Check whether Sentinel's view of each master matches the expected container.

    Returns a dict like {"stock-master": True, "order-master": False, ...}
    where True means the topology is drifted (needs restoration).
    """
    drifted = {}
    for service_name, (master_container, _replica) in MASTER_MAP.items():
        sentinel_ip = get_sentinel_master_ip(service_name)
        expected_ip = get_container_ip(master_container)
        if sentinel_ip is None or expected_ip is None:
            # Can't determine — assume drifted to be safe
            drifted[service_name] = True
        else:
            drifted[service_name] = sentinel_ip != expected_ip
    return drifted


def ensure_containers_running():
    """Start all DB, sentinel, NATS, and app containers in case a prior test left one dead."""
    containers = DB_CONTAINERS + SENTINEL_CONTAINERS + ["nats"] + ALL_APP_SERVICES
    _docker_compose("start", *containers)
    # Brief pause for containers to initialize
    time.sleep(3)


def restore_topology():
    """Restore the original master/replica roles for all drifted services.

    For each drifted service:
    1. REPLICAOF NO ONE on the original master (promote it back)
    2. REPLICAOF <master> 6379 on the replica (demote it back)
    3. SENTINEL RESET on all sentinels to force rediscovery
    4. Poll until Sentinel reports the correct master
    """
    drifted = is_topology_drifted()

    for service_name, is_drifted in drifted.items():
        if not is_drifted:
            continue

        master_container, replica_container = MASTER_MAP[service_name]
        print(f"[topology] Restoring {service_name}: "
              f"{master_container}=master, {replica_container}=replica")

        # Ensure the original master is running and reachable
        _docker_compose("start", master_container)
        _wait_redis_ready(master_container, timeout=15)

        # Promote original master back
        _redis_cli(master_container, "REPLICAOF", "NO", "ONE")

        # Demote the replica back (use container hostname for Docker DNS resolution)
        _redis_cli(replica_container, "REPLICAOF", master_container, "6379")

    # Reset all sentinels to force rediscovery of the correct topology
    for sentinel in SENTINEL_CONTAINERS:
        _redis_cli(sentinel, "SENTINEL", "RESET", "*", port=26379)

    # Wait for sentinels to converge on the correct masters
    _wait_sentinel_converged(timeout=30)


def _wait_redis_ready(container: str, timeout: float = 15):
    """Poll redis PING until the container responds."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = _redis_cli(container, "PING")
        if "PONG" in result:
            return
        time.sleep(1)
    print(f"[topology] WARNING: {container} did not respond to PING within {timeout}s")


def _wait_sentinel_converged(timeout: float = 30):
    """Poll sentinel until all services report the expected master IP."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        drifted = is_topology_drifted()
        if not any(drifted.values()):
            print("[topology] Sentinel topology restored successfully")
            return
        time.sleep(2)
    still_drifted = {k: v for k, v in is_topology_drifted().items() if v}
    print(f"[topology] WARNING: Sentinel did not converge within {timeout}s. "
          f"Still drifted: {still_drifted}")


def restart_app_services():
    """Restart all application services to refresh connection pools and reload Lua."""
    print(f"[topology] Restarting app services: {ALL_APP_SERVICES}")
    _docker_compose("restart", *ALL_APP_SERVICES)
    # Wait for healthchecks to pass (start_period=10s, interval=5s)
    time.sleep(20)


def wait_stack_healthy(timeout: float = 60):
    """Poll the gateway until all backend health endpoints return 200.

    After health checks pass, waits a brief settle period to allow
    connection pools and Lua functions to fully initialize.

    If health checks fail after initial attempts, restarts the gateway
    (HAProxy can get stuck with stale backend states after service restarts).
    """
    endpoints = [
        "/orders/health",
        "/orders/__checkout_health",
        "/stock/health",
        "/payment/health",
    ]
    deadline = time.monotonic() + timeout
    gateway_restarted = False

    while time.monotonic() < deadline:
        all_healthy = True
        for endpoint in endpoints:
            try:
                result = subprocess.run(
                    ["curl", "-sf", f"http://127.0.0.1:8000{endpoint}"],
                    capture_output=True, text=True, timeout=5, check=False,
                )
                if result.returncode != 0:
                    all_healthy = False
                    break
            except Exception:
                all_healthy = False
                break

        if all_healthy:
            return

        # If we've been failing for 15s and haven't tried a gateway restart yet,
        # do it — HAProxy can get stuck with stale backend state after service restarts.
        elapsed = timeout - (deadline - time.monotonic())
        if elapsed > 15 and not gateway_restarted:
            print("[topology] Health checks failing, restarting gateway...")
            _docker_compose("restart", "gateway")
            gateway_restarted = True
            time.sleep(5)
            continue

        time.sleep(2)

    print(f"[topology] WARNING: Stack not fully healthy after {timeout}s")
