"""Wait for the full stack to become stably healthy."""

from __future__ import annotations

import argparse
import sys
import time

import requests


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--gateway", default="http://127.0.0.1:8000")
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--interval", type=float, default=2.0)
    parser.add_argument("--stable-rounds", type=int, default=3)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    endpoints = ["/orders/health", "/stock/health", "/payment/health"]
    deadline = time.monotonic() + args.timeout
    healthy_rounds = 0

    while time.monotonic() < deadline:
        all_ok = True
        results: list[str] = []
        for path in endpoints:
            try:
                response = requests.get(
                    f"{args.gateway}{path}",
                    timeout=min(5.0, args.interval + 1.0),
                )
                results.append(f"{path}={response.status_code}")
                if response.status_code != 200:
                    all_ok = False
            except requests.RequestException as exc:
                results.append(f"{path}=ERR({exc})")
                all_ok = False

        if all_ok:
            healthy_rounds += 1
            print(
                f"[wait_for_stack] healthy round {healthy_rounds}/{args.stable_rounds}"
            )
            if healthy_rounds >= args.stable_rounds:
                print("[wait_for_stack] stack is stable")
                return 0
        else:
            healthy_rounds = 0
            print("[wait_for_stack] not ready -> " + " | ".join(results))

        time.sleep(args.interval)

    print(
        f"[wait_for_stack] timed out after {args.timeout:.0f}s waiting for stack health",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
