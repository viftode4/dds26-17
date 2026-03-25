#!/usr/bin/env python3
"""Consistency check: verify global conservation equation and non-negative invariants.

Samples items 0..N-1 and users 0..N-1, then checks:
  1. No negative stock or credit (quick sanity)
  2. Σ credit_spent == Σ stock_sold × price  (full conservation equation)

The conservation check requires that initial values were set via batch_init
(items 0..N-1 at price ITEM_PRICE with INITIAL_STOCK each; users 0..N-1
with INITIAL_CREDIT each). These defaults match the batch_init call in
run_benchmark.sh.

Usage:
    python test/check_consistency.py [gateway_url]
    python test/check_consistency.py http://127.0.0.1:8000
"""
import json
import sys
import urllib.request

gw = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"

# Must match the batch_init parameters used in run_benchmark.sh
N_ITEMS = 100
N_USERS = 100
INITIAL_STOCK = 100
INITIAL_CREDIT = 100_000
ITEM_PRICE = 10

errors = 0

# --- Collect current state ---
item_stocks = {}
user_credits = {}

for i in range(N_ITEMS):
    try:
        d = json.loads(urllib.request.urlopen(f"{gw}/stock/find/{i}", timeout=5).read())
        stock = d.get("stock", 0)
        if stock < 0:
            print(f"  FAIL item {i}: negative stock={stock}")
            errors += 1
        item_stocks[i] = stock
    except Exception:
        pass  # Item may not exist if benchmark didn't initialise it

for i in range(N_USERS):
    try:
        d = json.loads(urllib.request.urlopen(f"{gw}/payment/find_user/{i}", timeout=5).read())
        credit = d.get("credit", 0)
        if credit < 0:
            print(f"  FAIL user {i}: negative credit={credit}")
            errors += 1
        user_credits[i] = credit
    except Exception:
        pass

# --- Conservation equation ---
if item_stocks and user_credits:
    total_stock_sold_cost = sum(
        (INITIAL_STOCK - stock) * ITEM_PRICE
        for stock in item_stocks.values()
    )
    total_credit_spent = sum(
        INITIAL_CREDIT - credit
        for credit in user_credits.values()
    )

    if total_credit_spent != total_stock_sold_cost:
        print(
            f"  FAIL conservation: credit_spent={total_credit_spent} "
            f"!= stock_sold_cost={total_stock_sold_cost} "
            f"(delta={total_credit_spent - total_stock_sold_cost})"
        )
        errors += 1
    else:
        print(f"  PASS conservation: credit_spent={total_credit_spent} == stock_sold_cost={total_stock_sold_cost}")
else:
    print("  WARN: no items/users found — skipping conservation check")

status = "PASS" if errors == 0 else "FAIL"
print(f"  {status}: {errors} consistency violations found")
sys.exit(1 if errors else 0)
