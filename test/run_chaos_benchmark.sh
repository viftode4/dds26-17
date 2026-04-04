#!/usr/bin/env bash
# =============================================================================
# Chaos Benchmark — DDS26
#
# Runs Locust load while simultaneously injecting failures:
#   - Kill/restart service instances mid-load
#   - Kill/restart Redis masters (triggers Sentinel failover)
#   - Kill multiple containers simultaneously
#   - Rapid kill/restart cycles
#
# Usage:
#   bash test/run_chaos_benchmark.sh [scenario] [users] [workers]
#
# Scenarios:
#   all          Run all scenarios sequentially (default)
#   service      Kill service instances only
#   redis        Kill Redis masters (Sentinel failover)
#   multi        Kill multiple containers simultaneously
#   cascade      Kill service + its DB at the same time
#
# Examples:
#   bash test/run_chaos_benchmark.sh
#   bash test/run_chaos_benchmark.sh redis 500 4
#   bash test/run_chaos_benchmark.sh all 300 4
# =============================================================================
set -euo pipefail

GATEWAY="http://127.0.0.1:8000"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOCUSTFILE="$SCRIPT_DIR/locustfile.py"
RESULTS_DIR="$SCRIPT_DIR/benchmark_results"
mkdir -p "$RESULTS_DIR"

SCENARIO="${1:-all}"
USERS="${2:-300}"
WORKERS="${3:-4}"
RAMPUP=$(( USERS / 10 ))

# Colors
RED='\033[0;31m'
GRN='\033[0;32m'
YLW='\033[1;33m'
BLU='\033[0;34m'
NC='\033[0m'

log_kill()    { echo -e "${RED}[CHAOS]${NC} $*"; }
log_recover() { echo -e "${GRN}[RECOVER]${NC} $*"; }
log_info()    { echo -e "${BLU}[INFO]${NC} $*"; }
log_pass()    { echo -e "${GRN}[PASS]${NC} $*"; }
log_fail()    { echo -e "${RED}[FAIL]${NC} $*"; }

# -------------------------------------------------------------------
# Helper: wait for a URL to respond
# -------------------------------------------------------------------
wait_healthy() {
    local url="$1"
    local label="${2:-service}"
    local max="${3:-20}"
    for i in $(seq 1 $max); do
        if curl -sf "$url" > /dev/null 2>&1; then
            log_recover "$label recovered in ${i}s"
            return 0
        fi
        sleep 1
    done
    log_fail "$label did NOT recover within ${max}s"
    return 1
}

# -------------------------------------------------------------------
# Helper: start Locust load in background, return PID
# -------------------------------------------------------------------
start_load() {
    local csv_prefix="$1"
    local worker_pids=()
    for i in $(seq 1 $WORKERS); do
        python -m locust -f "$LOCUSTFILE" --worker --master-host=127.0.0.1 \
            > /tmp/chaos_worker_${i}.log 2>&1 &
        worker_pids+=($!)
    done
    sleep 2

    python -m locust -f "$LOCUSTFILE" --master \
        --host "$GATEWAY" --headless \
        -u "$USERS" -r "$RAMPUP" \
        --run-time 999s \
        --expect-workers "$WORKERS" \
        --csv "$csv_prefix" \
        > /tmp/chaos_master.log 2>&1 &

    local master_pid=$!
    # Store worker pids for cleanup
    echo "${worker_pids[*]}" > /tmp/chaos_worker_pids
    echo "$master_pid"
}

stop_load() {
    local master_pid="$1"
    kill "$master_pid" 2>/dev/null || true
    if [ -f /tmp/chaos_worker_pids ]; then
        for pid in $(cat /tmp/chaos_worker_pids); do
            kill "$pid" 2>/dev/null || true
        done
    fi
    sleep 2
}

# -------------------------------------------------------------------
# Helper: measure current RPS from Locust master log
# -------------------------------------------------------------------
get_rps() {
    grep -oP "(?<=Requests/s: )\d+\.?\d*" /tmp/chaos_master.log 2>/dev/null | tail -1 || echo "?"
}

