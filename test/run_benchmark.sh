#!/usr/bin/env bash
set -euo pipefail

GATEWAY="http://127.0.0.1:8000"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CSV_DIR="$SCRIPT_DIR/benchmark_results"

echo "=== DDS26 Benchmark Suite ==="
echo ""

# -------------------------------------------------------------------
# 1. Docker Compose up + wait for health
# -------------------------------------------------------------------
echo ">>> Starting Docker Compose..."
docker compose up -d

echo ">>> Waiting for services to be healthy..."
python "$SCRIPT_DIR/wait_for_stack.py" --gateway "$GATEWAY" --timeout 120 --interval 2 --stable-rounds 3

# -------------------------------------------------------------------
# 2. Batch init
# -------------------------------------------------------------------
echo ""
echo ">>> Initializing data: 1000 items x 1000 stock, 1000 users x 10000 credit"
curl -sf -X POST "$GATEWAY/stock/batch_init/1000/1000/10" > /dev/null
curl -sf -X POST "$GATEWAY/payment/batch_init/1000/10000" > /dev/null
curl -sf -X POST "$GATEWAY/orders/batch_init/1000/1000/1000/10" > /dev/null
echo "    Batch init complete, waiting for propagation..."
sleep 3

# -------------------------------------------------------------------
# 3. Locust headless: 100 → 200 → 500 users
# -------------------------------------------------------------------
mkdir -p "$CSV_DIR"

run_locust() {
    local users=$1
    local duration=$2
    local label="u${users}"

    echo ""
    echo ">>> Locust: ${users} users for ${duration}s"
    python -m locust \
        -f "$SCRIPT_DIR/locustfile.py" \
        --host "$GATEWAY" \
        --headless \
        -u "$users" \
        -r "$(( users / 5 ))" \
        --run-time "${duration}s" \
        --csv "$CSV_DIR/${label}" \
        --csv-full-history \
        --only-summary \
        2>&1 | tail -5
}

run_locust 100 60
run_locust 200 60
run_locust 500 60

# -------------------------------------------------------------------
# 4. Print results table
# -------------------------------------------------------------------
echo ""
echo "=== Benchmark Results ==="
echo ""
printf "%-8s  %8s  %8s  %8s  %8s  %10s\n" "Users" "p50(ms)" "p95(ms)" "p99(ms)" "Max(ms)" "Req/s"
echo "--------------------------------------------------------------"

for users in 100 200 500; do
    csv="$CSV_DIR/u${users}_stats.csv"
    if [ -f "$csv" ]; then
        # Parse the "Aggregated" row from Locust CSV output
        agg=$(grep -i "aggregated\|total" "$csv" | tail -1 || true)
        if [ -n "$agg" ]; then
            # Locust CSV columns vary; extract key percentiles from stats_history
            hist="$CSV_DIR/u${users}_stats_history.csv"
            if [ -f "$hist" ]; then
                last=$(tail -1 "$hist")
                # Use the stats.csv which has: Type,Name,Req Count,...,50%,66%,75%,80%,90%,95%,98%,99%,99.9%,99.99%,100%
                p50=$(echo "$agg" | awk -F',' '{print $(NF-10)}')
                p95=$(echo "$agg" | awk -F',' '{print $(NF-5)}')
                p99=$(echo "$agg" | awk -F',' '{print $(NF-3)}')
                pmax=$(echo "$agg" | awk -F',' '{print $(NF)}')
                rps=$(echo "$agg" | awk -F',' '{print $10}')
                printf "%-8s  %8s  %8s  %8s  %8s  %10s\n" "$users" "$p50" "$p95" "$p99" "$pmax" "$rps"
            fi
        fi
    else
        printf "%-8s  %8s\n" "$users" "(no data)"
    fi
done

# -------------------------------------------------------------------
# 5. Consistency verification
# -------------------------------------------------------------------
echo ""
echo ">>> Running consistency verification..."

python -c "
import requests, sys

GATEWAY = '$GATEWAY'
errors = 0

# Check a sample of items
for item_id in range(100):
    r = requests.get(f'{GATEWAY}/stock/find/{item_id}')
    if r.status_code == 200:
        data = r.json()
        stock = data.get('stock', -1)
        if stock < 0:
            print(f'  ERROR: item {item_id} has negative stock: {stock}')
            errors += 1

# Check a sample of users
for user_id in range(100):
    r = requests.get(f'{GATEWAY}/payment/find_user/{user_id}')
    if r.status_code == 200:
        data = r.json()
        credit = data.get('credit', -1)
        if credit < 0:
            print(f'  ERROR: user {user_id} has negative credit: {credit}')
            errors += 1

if errors == 0:
    print('  PASS: 0 consistency violations found')
else:
    print(f'  FAIL: {errors} consistency violations found')
    sys.exit(1)
"

echo ""
echo "=== Benchmark complete ==="
