"""
Crash Recovery for the Order Service.

Runs at startup to handle incomplete transactions:
- PENDING orders: attempt compensation/abort based on checkout_step
- Works for both SAGA and 2PC modes
"""
import json
import logging
import time

from msgspec import msgpack

logger = logging.getLogger("order-service")


def run_recovery(db, event_bus, tx_mode: str):
    """
    Scan for incomplete transactions and attempt recovery.

    Args:
        db: Order-db Redis connection
        event_bus: Event-bus Redis connection
        tx_mode: 'saga' or '2pc'
    """
    from app import OrderValue

    logger.info(f"Running startup recovery (mode: {tx_mode})...")
    recovered = 0

    try:
        cursor = 0
        while True:
            cursor, keys = db.scan(cursor, match="*", count=100)
            for key in keys:
                key_str = key.decode() if isinstance(key, bytes) else key
                # Skip non-order keys (idempotency keys, etc.)
                if key_str.startswith("op:") or key_str.startswith("2pc:") or key_str.startswith("tx:"):
                    continue

                try:
                    entry = db.get(key)
                    if entry is None:
                        continue
                    order = msgpack.decode(entry, type=OrderValue)
                except Exception:
                    continue

                # Only recover orders that are stuck in PENDING
                if order.checkout_status != "PENDING":
                    continue

                logger.warning(f"Found incomplete transaction: {key_str} "
                             f"(step: {order.checkout_step})")

                _recover_order(key_str, order, db, event_bus, tx_mode)
                recovered += 1

            if cursor == 0:
                break

    except Exception as e:
        logger.error(f"Recovery scan error: {e}")

    logger.info(f"Recovery complete: {recovered} transactions recovered")


def _recover_order(order_id: str, order, db, event_bus, tx_mode: str):
    """
    Recover a single incomplete order.

    Strategy: When in doubt, abort. It's safer to abort and let the user retry
    than to commit a potentially inconsistent transaction.
    """
    from app import save_order
    from collections import defaultdict

    # Build items_quantities from order
    items_quantities = defaultdict(int)
    for item_id, quantity in order.items:
        items_quantities[item_id] += quantity

    logger.info(f"Recovering order {order_id}: step={order.checkout_step}, mode={tx_mode}")

    if tx_mode == "saga":
        _recover_saga(order_id, order, items_quantities, db, event_bus)
    else:
        _recover_2pc(order_id, order, items_quantities, db, event_bus)


def _recover_saga(order_id, order, items_quantities, db, event_bus):
    """Recover a SAGA transaction by compensating all potentially completed steps."""
    from app import save_order

    TIMEOUT = 10

    # Conservative approach: send compensate for all items that might have been reserved
    if order.checkout_step in ("STOCK", "PAYMENT", "STOCK_FAILED", "PAYMENT_FAILED"):
        # Compensate stock
        for item_id, quantity in items_quantities.items():
            event_bus.xadd("stock-stream", {
                "cmd": "compensate",
                "order_id": order_id,
                "item_id": item_id,
                "quantity": str(quantity),
            })
            response_key = f"response:{order_id}:stock"
            event_bus.blpop(response_key, timeout=TIMEOUT)

        # Compensate payment if we got past the stock phase
        if order.checkout_step in ("PAYMENT", "PAYMENT_FAILED"):
            event_bus.xadd("payment-stream", {
                "cmd": "compensate",
                "order_id": order_id,
                "user_id": order.user_id,
                "amount": str(order.total_cost),
            })
            response_key = f"response:{order_id}:payment"
            event_bus.blpop(response_key, timeout=TIMEOUT)

    order.checkout_status = "ABORTED"
    order.checkout_step = "RECOVERED"
    save_order(db, order_id, order)
    logger.info(f"SAGA recovery complete for {order_id}: ABORTED")


def _recover_2pc(order_id, order, items_quantities, db, event_bus):
    """Recover a 2PC transaction by aborting all participants."""
    from app import save_order

    TIMEOUT = 10

    # For 2PC, always abort on recovery — locks have TTL as safety net
    # Send abort to stock for all items
    for item_id, quantity in items_quantities.items():
        event_bus.xadd("stock-stream", {
            "cmd": "abort",
            "order_id": order_id,
            "item_id": item_id,
            "quantity": str(quantity),
        })
        response_key = f"response:{order_id}:stock"
        event_bus.blpop(response_key, timeout=TIMEOUT)

    # Send abort to payment
    event_bus.xadd("payment-stream", {
        "cmd": "abort",
        "order_id": order_id,
        "user_id": order.user_id,
        "amount": str(order.total_cost),
    })
    response_key = f"response:{order_id}:payment"
    event_bus.blpop(response_key, timeout=TIMEOUT)

    order.checkout_status = "ABORTED"
    order.checkout_step = "RECOVERED"
    save_order(db, order_id, order)
    logger.info(f"2PC recovery complete for {order_id}: ABORTED")