# -------------------------------------------------------------------
# Helper: run a kill scenario with timing
# -------------------------------------------------------------------
run_kill_scenario() {
    local label="$1"
    local container="$2"
    local check_url="$3"

    echo ""
    echo -e "${YLW}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    log_info "Scenario: $label"
    echo -e "${YLW}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"

    local t_kill=$(date +%s%3N)
    log_kill "Killing $container..."
    docker stop "$container" > /dev/null
    log_kill "$container stopped at t=0ms"

    local recovered=false
    local t_recover
    for i in $(seq 1 20); do
        if curl -sf "$check_url" > /dev/null 2>&1; then
            t_recover=$(date +%s%3N)
            local ms=$(( t_recover - t_kill ))
            log_recover "$label recovered in ${ms}ms"
            recovered=true
            break
        fi
        sleep 0.5
    done

    if [ "$recovered" = false ]; then
        log_fail "$label: no recovery after 10s!"
    fi

    sleep 3
    log_info "Restarting $container..."
    docker start "$container" > /dev/null
    sleep 5
}

# -------------------------------------------------------------------
# Init data
# -------------------------------------------------------------------
init_data() {
    log_info "Initializing data..."
    curl -sf -X POST "$GATEWAY/stock/batch_init/1000/1000/10" > /dev/null
    curl -sf -X POST "$GATEWAY/payment/batch_init/1000/1000000" > /dev/null
    curl -sf -X POST "$GATEWAY/orders/batch_init/1000/1000/1000/10" > /dev/null
    sleep 2
    log_info "Data ready"
}

# -------------------------------------------------------------------
# Health check
# -------------------------------------------------------------------
echo ""
echo "=================================================="
echo "  DDS26 Chaos Benchmark"
echo "  Scenario: $SCENARIO | Users: $USERS | Workers: $WORKERS"
echo "=================================================="

log_info "Checking gateway health..."
for i in $(seq 1 30); do
    curl -sf "$GATEWAY/orders/health" > /dev/null 2>&1 && log_info "Gateway healthy" && break
    [ "$i" -eq 30 ] && echo "ERROR: gateway not up" && exit 1
    sleep 1
done

init_data

# ===================================================================
# SCENARIO 1: Service instance kill (HAProxy should route around it)
# ===================================================================
run_scenario_service() {
    echo ""
    echo "=================================================="
    echo "  SCENARIO 1: Service Instance Kills Under Load"
    echo "  (HAProxy leastconn should route around dead instance)"
    echo "=================================================="

    TIMESTAMP=$(date +%Y%m%d_%H%M%S)
    CSV="$RESULTS_DIR/chaos_service_${TIMESTAMP}"
    log_info "Starting $USERS users..."
    MASTER_PID=$(start_load "$CSV")
    sleep 15  # let load ramp up

    run_kill_scenario "order-service-1 killed"  \
        "distributed-data-systems-order-service-1-1" \
        "$GATEWAY/orders/health"

    run_kill_scenario "stock-service killed" \
        "distributed-data-systems-stock-service-1" \
        "$GATEWAY/stock/find/1"

    run_kill_scenario "payment-service killed" \
        "distributed-data-systems-payment-service-1" \
        "$GATEWAY/payment/find_user/1"

    run_kill_scenario "order-service-2 killed" \
        "distributed-data-systems-order-service-2-1" \
        "$GATEWAY/orders/health"

    sleep 10
    stop_load "$MASTER_PID"

    echo ""
    log_info "Results (service kills):"
    STATS=$(ls -t "$RESULTS_DIR"/chaos_service_*_stats.csv 2>/dev/null | head -1)
    [ -f "$STATS" ] && python "$SCRIPT_DIR/parse_results.py" "$STATS" || echo "  (no CSV)"

    echo ""
    log_info "Consistency check after service kills..."
    python "$SCRIPT_DIR/check_consistency.py" "$GATEWAY"
}

