"""Analyze stress test results: Locust CSV + Docker logs."""
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

import pandas as pd


def analyze_locust_history(csv_path: str):
    """Analyze Locust stats_history.csv for throughput/latency over time."""
    df = pd.read_csv(csv_path)
    # Filter to Aggregated rows only
    df = df[df["Name"] == "Aggregated"].copy()
    df["elapsed_s"] = df["Timestamp"] - df["Timestamp"].iloc[0]

    print("=" * 70)
    print("LOCUST TIME-SERIES ANALYSIS")
    print("=" * 70)

    # Phase analysis: ramp-up vs steady-state
    ramp_end = df[df["User Count"] == df["User Count"].max()].iloc[0]["elapsed_s"]
    steady = df[df["elapsed_s"] >= ramp_end]
    ramp = df[df["elapsed_s"] < ramp_end]

    print(f"\nRamp-up phase: 0 - {ramp_end:.0f}s ({len(ramp)} samples)")
    print(f"Steady-state phase: {ramp_end:.0f}s - {df['elapsed_s'].iloc[-1]:.0f}s ({len(steady)} samples)")

    if len(steady) > 0:
        print(f"\n--- Steady-State Metrics ---")
        print(f"  Throughput:  avg={steady['Requests/s'].mean():.0f} req/s, "
              f"min={steady['Requests/s'].min():.0f}, max={steady['Requests/s'].max():.0f}")
        print(f"  p50:  avg={steady['50%'].mean():.0f}ms, min={steady['50%'].min():.0f}ms, max={steady['50%'].max():.0f}ms")
        print(f"  p95:  avg={steady['95%'].mean():.0f}ms, min={steady['95%'].min():.0f}ms, max={steady['95%'].max():.0f}ms")
        print(f"  p99:  avg={steady['99%'].mean():.0f}ms, min={steady['99%'].min():.0f}ms, max={steady['99%'].max():.0f}ms")

    # Detect latency spikes (p99 > 2x average p99)
    avg_p99 = steady["99%"].mean() if len(steady) > 0 else df["99%"].mean()
    spikes = df[df["99%"] > avg_p99 * 2]
    if len(spikes) > 0:
        print(f"\n--- Latency Spikes (p99 > {avg_p99 * 2:.0f}ms) ---")
        for _, row in spikes.iterrows():
            print(f"  t={row['elapsed_s']:.0f}s: p99={row['99%']:.0f}ms, "
                  f"throughput={row['Requests/s']:.0f} req/s, users={row['User Count']}")
    else:
        print(f"\n  No latency spikes detected (threshold: p99 > {avg_p99 * 2:.0f}ms)")

    # Throughput stability
    if len(steady) > 2:
        cv = steady["Requests/s"].std() / steady["Requests/s"].mean() * 100
        print(f"\n--- Throughput Stability ---")
        print(f"  Coefficient of variation: {cv:.1f}%", end="")
        if cv < 10:
            print(" (excellent)")
        elif cv < 20:
            print(" (good)")
        else:
            print(" (unstable - investigate)")

    # Latency trend (is it increasing over time?)
    if len(steady) > 5:
        first_half = steady.iloc[:len(steady)//2]
        second_half = steady.iloc[len(steady)//2:]
        p50_drift = second_half["50%"].mean() - first_half["50%"].mean()
        p99_drift = second_half["99%"].mean() - first_half["99%"].mean()
        print(f"\n--- Latency Drift (2nd half vs 1st half) ---")
        print(f"  p50 drift: {p50_drift:+.0f}ms", end="")
        if abs(p50_drift) < 20:
            print(" (stable)")
        elif p50_drift > 0:
            print(" (DEGRADING - possible memory/GC/connection leak)")
        else:
            print(" (improving)")
        print(f"  p99 drift: {p99_drift:+.0f}ms", end="")
        if abs(p99_drift) < 50:
            print(" (stable)")
        elif p99_drift > 0:
            print(" (DEGRADING)")
        else:
            print(" (improving)")

    # Per-second throughput breakdown for first 20s and last 20s
    print(f"\n--- Throughput Over Time (every 10s) ---")
    for i in range(0, int(df["elapsed_s"].max()) + 1, 10):
        window = df[(df["elapsed_s"] >= i) & (df["elapsed_s"] < i + 10)]
        if len(window) > 0:
            rps = window["Requests/s"].mean()
            p50 = window["50%"].mean()
            p99 = window["99%"].mean()
            users = window["User Count"].iloc[-1]
            bar = "#" * int(rps / 20)
            print(f"  {i:>4}s-{i+10:<4}s | {users:>5} users | {rps:>7.0f} req/s | p50={p50:>5.0f}ms p99={p99:>5.0f}ms | {bar}")

    return df


def analyze_order_logs(log_path: str):
    """Parse order service JSON logs for checkout patterns."""
    print("\n" + "=" * 70)
    print("ORDER SERVICE LOG ANALYSIS")
    print("=" * 70)

    events = []
    errors = []
    protocols = Counter()
    outcomes = Counter()
    error_types = Counter()
    instance_counts = Counter()
    timestamps = []

    with open(log_path) as f:
        for line in f:
            # Extract container name
            match = re.match(r"([\w-]+)\s+\|\s+(.*)", line.strip())
            if not match:
                continue
            container, payload = match.groups()

            # Skip non-JSON lines
            if not payload.startswith("{"):
                continue

            try:
                entry = json.loads(payload)
            except json.JSONDecodeError:
                continue

            event = entry.get("event", "")
            ts_str = entry.get("timestamp", "")

            if "Checkout" in event:
                try:
                    ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                    timestamps.append(ts)
                except (ValueError, AttributeError):
                    pass

                instance_counts[container] += 1

                if "successful" in event.lower():
                    outcomes["success"] += 1
                    protocols[entry.get("protocol", "unknown")] += 1
                elif "failed" in event.lower():
                    outcomes["failed"] += 1
                    error = entry.get("error", "unknown")
                    error_types[error] += 1
                elif "already" in event.lower():
                    outcomes["already_paid"] += 1

            if entry.get("level") == "error":
                errors.append({
                    "container": container,
                    "event": event,
                    "error": entry.get("error", ""),
                    "timestamp": ts_str,
                })

    total = sum(outcomes.values())
    print(f"\nTotal checkouts logged: {total}")

    print(f"\n--- Outcomes ---")
    for outcome, count in outcomes.most_common():
        pct = count / total * 100 if total else 0
        print(f"  {outcome:>15}: {count:>6} ({pct:.1f}%)")

    print(f"\n--- Protocol Distribution ---")
    for proto, count in protocols.most_common():
        pct = count / outcomes.get("success", 1) * 100
        print(f"  {proto:>8}: {count:>6} ({pct:.1f}% of successes)")

    print(f"\n--- Failure Breakdown ---")
    if error_types:
        for err, count in error_types.most_common():
            print(f"  {err:>30}: {count:>6}")
    else:
        print("  No failures!")

    print(f"\n--- Load Distribution Across Instances ---")
    for container, count in instance_counts.most_common():
        pct = count / total * 100 if total else 0
        bar = "#" * int(pct / 2)
        print(f"  {container:>25}: {count:>6} ({pct:.1f}%) {bar}")

    # Throughput over time from logs
    if timestamps:
        timestamps.sort()
        start = timestamps[0]
        end = timestamps[-1]
        duration = (end - start).total_seconds()
        print(f"\n--- Log Timeline ---")
        print(f"  First checkout: {start.strftime('%H:%M:%S')}")
        print(f"  Last checkout:  {end.strftime('%H:%M:%S')}")
        print(f"  Duration:       {duration:.0f}s")
        print(f"  Avg throughput: {total / duration:.0f} checkouts/s")

        # Per-10s buckets
        print(f"\n--- Checkout Rate Over Time (from logs, 10s buckets) ---")
        bucket_size = 10
        buckets_success = defaultdict(int)
        buckets_fail = defaultdict(int)

        # Re-parse to get per-bucket data
        with open(log_path) as f:
            for line in f:
                match = re.match(r"([\w-]+)\s+\|\s+(.*)", line.strip())
                if not match:
                    continue
                _, payload = match.groups()
                if not payload.startswith("{"):
                    continue
                try:
                    entry = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                event = entry.get("event", "")
                if "Checkout" not in event:
                    continue
                ts_str = entry.get("timestamp", "")
                try:
                    ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                    bucket = int((ts - start).total_seconds() // bucket_size) * bucket_size
                    if "successful" in event.lower():
                        buckets_success[bucket] += 1
                    elif "failed" in event.lower():
                        buckets_fail[bucket] += 1
                except (ValueError, AttributeError):
                    continue

        all_buckets = sorted(set(list(buckets_success.keys()) + list(buckets_fail.keys())))
        for b in all_buckets:
            s = buckets_success.get(b, 0)
            f = buckets_fail.get(b, 0)
            total_b = s + f
            rate = total_b / bucket_size
            fail_pct = f / total_b * 100 if total_b else 0
            bar_s = "#" * int(rate / 15)
            bar_f = "!" * int(f / bucket_size / 15) if f else ""
            print(f"  {b:>4}s-{b+bucket_size:<4}s | {rate:>5.0f}/s | ok={s:>5} fail={f:>4} ({fail_pct:>4.1f}%) | {bar_s}{bar_f}")

    # Errors section
    if errors:
        print(f"\n--- Errors ({len(errors)} total) ---")
        for e in errors[:20]:
            print(f"  [{e['container']}] {e['timestamp']}: {e['event']} - {e['error']}")
        if len(errors) > 20:
            print(f"  ... and {len(errors) - 20} more")
    else:
        print(f"\n  No errors in logs!")


def main():
    csv_path = sys.argv[1] if len(sys.argv) > 1 else "G:/Projects/wdm-project-benchmark/stress-test/official_stress_stats_history.csv"
    log_path = sys.argv[2] if len(sys.argv) > 2 else "/tmp/order_logs.txt"

    analyze_locust_history(csv_path)

    if Path(log_path).exists():
        analyze_order_logs(log_path)


if __name__ == "__main__":
    main()
