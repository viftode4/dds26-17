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
}

ALL_APP_SERVICES = [
    "stock-service", "stock-service-2",
    "payment-service", "payment-service-2",
    "order-service-1", "order-service-2",
]

DB_CONTAINERS = [
    "stock-db", "stock-db-replica",
    "order-db", "order-db-replica",
    "payment-db", "payment-db-replica",
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
    """Start all DB, sentinel, and NATS containers in case a prior test left one dead."""
    containers = DB_CONTAINERS + SENTINEL_CONTAINERS + ["nats"]
    _docker_compose("start", *containers)
    # Brief pause for containers to initialize
    time.sleep(3)


def restore_topology():
    """Restore the original master/replica roles for all drifted services.

    For each drifted service:
    1. REPLICAOF NO ONE on the original master (promote it back)
    2. REPLICAOF <master> 6379 on the replica (demote it back)
    3. SENTINEL REMOVE + SENTINEL MONITOR to give sentinels the correct master
       IP directly — avoids the multi-second failover cycle that SENTINEL RESET
       triggers when the current monitored IP has become a replica.
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

        # Explicitly re-register the master with each sentinel.
        # SENTINEL RESET keeps the old (drifted) master IP and waits for
        # auto-discovery which can take >60s when failover-timeout doubles
        # after repeated failovers. SENTINEL REMOVE + MONITOR gives the
        # correct IP immediately.
        master_ip = get_container_ip(master_container)
        if master_ip:
            for sentinel in SENTINEL_CONTAINERS:
                _redis_cli(sentinel, "SENTINEL", "REMOVE", service_name, port=26379)
                _redis_cli(sentinel, "SENTINEL", "MONITOR", service_name,
                           master_ip, "6379", "2", port=26379)
                _redis_cli(sentinel, "SENTINEL", "SET", service_name,
                           "auth-pass", "redis", port=26379)
                _redis_cli(sentinel, "SENTINEL", "SET", service_name,
                           "down-after-milliseconds", "5000", port=26379)
                _redis_cli(sentinel, "SENTINEL", "SET", service_name,
                           "failover-timeout", "10000", port=26379)
        else:
            print(f"[topology] WARNING: Cannot resolve IP for {master_container}, "
                  f"falling back to SENTINEL RESET")
            for sentinel in SENTINEL_CONTAINERS:
                _redis_cli(sentinel, "SENTINEL", "RESET", "*", port=26379)

    # Brief settle for sentinel state propagation
    time.sleep(3)

    # Wait for sentinels to converge on the correct masters
    _wait_sentinel_converged(timeout=60)


def _wait_redis_ready(container: str, timeout: float = 15):
    """Poll redis PING until the container responds."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = _redis_cli(container, "PING")
        if "PONG" in result:
            return
        time.sleep(1)
    print(f"[topology] WARNING: {container} did not respond to PING within {timeout}s")


def _wait_sentinel_converged(timeout: float = 60):
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


def force_failover(service_name: str, new_master_container: str) -> None:
    """Manually promote a replica to master when Sentinel auto-failover is blocked.

    On Docker Desktop/Windows the VM clock sync triggers Sentinel's TILT mode
    continuously (~every 30 s), preventing automatic failover indefinitely.
    SENTINEL FAILOVER is also aborted by TILT re-entry immediately after the
    +try-failover event.

    This function bypasses Sentinel's failover engine entirely:
    1. Sends REPLICAOF NO ONE directly to the chosen replica container.
    2. Re-registers the service with all Sentinels using the replica's new IP.
    3. Waits for Sentinel to converge (accept the updated master info).
    """
    new_master_ip = get_container_ip(new_master_container)
    if not new_master_ip:
        raise RuntimeError(
            f"[topology] force_failover: cannot resolve IP for {new_master_container}"
        )

    # Promote the replica to standalone master
    result = _redis_cli(new_master_container, "REPLICAOF", "NO", "ONE")
    print(
        f"[topology] force_failover: promoted {new_master_container} "
        f"({new_master_ip}) to master for {service_name}: {result!r}"
    )

    # Re-register the service with each Sentinel pointing at the new master
    for sentinel in SENTINEL_CONTAINERS:
        _redis_cli(sentinel, "SENTINEL", "REMOVE", service_name, port=26379)
        _redis_cli(sentinel, "SENTINEL", "MONITOR", service_name,
                   new_master_ip, "6379", "2", port=26379)
        _redis_cli(sentinel, "SENTINEL", "SET", service_name,
                   "auth-pass", "redis", port=26379)
        _redis_cli(sentinel, "SENTINEL", "SET", service_name,
                   "down-after-milliseconds", "5000", port=26379)
        _redis_cli(sentinel, "SENTINEL", "SET", service_name,
                   "failover-timeout", "10000", port=26379)

    # Wait for all sentinels to report the new master IP.
    # Cannot use _wait_sentinel_converged() here — that function checks against
    # MASTER_MAP (original master), but we intentionally inverted the topology.
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        reported = get_sentinel_master_ip(service_name)
        if reported == new_master_ip:
            break
        time.sleep(1)
    else:
        print(
            f"[topology] WARNING: force_failover: Sentinel did not converge to "
            f"{new_master_ip} within 30s (last reported: {get_sentinel_master_ip(service_name)})"
        )
    print(
        f"[topology] force_failover: {service_name} master is now "
        f"{new_master_container} ({new_master_ip})"
    )


def sync_sentinel_ips():
    """Sync Sentinel master IPs with current container IPs.

    Handles the Docker Desktop/Windows behaviour where kill+start reassigns
    container IPs.  Safe after both:
    - kill+start with IP reassignment (only updates when IP truly stale)
    - Sentinel failover (replica is legitimately the master — no update needed)

    Algorithm:
    1. If Sentinel's IP matches any current container IP → already correct, skip.
    2. Otherwise Sentinel has a stale IP (container got new IP).  Find the actual
       master by checking ROLE.  Check replica first so that when both containers
       report master (split-brain / Sentinel not yet reconfigured the rejoined
       original master), we prefer the replica which holds all Sentinel-written data.
    """
    for service_name, (master_container, replica_container) in MASTER_MAP.items():
        sentinel_ip = get_sentinel_master_ip(service_name)
        master_ip = get_container_ip(master_container)
        replica_ip = get_container_ip(replica_container)

        # If Sentinel's IP already matches any current container, nothing to fix
        if sentinel_ip and (sentinel_ip == master_ip or sentinel_ip == replica_ip):
            continue

        # Sentinel has a stale IP — find the actual master via ROLE.
        # Check replica_container first: more likely to hold canonical data
        # after a Sentinel-driven failover.
        actual_master_ip = None
        for container, ip in [(replica_container, replica_ip), (master_container, master_ip)]:
            if not ip:
                continue
            role = _redis_cli(container, "ROLE")
            if role.startswith("master"):
                actual_master_ip = ip
                break

        if actual_master_ip:
            print(f"[topology] IP mismatch for {service_name}: "
                  f"Sentinel={sentinel_ip}, actual master={actual_master_ip}. Fixing...")
            for sentinel in SENTINEL_CONTAINERS:
                _redis_cli(sentinel, "SENTINEL", "REMOVE", service_name, port=26379)
                _redis_cli(sentinel, "SENTINEL", "MONITOR", service_name,
                           actual_master_ip, "6379", "2", port=26379)
                _redis_cli(sentinel, "SENTINEL", "SET", service_name,
                           "auth-pass", "redis", port=26379)
                _redis_cli(sentinel, "SENTINEL", "SET", service_name,
                           "down-after-milliseconds", "5000", port=26379)
                _redis_cli(sentinel, "SENTINEL", "SET", service_name,
                           "failover-timeout", "10000", port=26379)
    _wait_sentinel_converged(timeout=30)


def restart_app_services():
    """Restart all application services to refresh connection pools and reload Lua."""
    # Sync Sentinel IPs first — Docker Desktop can reassign container IPs on kill+start
    sync_sentinel_ips()
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
