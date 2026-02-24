"""
Two-Phase Commit Coordinator for the Order Service (async version).

Uses per-service Redis for commands + responses (no shared event-bus).
"""
import json
import logging

logger = logging.getLogger("order-service")

BLPOP_TIMEOUT = 30


async def checkout(order_id, order_entry, items_quantities, db, stock_db, payment_db):
    """
    Execute a 2PC checkout (async).

    Returns: (success: bool, message: str)
    """
    from app import save_order

    # Step 1: PREPARE phase
    order_entry.checkout_status = "PENDING"
    order_entry.checkout_step = "PREPARE"
    await save_order(db, order_id, order_entry)

    prepared_stock_items = []
    all_prepared = True
    failure_reason = ""

    # Prepare stock for each item
    for item_id, quantity in items_quantities.items():
        await stock_db.xadd("stock-commands", {
            "cmd": "prepare", "order_id": order_id,
            "item_id": item_id, "quantity": str(quantity),
        })
        result = await stock_db.blpop(f"response:{order_id}:stock", timeout=BLPOP_TIMEOUT)
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

    # Prepare payment
    payment_prepared = False
    if all_prepared:
        await payment_db.xadd("payment-commands", {
            "cmd": "prepare", "order_id": order_id,
            "user_id": order_entry.user_id, "amount": str(order_entry.total_cost),
        })
        result = await payment_db.blpop(f"response:{order_id}:payment", timeout=BLPOP_TIMEOUT)
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

    # Step 2: Decision
    if all_prepared:
        order_entry.checkout_step = "COMMIT"
        await save_order(db, order_id, order_entry)
        return await _commit(order_id, order_entry, prepared_stock_items, db, stock_db, payment_db)
    else:
        order_entry.checkout_step = "ABORT"
        await save_order(db, order_id, order_entry)
        return await _abort(order_id, order_entry, prepared_stock_items, payment_prepared,
                            failure_reason, db, stock_db, payment_db)


async def _commit(order_id, order_entry, stock_items, db, stock_db, payment_db):
    """Send commit to all participants."""
    from app import save_order

    for item_id, quantity in stock_items:
        await stock_db.xadd("stock-commands", {
            "cmd": "commit", "order_id": order_id,
            "item_id": item_id, "quantity": "0",
        })
        await stock_db.blpop(f"response:{order_id}:stock", timeout=BLPOP_TIMEOUT)

    await payment_db.xadd("payment-commands", {
        "cmd": "commit", "order_id": order_id,
        "user_id": order_entry.user_id, "amount": "0",
    })
    await payment_db.blpop(f"response:{order_id}:payment", timeout=BLPOP_TIMEOUT)

    order_entry.paid = True
    order_entry.checkout_status = "COMMITTED"
    order_entry.checkout_step = "DONE"
    await save_order(db, order_id, order_entry)
    return True, "Checkout successful"


async def _abort(order_id, order_entry, stock_items, payment_prepared, reason, db, stock_db, payment_db):
    """Send abort to all prepared participants."""
    from app import save_order

    for item_id, quantity in stock_items:
        await stock_db.xadd("stock-commands", {
            "cmd": "abort", "order_id": order_id,
            "item_id": item_id, "quantity": str(quantity),
        })
        await stock_db.blpop(f"response:{order_id}:stock", timeout=BLPOP_TIMEOUT)

    if payment_prepared:
        await payment_db.xadd("payment-commands", {
            "cmd": "abort", "order_id": order_id,
            "user_id": order_entry.user_id, "amount": str(order_entry.total_cost),
        })
        await payment_db.blpop(f"response:{order_id}:payment", timeout=BLPOP_TIMEOUT)

    order_entry.checkout_status = "ABORTED"
    order_entry.checkout_step = "DONE"
    await save_order(db, order_id, order_entry)
    return False, reason
