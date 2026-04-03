#!/usr/bin/env bash
# =============================================================================
# Distributed Locust Benchmark — DDS26
#
# Spawns multiple Locust worker processes to saturate the system properly.
# Uses our internal locustfile (realistic mix: create+checkout, reads, funds).
#
# Usage:
#   bash test/run_distributed_benchmark.sh [users] [workers] [duration] [spawn_rate]
#
# Examples:
#   bash test/run_distributed_benchmark.sh                      # 10000u, 8w, 60s, 500/s ramp
#   bash test/run_distributed_benchmark.sh 500 4 90 100       # small run, lower ramp
#
# Locust warns if spawn rate per worker is very high (~>100/s). Use enough workers:
#   workers >= ceil(spawn_rate / 80). For 500/s use at least 8 workers.
#
# On one machine, many workers can *increase* aggregate client RPS vs one process and
# overload the gateway before the server is steady — if you see mass 503/0 failures,
# reduce users, reduce spawn rate, or use a single Locust process (see README).
# =============================================================================
set -euo pipefail

GATEWAY="http://127.0.0.1:8000"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOCUSTFILE="$SCRIPT_DIR/locustfile.py"
RESULTS_DIR="$SCRIPT_DIR/benchmark_results"

USERS="${1:-10000}"
WORKERS="${2:-8}"
DURATION="${3:-60}"
RAMPUP="${4:-500}"

# ~80 users/s per worker avoids Locust heartbeat / spawn warnings in practice
MAX_SPAWN_PER_WORKER=80
if [ "$WORKERS" -gt 0 ] && [ "$(( RAMPUP / WORKERS ))" -gt "$MAX_SPAWN_PER_WORKER" ] 2>/dev/null; then
    echo "WARNING: spawn rate $RAMPUP/s across $WORKERS workers is ~$(( RAMPUP / WORKERS ))/s per worker." >&2
    echo "         Prefer at least $(( (RAMPUP + MAX_SPAWN_PER_WORKER - 1) / MAX_SPAWN_PER_WORKER )) workers, or lower RAMPUP (4th arg)." >&2
    echo "" >&2
fi

echo "=================================================="
echo "  DDS26 Distributed Benchmark"
echo "  Users: $USERS | Workers: $WORKERS | Spawn: ${RAMPUP}/s | Duration: ${DURATION}s"
echo "=================================================="
echo ""

# -------------------------------------------------------------------
# 1. Check services are up
# -------------------------------------------------------------------
echo ">>> Checking gateway health..."
for i in $(seq 1 30); do
    if curl -sf "$GATEWAY/orders/health" > /dev/null 2>&1 && \
       curl -sf "$GATEWAY/orders/__checkout_health" > /dev/null 2>&1; then
        echo "    Gateway healthy"
        break
    fi
    if [ "$i" -eq 30 ]; then
        echo "ERROR: Gateway not healthy. Run: docker compose up -d"
        exit 1
    fi
    sleep 1
done

# -------------------------------------------------------------------
# 2. Batch init
# -------------------------------------------------------------------
echo ""
echo ">>> Initializing data (10000 items x 1000 stock, 10000 users x 1M credit)..."
curl -sf -X POST "$GATEWAY/stock/batch_init/10000/1000/10" > /dev/null
curl -sf -X POST "$GATEWAY/payment/batch_init/10000/1000000" > /dev/null
curl -sf -X POST "$GATEWAY/orders/batch_init/10000/10000/10000/10" > /dev/null
sleep 2
echo "    Done"

# -------------------------------------------------------------------
# 3. Start Locust workers in background
# -------------------------------------------------------------------
echo ""
echo ">>> Starting $WORKERS Locust workers..."
mkdir -p "$RESULTS_DIR"
WORKER_PIDS=()

for i in $(seq 1 $WORKERS); do
    python -m locust \
        -f "$LOCUSTFILE" \
        --worker \
        --master-host=127.0.0.1 \
        > /tmp/locust_worker_${i}.log 2>&1 &
    WORKER_PIDS+=($!)
done
sleep 2
echo "    Workers started (PIDs: ${WORKER_PIDS[*]})"

# -------------------------------------------------------------------
# 4. Run master (blocks until complete)
# -------------------------------------------------------------------
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
CSV_PREFIX="$RESULTS_DIR/${TIMESTAMP}_u${USERS}"

echo ""
echo ">>> Running master: $USERS users for ${DURATION}s..."
python -m locust \
    -f "$LOCUSTFILE" \
    --master \
    --host "$GATEWAY" \
    --headless \
    -u "$USERS" \
    -r "$RAMPUP" \
    --run-time "${DURATION}s" \
    --expect-workers "$WORKERS" \
    --csv "$CSV_PREFIX" \
    --csv-full-history \
    --only-summary \
    2>&1

# -------------------------------------------------------------------
# 5. Kill workers
# -------------------------------------------------------------------
for pid in "${WORKER_PIDS[@]}"; do
    kill "$pid" 2>/dev/null || true
done

# -------------------------------------------------------------------
# 6. Parse and display results
# -------------------------------------------------------------------
echo ""
echo "=================================================="
echo "  Results — ${USERS} users, ${WORKERS} workers, ${RAMPUP}/s ramp, ${DURATION}s"
echo "=================================================="
echo ""

STATS_FILE=$(ls -t "$RESULTS_DIR"/*_stats.csv 2>/dev/null | head -1)
if [ -f "$STATS_FILE" ]; then
    python "$SCRIPT_DIR/parse_results.py" "$STATS_FILE"
fi

# -------------------------------------------------------------------
# 7. Consistency check
# -------------------------------------------------------------------
echo ""
echo ">>> Consistency verification (sample 100 items + 100 users)..."
python "$SCRIPT_DIR/check_consistency.py" "$GATEWAY"

echo ""
echo "=================================================="
echo "  Benchmark complete"
echo "=================================================="
