"""Cluster topology health checking and container restart for test isolation.

After chaos tests kill cluster master nodes, the cluster auto-promotes replicas.
These utilities ensure all containers are running and the cluster has stabilized
before subsequent tests start.
"""
from __future__ import annotations

import subprocess
import time


CLUSTER_CONTAINERS = [
    "order-cluster-1", "order-cluster-2", "order-cluster-3",
    "order-cluster-replica-1", "order-cluster-replica-2", "order-cluster-replica-3",
    "stock-cluster-1", "stock-cluster-2", "stock-cluster-3",
    "stock-cluster-replica-1", "stock-cluster-replica-2", "stock-cluster-replica-3",
    "payment-cluster-1", "payment-cluster-2", "payment-cluster-3",
    "payment-cluster-replica-1", "payment-cluster-replica-2", "payment-cluster-replica-3",
]

ALL_APP_SERVICES = [
    "order-service-1", "order-service-2",
    "stock-service-1", "stock-service-2",
    "payment-service-1", "payment-service-2",
]


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


def ensure_containers_running():
    """Start all cluster nodes and NATS in case a prior test left one dead."""
    containers = CLUSTER_CONTAINERS + ["nats"]
    _docker_compose("start", *containers)
    # Brief pause for containers to initialize and cluster to stabilize
    time.sleep(5)


def wait_cluster_stable(timeout: float = 60):
    """Poll CLUSTER INFO until all three clusters report cluster_state:ok.

    After killing a cluster master, the cluster elects a new master in
    ~5-10s (cluster-node-timeout=5000ms + election overhead). Polls until
    all three clusters (order, stock, payment) confirm a healthy state.
    """
    # Use the -2 nodes as check targets — they are never the killed masters
    # in failover tests (tests always kill -1).
    check_nodes = ["order-cluster-2", "stock-cluster-2", "payment-cluster-2"]
    deadline = time.monotonic() + timeout

    while time.monotonic() < deadline:
        all_ok = True
        for node in check_nodes:
            info = _redis_cli(node, "CLUSTER", "INFO")
            if "cluster_state:ok" not in info:
                all_ok = False
                break
        if all_ok:
            print("[topology] All clusters stable (cluster_state:ok)")
            return
        time.sleep(2)

    print(f"[topology] WARNING: Cluster did not stabilize within {timeout}s")


def _wait_redis_ready(container: str, timeout: float = 15):
    """Poll redis PING until the container responds."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = _redis_cli(container, "PING")
        if "PONG" in result:
            return
        time.sleep(1)
    print(f"[topology] WARNING: {container} did not respond to PING within {timeout}s")


def restart_app_services():
    """Restart all application services to refresh connection pools and reload Lua."""
    print(f"[topology] Restarting app services: {ALL_APP_SERVICES}")
    _docker_compose("restart", *ALL_APP_SERVICES)
    # Wait for healthchecks to pass (start_period=10s, interval=5s)
    time.sleep(20)


def wait_stack_healthy(timeout: float = 60):
    """Poll the gateway until all backend health endpoints return 200.

    If health checks fail after initial attempts, restarts the gateway
    (HAProxy can get stuck with stale backend states after service restarts).
    """
    endpoints = ["/orders/health", "/stock/health", "/payment/health"]
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
