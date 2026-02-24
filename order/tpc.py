"""
Two-Phase Commit Coordinator for the Order Service.

Implements 2PC with the Order service as coordinator:
1. Prepare: Send prepare to stock + payment (lock resources with TTL)
2. Decision: All prepared → commit; any fail → abort
3. Commit/Abort: Finalize or rollback all participants
"""
import json
import logging

logger = logging.getLogger("order-service")

BLPOP_TIMEOUT = 30  # seconds


def checkout(order_id: str, order_entry, items_quantities: dict, db, event_bus):
    """
    Execute a 2PC checkout.

    Args:
        order_id: The order UUID
        order_entry: OrderValue instance
        items_quantities: dict of {item_id: quantity}
        db: Order-db Redis connection
        event_bus: Event-bus Redis connection

    Returns:
        (success: bool, message: str)
    """
    from app import save_order

    # Step 1: Update order status to PENDING / PREPARE phase
    order_entry.checkout_status = "PENDING"
    order_entry.checkout_step = "PREPARE"
    save_order(db, order_id, order_entry)

    # Step 2: Prepare phase — send prepare to all participants
    prepared_stock_items = []
    all_prepared = True
    failure_reason = ""

    # Prepare stock for each item
    for item_id, quantity in items_quantities.items():
        event_bus.xadd("stock-stream", {
            "cmd": "prepare",
            "order_id": order_id,
            "item_id": item_id,
            "quantity": str(quantity),
        })

        response_key = f"response:{order_id}:stock"
        result = event_bus.blpop(response_key, timeout=BLPOP_TIMEOUT)

        if result is None:
            all_prepared = False
            failure_reason = "Timeout waiting for stock prepare"
            break

        response = json.loads(result[1])
        if response.get("status") != "prepared":
            all_prepared = False
            failure_reason = f"Stock prepare failed for {item_id}: {response.get('reason', 'unknown')}"
            break

        prepared_stock_items.append((item_id, quantity))

    # Prepare payment (only if all stock prepared)
    payment_prepared = False
    if all_prepared:
        event_bus.xadd("payment-stream", {
            "cmd": "prepare",
            "order_id": order_id,
            "user_id": order_entry.user_id,
            "amount": str(order_entry.total_cost),
        })

        response_key = f"response:{order_id}:payment"
        result = event_bus.blpop(response_key, timeout=BLPOP_TIMEOUT)

        if result is None:
            all_prepared = False
            failure_reason = "Timeout waiting for payment prepare"
        else:
            response = json.loads(result[1])
            if response.get("status") != "prepared":
                all_prepared = False
                failure_reason = f"Payment prepare failed: {response.get('reason', 'unknown')}"
            else:
                payment_prepared = True

    # Step 3: Decision phase
    if all_prepared:
        # COMMIT
        order_entry.checkout_step = "COMMIT"
        save_order(db, order_id, order_entry)
        return _commit(order_id, order_entry, prepared_stock_items, db, event_bus)
    else:
        # ABORT
        order_entry.checkout_step = "ABORT"
        save_order(db, order_id, order_entry)
        return _abort(order_id, order_entry, prepared_stock_items, payment_prepared, failure_reason, db, event_bus)


def _commit(order_id, order_entry, stock_items, db, event_bus):
    """Send commit to all participants."""
    from app import save_order

    # Commit stock
    for item_id, quantity in stock_items:
        event_bus.xadd("stock-stream", {
            "cmd": "commit",
            "order_id": order_id,
            "item_id": item_id,
            "quantity": "0",  # Not used for commit
        })
        response_key = f"response:{order_id}:stock"
        event_bus.blpop(response_key, timeout=BLPOP_TIMEOUT)

    # Commit payment
    event_bus.xadd("payment-stream", {
        "cmd": "commit",
        "order_id": order_id,
        "user_id": order_entry.user_id,
        "amount": "0",  # Not used for commit
    })
    response_key = f"response:{order_id}:payment"
    event_bus.blpop(response_key, timeout=BLPOP_TIMEOUT)

    # Mark order as paid
    order_entry.paid = True
    order_entry.checkout_status = "COMMITTED"
    order_entry.checkout_step = "DONE"
    save_order(db, order_id, order_entry)
    return True, "Checkout successful"


def _abort(order_id, order_entry, stock_items, payment_prepared, reason, db, event_bus):
    """Send abort to all prepared participants."""
    from app import save_order

    # Abort prepared stock items
    for item_id, quantity in stock_items:
        event_bus.xadd("stock-stream", {
            "cmd": "abort",
            "order_id": order_id,
            "item_id": item_id,
            "quantity": str(quantity),
        })
        response_key = f"response:{order_id}:stock"
        event_bus.blpop(response_key, timeout=BLPOP_TIMEOUT)

    # Abort payment if it was prepared
    if payment_prepared:
        event_bus.xadd("payment-stream", {
            "cmd": "abort",
            "order_id": order_id,
            "user_id": order_entry.user_id,
            "amount": str(order_entry.total_cost),
        })
        response_key = f"response:{order_id}:payment"
        event_bus.blpop(response_key, timeout=BLPOP_TIMEOUT)

    order_entry.checkout_status = "ABORTED"
    order_entry.checkout_step = "DONE"
    save_order(db, order_id, order_entry)
    return False, reason
