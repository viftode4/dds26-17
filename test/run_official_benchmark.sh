#!/usr/bin/env bash
# =============================================================================
# Official WDM Benchmark Runner — DDS26
#
# Runs BOTH the official consistency test and distributed stress test
# from https://github.com/delftdata/wdm-project-benchmark
#
# Usage:
#   bash test/run_official_benchmark.sh [benchmark_dir] [users] [workers] [duration]
#
# Examples:
#   bash test/run_official_benchmark.sh                              # auto-detect, 1000u, 4w, 90s
#   bash test/run_official_benchmark.sh ../wdm-project-benchmark    # explicit path
#   bash test/run_official_benchmark.sh ../wdm-project-benchmark 2000 8 120
# =============================================================================
set -euo pipefail

GATEWAY="http://127.0.0.1:8000"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# --- Locate the official benchmark repo ---
BENCH_DIR="${1:-}"
if [ -z "$BENCH_DIR" ]; then
    for candidate in \
        "../wdm-project-benchmark" \
        "../../wdm-project-benchmark" \
        "$HOME/Projects/wdm-project-benchmark" \
        "G:/Projects/wdm-project-benchmark"
    do
        if [ -d "$candidate/consistency-test" ]; then
            BENCH_DIR="$candidate"
            break
        fi
    done
fi

if [ -z "$BENCH_DIR" ] || [ ! -d "$BENCH_DIR/consistency-test" ]; then
    echo "ERROR: Could not find wdm-project-benchmark directory."
    echo "  Clone it: git clone https://github.com/delftdata/wdm-project-benchmark.git"
    echo "  Then pass the path: bash test/run_official_benchmark.sh /path/to/wdm-project-benchmark"
    exit 1
fi

USERS="${2:-1000}"
WORKERS="${3:-4}"
DURATION="${4:-90}"
RAMPUP=$(( USERS / 10 ))

echo "=================================================="
echo "  DDS26 Official WDM Benchmark"
echo "  Benchmark dir : $BENCH_DIR"
echo "  Users: $USERS | Workers: $WORKERS | Duration: ${DURATION}s"
echo "=================================================="
echo ""

# -------------------------------------------------------------------
# 1. Health check
# -------------------------------------------------------------------
echo ">>> Checking gateway health..."
for i in $(seq 1 30); do
    if curl -sf "$GATEWAY/orders/health" > /dev/null 2>&1; then
        echo "    Gateway healthy"
        break
    fi
    if [ "$i" -eq 30 ]; then
        echo "ERROR: Gateway not reachable at $GATEWAY"
        echo "  Start services: docker compose up -d"
        exit 1
    fi
    sleep 1
done

# -------------------------------------------------------------------
# 2. Official Consistency Test
# -------------------------------------------------------------------
echo ""
echo "=================================================="
echo "  PHASE 1: Official Consistency Test"
echo "=================================================="
echo ""
cd "$BENCH_DIR/consistency-test"
python run_consistency_test.py
cd - > /dev/null

# -------------------------------------------------------------------
# 3. Init stress test data
# -------------------------------------------------------------------
echo ""
echo "=================================================="
echo "  PHASE 2: Official Stress Test (Distributed)"
echo "=================================================="
echo ""
echo ">>> Initializing stress test data (100K items, 100K users, 100K orders)..."
cd "$BENCH_DIR/stress-test"
python init_orders.py
cd - > /dev/null
echo "    Done"

# -------------------------------------------------------------------
# 4. Start Locust workers
# -------------------------------------------------------------------
echo ""
echo ">>> Starting $WORKERS Locust workers..."
WORKER_PIDS=()

# Must cd into stress-test dir because locustfile imports init_orders which uses ../urls.json
STRESS_DIR="$(cd "$BENCH_DIR/stress-test" && pwd)"

for i in $(seq 1 $WORKERS); do
    (cd "$STRESS_DIR" && python -m locust \
        -f locustfile.py \
        --worker \
        --master-host=127.0.0.1 \
        > /tmp/official_worker_${i}.log 2>&1) &
    WORKER_PIDS+=($!)
done
sleep 2
echo "    Workers started (PIDs: ${WORKER_PIDS[*]})"

# -------------------------------------------------------------------
# 5. Run master
# -------------------------------------------------------------------
RESULTS_DIR="$SCRIPT_DIR/benchmark_results"
mkdir -p "$RESULTS_DIR"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
CSV_PREFIX="$RESULTS_DIR/official_${TIMESTAMP}_u${USERS}"

echo ""
echo ">>> Running $USERS users for ${DURATION}s..."
(cd "$STRESS_DIR" && python -m locust \
    -f locustfile.py \
    --master \
    --host "$GATEWAY" \
    --headless \
    -u "$USERS" \
    -r "$RAMPUP" \
    --run-time "${DURATION}s" \
    --expect-workers "$WORKERS" \
    --csv "$CSV_PREFIX" \
    --only-summary \
    2>&1)

for pid in "${WORKER_PIDS[@]}"; do
    kill "$pid" 2>/dev/null || true
done

# -------------------------------------------------------------------
# 6. Results
# -------------------------------------------------------------------
echo ""
echo "=================================================="
echo "  Results — Official WDM Stress Test"
echo "  ${USERS} users | ${WORKERS} workers | ${DURATION}s"
echo "=================================================="
echo ""

STATS_FILE=$(ls -t "$RESULTS_DIR"/official_*_stats.csv 2>/dev/null | head -1)
if [ -f "$STATS_FILE" ]; then
    python "$SCRIPT_DIR/parse_results.py" "$STATS_FILE" --official
fi

echo ""
echo "  Results saved: $CSV_PREFIX*.csv"
echo ""
echo "=================================================="
echo "  Benchmark complete"
echo "=================================================="
