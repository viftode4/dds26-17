#!/usr/bin/env python3
"""Parse Locust CSV stats and print a summary table.

Usage:
    python test/parse_results.py <stats.csv>
    python test/parse_results.py <stats.csv> --official
"""
import csv, sys, argparse

parser = argparse.ArgumentParser()
parser.add_argument("stats_file")
parser.add_argument("--official", action="store_true",
                    help="Official WDM locustfile (single checkout endpoint)")
args = parser.parse_args()

with open(args.stats_file) as f:
    rows = list(csv.DictReader(f))

agg = next(
    (r for r in rows if r.get("Name", "").lower() in ("aggregated", "total", "")),
    rows[-1] if rows else None,
)
if not agg:
    print("  (no results)")
    sys.exit(0)

total = int(agg.get("Request Count") or 0)
fails = int(agg.get("Failure Count") or 0)
rps   = float(agg.get("Requests/s") or 0)
avg   = float(agg.get("Average Response Time") or 0)
p50   = float(agg.get("50%") or 0)
p95   = float(agg.get("95%") or 0)
p99   = float(agg.get("99%") or 0)
pmax  = float(agg.get("Max Response Time") or 0)
fp    = fails / total * 100 if total else 0
ok    = total - fails

if args.official:
    print(f"  Total requests   : {total:,}")
    print(f"  Successful       : {ok:,}  ({ok/total*100:.1f}%)")
    print(f"  Already paid     : {fails:,}  ({fp:.1f}%)  <- expected (birthday paradox)")
    print(f"  Throughput       : {rps:.0f} req/s")
    print(f"  p50 latency      : {p50:.0f} ms")
    print(f"  p95 latency      : {p95:.0f} ms")
    print(f"  p99 latency      : {p99:.0f} ms")
    print(f"  Max latency      : {pmax:.0f} ms")
else:
    # Per-endpoint breakdown
    endpoints = [r for r in rows if r.get("Name", "").lower() not in ("aggregated", "total", "")]
    print(f"  {'Endpoint':<30} {'req/s':>8} {'p50':>8} {'p95':>8} {'p99':>8} {'fails':>8}")
    print("  " + "-" * 72)
    for r in endpoints:
        name = (r.get("Method", "") + " " + r.get("Name", ""))[:30]
        print(
            f"  {name:<30}"
            f" {float(r.get('Requests/s') or 0):>8.0f}"
            f" {float(r.get('50%') or 0):>8.0f}"
            f" {float(r.get('95%') or 0):>8.0f}"
            f" {float(r.get('99%') or 0):>8.0f}"
            f" {int(r.get('Failure Count') or 0):>8}"
        )
    print("  " + "-" * 72)
    print(f"  {'TOTAL':<30} {rps:>8.0f} {p50:>8.0f} {p95:>8.0f} {p99:>8.0f} {fails:>8}")
    print()
    print(f"  Total requests : {total:,}")
    print(f"  Failure rate   : {fp:.1f}%")
    print(f"  Throughput     : {rps:.0f} req/s")
    print(f"  Avg latency    : {avg:.0f} ms")