# ===================================================================
# SCENARIO 2: Redis master kill (Sentinel failover under load)
# ===================================================================
run_scenario_redis() {
    echo ""
    echo "=================================================="
    echo "  SCENARIO 2: Redis Master Kills Under Load"
    echo "  (Sentinel should elect replica as new master)"
    echo "=================================================="

    TIMESTAMP=$(date +%Y%m%d_%H%M%S)
    CSV="$RESULTS_DIR/chaos_redis_${TIMESTAMP}"
    log_info "Starting $USERS users..."
    MASTER_PID=$(start_load "$CSV")
    sleep 15

    run_kill_scenario "order-db (Redis master) killed" \
        "distributed-data-systems-order-db-1" \
        "$GATEWAY/orders/health"

    # Check sentinel promoted replica
    log_info "Sentinel failover log:"
    docker logs distributed-data-systems-sentinel-1-1 2>&1 | grep "switch-master\|promoted\|failover" | tail -3 || true

    run_kill_scenario "stock-db (Redis master) killed" \
        "distributed-data-systems-stock-db-1" \
        "$GATEWAY/stock/find/1"

    run_kill_scenario "payment-db (Redis master) killed" \
        "distributed-data-systems-payment-db-1" \
        "$GATEWAY/payment/find_user/1"

    sleep 10
    stop_load "$MASTER_PID"

    echo ""
    log_info "Results (Redis master kills):"
    STATS=$(ls -t "$RESULTS_DIR"/chaos_redis_*_stats.csv 2>/dev/null | head -1)
    [ -f "$STATS" ] && python "$SCRIPT_DIR/parse_results.py" "$STATS" || echo "  (no CSV)"

    echo ""
    log_info "Consistency check after Redis failovers..."
    python "$SCRIPT_DIR/check_consistency.py" "$GATEWAY"
}

# ===================================================================
# SCENARIO 3: Multi-kill (kill 2 containers simultaneously)
# ===================================================================
run_scenario_multi() {
    echo ""
    echo "=================================================="
    echo "  SCENARIO 3: Simultaneous Multi-Container Kill"
    echo "  (Kill order-service-1 + stock-service-2 at same time)"
    echo "=================================================="

    TIMESTAMP=$(date +%Y%m%d_%H%M%S)
    CSV="$RESULTS_DIR/chaos_multi_${TIMESTAMP}"
    log_info "Starting $USERS users..."
    MASTER_PID=$(start_load "$CSV")
    sleep 15

    echo ""
    log_kill "Killing order-service-1 + stock-service-2 simultaneously..."
    local t_kill=$(date +%s%3N)
    docker stop distributed-data-systems-order-service-1-1 \
                 distributed-data-systems-stock-service-2-1 > /dev/null
    log_kill "Both containers stopped"

    # Wait for recovery
    local recovered=false
    for i in $(seq 1 20); do
        if curl -sf "$GATEWAY/orders/health" > /dev/null 2>&1 && \
           curl -sf "$GATEWAY/stock/find/1" > /dev/null 2>&1; then
            local ms=$(( $(date +%s%3N) - t_kill ))
            log_recover "Both services healthy again in ${ms}ms"
            recovered=true
            break
        fi
        sleep 0.5
    done
    [ "$recovered" = false ] && log_fail "Multi-kill: did not recover within 10s!"

    sleep 3
    docker start distributed-data-systems-order-service-1-1 \
                  distributed-data-systems-stock-service-2-1 > /dev/null
    sleep 5

    echo ""
    log_kill "Killing payment-service + order-service-2 simultaneously..."
    t_kill=$(date +%s%3N)
    docker stop distributed-data-systems-payment-service-1 \
                 distributed-data-systems-order-service-2-1 > /dev/null

    for i in $(seq 1 20); do
        if curl -sf "$GATEWAY/payment/find_user/1" > /dev/null 2>&1 && \
           curl -sf "$GATEWAY/orders/health" > /dev/null 2>&1; then
            local ms=$(( $(date +%s%3N) - t_kill ))
            log_recover "Both services healthy again in ${ms}ms"
            break
        fi
        sleep 0.5
    done

    sleep 3
    docker start distributed-data-systems-payment-service-1 \
                  distributed-data-systems-order-service-2-1 > /dev/null
    sleep 5

    sleep 10
    stop_load "$MASTER_PID"

    echo ""
    log_info "Results (multi-kill):"
    STATS=$(ls -t "$RESULTS_DIR"/chaos_multi_*_stats.csv 2>/dev/null | head -1)
    [ -f "$STATS" ] && python "$SCRIPT_DIR/parse_results.py" "$STATS" || echo "  (no CSV)"

    echo ""
    log_info "Consistency check after multi-kills..."
    python "$SCRIPT_DIR/check_consistency.py" "$GATEWAY"
}

