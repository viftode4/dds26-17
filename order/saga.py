"""
SAGA Orchestrator for the Order Service.

Implements synchronous orchestrated SAGA pattern:
1. Reserve stock for each item (via stock-stream)
2. Deduct payment (via payment-stream)
3. On failure: compensating transactions for completed steps
"""
import json
import logging

logger = logging.getLogger("order-service")

BLPOP_TIMEOUT = 30  # seconds


def checkout(order_id: str, order_entry, items_quantities: dict, db, event_bus):
    """
    Execute a SAGA checkout.

    Args:
        order_id: The order UUID
        order_entry: OrderValue instance
        items_quantities: dict of {item_id: quantity}
        db: Order-db Redis connection
        event_bus: Event-bus Redis connection

    Returns:
        (success: bool, message: str)
    """
    from app import OrderValue, save_order

    # Step 0: If retrying, clear old idempotency keys in stock/payment
    _clear_idempotency_keys(order_id, items_quantities, order_entry, event_bus)

    # Step 1: Update order status to PENDING / STOCK phase
    order_entry.checkout_status = "PENDING"
    order_entry.checkout_step = "STOCK"
    save_order(db, order_id, order_entry)

    # Step 2: Reserve stock for each item
    reserved_items = []  # Track what we've reserved for compensation

    for item_id, quantity in items_quantities.items():
        # Publish reserve command
        event_bus.xadd("stock-stream", {
            "cmd": "reserve",
            "order_id": order_id,
            "item_id": item_id,
            "quantity": str(quantity),
        })

        # Wait for response
        response_key = f"response:{order_id}:stock"
        result = event_bus.blpop(response_key, timeout=BLPOP_TIMEOUT)

        if result is None:
            # Timeout — compensate what we've done so far
            logger.warning(f"SAGA timeout waiting for stock reserve: {order_id}/{item_id}")
            _compensate_stock(order_id, reserved_items, event_bus)
            order_entry.checkout_status = "ABORTED"
            order_entry.checkout_step = "TIMEOUT"
            save_order(db, order_id, order_entry)
            return False, "Checkout timeout"

        response = json.loads(result[1])
        if response.get("status") != "ok":
            # Stock reservation failed — compensate
            logger.info(f"SAGA stock reserve failed: {order_id}/{item_id}")
            _compensate_stock(order_id, reserved_items, event_bus)
            order_entry.checkout_status = "ABORTED"
            order_entry.checkout_step = "STOCK_FAILED"
            save_order(db, order_id, order_entry)
            return False, f"Out of stock on item_id: {item_id}"

        reserved_items.append((item_id, quantity))

    # Step 3: Deduct payment
    order_entry.checkout_step = "PAYMENT"
    save_order(db, order_id, order_entry)

    event_bus.xadd("payment-stream", {
        "cmd": "deduct",
        "order_id": order_id,
        "user_id": order_entry.user_id,
        "amount": str(order_entry.total_cost),
    })

    response_key = f"response:{order_id}:payment"
    result = event_bus.blpop(response_key, timeout=BLPOP_TIMEOUT)

    if result is None:
        logger.warning(f"SAGA timeout waiting for payment: {order_id}")
        _compensate_stock(order_id, reserved_items, event_bus)
        _compensate_payment(order_id, order_entry.user_id, order_entry.total_cost, event_bus)
        order_entry.checkout_status = "ABORTED"
        order_entry.checkout_step = "TIMEOUT"
        save_order(db, order_id, order_entry)
        return False, "Checkout timeout"

    response = json.loads(result[1])
    if response.get("status") != "ok":
        logger.info(f"SAGA payment failed: {order_id}")
        _compensate_stock(order_id, reserved_items, event_bus)
        order_entry.checkout_status = "ABORTED"
        order_entry.checkout_step = "PAYMENT_FAILED"
        save_order(db, order_id, order_entry)
        return False, "User out of credit"

    # Step 4: Success
    order_entry.paid = True
    order_entry.checkout_status = "COMMITTED"
    order_entry.checkout_step = "DONE"
    save_order(db, order_id, order_entry)
    return True, "Checkout successful"


def _compensate_stock(order_id: str, reserved_items: list, event_bus):
    """Send compensating transactions for reserved stock."""
    for item_id, quantity in reserved_items:
        event_bus.xadd("stock-stream", {
            "cmd": "compensate",
            "order_id": order_id,
            "item_id": item_id,
            "quantity": str(quantity),
        })
        # Wait for compensation confirmation
        response_key = f"response:{order_id}:stock"
        event_bus.blpop(response_key, timeout=BLPOP_TIMEOUT)


def _compensate_payment(order_id: str, user_id: str, amount: int, event_bus):
    """Send compensating transaction for payment."""
    event_bus.xadd("payment-stream", {
        "cmd": "compensate",
        "order_id": order_id,
        "user_id": user_id,
        "amount": str(amount),
    })
    response_key = f"response:{order_id}:payment"
    event_bus.blpop(response_key, timeout=BLPOP_TIMEOUT)


def _clear_idempotency_keys(order_id: str, items_quantities: dict, order_entry, event_bus):
    """Clear old idempotency keys in stock/payment so a retry can proceed."""
    # Clear stock idempotency keys
    for item_id, quantity in items_quantities.items():
        event_bus.xadd("stock-stream", {
            "cmd": "clear_keys",
            "order_id": order_id,
            "item_id": item_id,
            "quantity": "0",
        })
        response_key = f"response:{order_id}:stock"
        event_bus.blpop(response_key, timeout=BLPOP_TIMEOUT)

    # Clear payment idempotency keys
    event_bus.xadd("payment-stream", {
        "cmd": "clear_keys",
        "order_id": order_id,
        "user_id": order_entry.user_id,
        "amount": "0",
    })
    response_key = f"response:{order_id}:payment"
    event_bus.blpop(response_key, timeout=BLPOP_TIMEOUT)

