#!/usr/bin/env python3
"""Quick consistency check: sample 100 items and 100 users for negative values.

Usage:
    python test/check_consistency.py [gateway_url]
    python test/check_consistency.py http://127.0.0.1:8000
"""
import urllib.request, json, sys

gw = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
errors = 0

for i in range(100):
    try:
        d = json.loads(urllib.request.urlopen(f"{gw}/stock/find/{i}", timeout=5).read())
        if d.get("stock", 0) < 0:
            print(f"  FAIL item {i}: negative stock={d['stock']}")
            errors += 1
    except Exception:
        pass

for i in range(100):
    try:
        d = json.loads(urllib.request.urlopen(f"{gw}/payment/find_user/{i}", timeout=5).read())
        if d.get("credit", 0) < 0:
            print(f"  FAIL user {i}: negative credit={d['credit']}")
            errors += 1
    except Exception:
        pass

status = "PASS" if errors == 0 else "FAIL"
print(f"  {status}: {errors} consistency violations found")
sys.exit(1 if errors else 0)