# ===================================================================
# SCENARIO 4: Cascade kill (service + its DB simultaneously)
# ===================================================================
run_scenario_cascade() {
    echo ""
    echo "=================================================="
    echo "  SCENARIO 4: Cascade Kill — Service + DB Together"
    echo "  (Worst case: both the service and its Redis master die)"
    echo "=================================================="

    TIMESTAMP=$(date +%Y%m%d_%H%M%S)
    CSV="$RESULTS_DIR/chaos_cascade_${TIMESTAMP}"
    log_info "Starting $USERS users..."
    MASTER_PID=$(start_load "$CSV")
    sleep 15

    echo ""
    log_kill "Cascade: killing order-service-1 + order-db simultaneously..."
    local t_kill=$(date +%s%3N)
    docker stop distributed-data-systems-order-service-1-1 \
                 distributed-data-systems-order-db-1 > /dev/null
    log_kill "order-service-1 + order-db stopped (Sentinel should promote replica)"

    local cascade_recovered=false
    for i in $(seq 1 30); do
        if curl -sf "$GATEWAY/orders/health" > /dev/null 2>&1; then
            local ms=$(( $(date +%s%3N) - t_kill ))
            log_recover "Order service healthy again in ${ms}ms"
            cascade_recovered=true
            break
        fi
        sleep 0.5 || true
    done
    [ "$cascade_recovered" = false ] && log_fail "Cascade: did not recover within 15s!" || true

    log_info "Sentinel failover confirmation:"
    docker logs distributed-data-systems-sentinel-1-1 2>&1 | grep "switch-master" | tail -2 || true

    sleep 5
    docker start distributed-data-systems-order-service-1-1 \
                  distributed-data-systems-order-db-1 > /dev/null || true
    sleep 10

    sleep 10
    stop_load "$MASTER_PID" || true

    echo ""
    log_info "Results (cascade kill):"
    STATS=$(ls -t "$RESULTS_DIR"/chaos_cascade_*_stats.csv 2>/dev/null | head -1)
    [ -f "$STATS" ] && python "$SCRIPT_DIR/parse_results.py" "$STATS" || echo "  (no CSV)"

    echo ""
    log_info "Consistency check after cascade kill..."
    python "$SCRIPT_DIR/check_consistency.py" "$GATEWAY"
}

# ===================================================================
# Run selected scenario(s)
# ===================================================================
case "$SCENARIO" in
    service)  run_scenario_service ;;
    redis)    run_scenario_redis ;;
    multi)    run_scenario_multi ;;
    cascade)  run_scenario_cascade ;;
    all)
        run_scenario_service
        sleep 10
        init_data
        run_scenario_redis
        sleep 10
        init_data
        run_scenario_multi
        sleep 10
        init_data
        run_scenario_cascade
        ;;
    *)
        echo "Unknown scenario: $SCENARIO"
        echo "Use: all | service | redis | multi | cascade"
        exit 1
        ;;
esac

echo ""
echo "=================================================="
echo "  Chaos Benchmark Complete"
echo "=================================================="
