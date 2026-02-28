"""Locust load test for the distributed checkout system.

Tasks with realistic checkout flow:
- create_and_checkout (weight=5) — full checkout saga
- find_item (weight=3) — read-only stock lookup
- add_funds (weight=2) — credit top-up

Usage:
    locust -f test/locustfile.py --host http://127.0.0.1:8000 --headless \
           -u 100 -r 10 --run-time 60s
"""
from __future__ import annotations

import random

from locust import HttpUser, between, task


# Pre-allocated ID ranges (set by batch_init: 1000 items, 1000 users)
NUM_ITEMS = 1000
NUM_USERS = 1000


class CheckoutUser(HttpUser):
    wait_time = between(0.1, 0.5)

    @task(5)
    def create_and_checkout(self):
        """Full checkout saga: create order -> add item -> checkout."""
        user_id = random.randint(0, NUM_USERS - 1)
        item_id = random.randint(0, NUM_ITEMS - 1)
        quantity = 1

        with self.client.post(
            f"/orders/create/{user_id}",
            name="/orders/create",
            catch_response=True,
        ) as r:
            if r.status_code != 200:
                r.failure(f"Create order failed: {r.status_code}")
                return
            order_id = r.json().get("order_id")

        with self.client.post(
            f"/orders/addItem/{order_id}/{item_id}/{quantity}",
            name="/orders/addItem",
            catch_response=True,
        ) as r:
            if r.status_code != 200:
                r.failure(f"Add item failed: {r.status_code}")
                return

        with self.client.post(
            f"/orders/checkout/{order_id}",
            name="/orders/checkout",
            catch_response=True,
        ) as r:
            # 200 = success, 400 = business failure (insufficient stock/credit) — both valid
            if r.status_code in (200, 400):
                r.success()
            else:
                r.failure(f"Checkout error: {r.status_code}")

    @task(3)
    def find_item(self):
        """Read-only stock lookup."""
        item_id = random.randint(0, NUM_ITEMS - 1)
        self.client.get(f"/stock/find/{item_id}", name="/stock/find")

    @task(2)
    def add_funds(self):
        """Credit top-up."""
        user_id = random.randint(0, NUM_USERS - 1)
        amount = random.randint(100, 1000)
        self.client.post(
            f"/payment/add_funds/{user_id}/{amount}",
            name="/payment/add_funds",
        )
