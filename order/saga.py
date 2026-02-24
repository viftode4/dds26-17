"""
SAGA Orchestrator for the Order Service (async, WAL-backed).

Every state transition is logged to the WAL stream BEFORE the action
is taken. This ensures crash recovery can deterministically resume
or compensate in-flight sagas.
"""
import json
import logging

import wal

logger = logging.getLogger("order-service")

BLPOP_TIMEOUT = 30
COMPENSATION_RETRIES = 3
COMPENSATION_TIMEOUT = 10


async def checkout(order_id, order_entry, items_quantities, db, stock_db, payment_db):
    """
    Execute a SAGA checkout with WAL-backed durable execution.
    Returns: (success: bool, message: str)
    """
    from app import save_order

    # WAL: log saga start
    await wal.log_step(db, order_id, "started",
                       items=list(items_quantities.items()),
                       user_id=order_entry.user_id,
                       total_cost=order_entry.total_cost)

    # Step 0: Clear old idempotency keys on retry
    await _clear_idempotency_keys(order_id, items_quantities, order_entry, stock_db, payment_db)

    # Step 1: Mark as PENDING
    order_entry.checkout_status = "PENDING"
    order_entry.checkout_step = "STOCK"
    await save_order(db, order_id, order_entry)

    # Step 2: Reserve stock for each item
    reserved_items = []
    for item_id, quantity in items_quantities.items():
        # WAL: log BEFORE sending the Try command
        await wal.log_step(db, order_id, "reserving_item", item_id=item_id, quantity=quantity)

        await stock_db.xadd("stock-commands", {
            "cmd": "reserve", "order_id": order_id,
            "item_id": item_id, "quantity": str(quantity),
        })
        result = await stock_db.blpop(f"response:{order_id}:stock", timeout=BLPOP_TIMEOUT)
        if result is None:
            logger.warning(f"SAGA timeout for stock reserve: {order_id}/{item_id}")
            await wal.log_step(db, order_id, "cancelling_stock", reason="timeout",
                               items_to_cancel=reserved_items)
            await _compensate_stock(order_id, reserved_items, stock_db)
            order_entry.checkout_status = "ABORTED"
            order_entry.checkout_step = "TIMEOUT"
            await save_order(db, order_id, order_entry)
            await wal.log_step(db, order_id, "failed", reason="stock_timeout")
            return False, "Checkout timeout"
        response = json.loads(result[1])
        if response.get("status") != "ok":
            logger.info(f"SAGA stock reserve failed: {order_id}/{item_id}")
            await wal.log_step(db, order_id, "cancelling_stock", reason="reserve_failed",
                               items_to_cancel=reserved_items)
            await _compensate_stock(order_id, reserved_items, stock_db)
            order_entry.checkout_status = "ABORTED"
            order_entry.checkout_step = "STOCK_FAILED"
            await save_order(db, order_id, order_entry)
            await wal.log_step(db, order_id, "failed", reason=f"stock_failed:{item_id}")
            return False, f"Out of stock on item_id: {item_id}"

        # WAL: log AFTER successful reservation
        await wal.log_step(db, order_id, "item_reserved", item_id=item_id)
        reserved_items.append((item_id, quantity))

    # WAL: all stock reserved
    await wal.log_step(db, order_id, "stock_reserved",
                       items=reserved_items)

    # Step 3: Deduct payment
    order_entry.checkout_step = "PAYMENT"
    await save_order(db, order_id, order_entry)
    await wal.log_step(db, order_id, "reserving_payment",
                       user_id=order_entry.user_id,
                       amount=order_entry.total_cost)

    await payment_db.xadd("payment-commands", {
        "cmd": "deduct", "order_id": order_id,
        "user_id": order_entry.user_id, "amount": str(order_entry.total_cost),
    })
    result = await payment_db.blpop(f"response:{order_id}:payment", timeout=BLPOP_TIMEOUT)
    if result is None:
        logger.warning(f"SAGA timeout for payment: {order_id}")
        await wal.log_step(db, order_id, "cancelling_all", reason="payment_timeout")
        await _compensate_stock(order_id, reserved_items, stock_db)
        await _compensate_payment(order_id, order_entry.user_id, order_entry.total_cost, payment_db)
        order_entry.checkout_status = "ABORTED"
        order_entry.checkout_step = "TIMEOUT"
        await save_order(db, order_id, order_entry)
        await wal.log_step(db, order_id, "failed", reason="payment_timeout")
        return False, "Checkout timeout"
    response = json.loads(result[1])
    if response.get("status") != "ok":
        logger.info(f"SAGA payment failed: {order_id}")
        await wal.log_step(db, order_id, "cancelling_stock", reason="payment_failed",
                           items_to_cancel=reserved_items)
        await _compensate_stock(order_id, reserved_items, stock_db)
        order_entry.checkout_status = "ABORTED"
        order_entry.checkout_step = "PAYMENT_FAILED"
        await save_order(db, order_id, order_entry)
        await wal.log_step(db, order_id, "failed", reason="payment_failed")
        return False, "User out of credit"

    # WAL: payment reserved
    await wal.log_step(db, order_id, "payment_reserved")

    # Step 4: Success — confirming
    await wal.log_step(db, order_id, "confirming")
    order_entry.paid = True
    order_entry.checkout_status = "COMMITTED"
    order_entry.checkout_step = "DONE"
    await save_order(db, order_id, order_entry)
    await wal.log_step(db, order_id, "completed")
    return True, "Checkout successful"


async def _compensate_stock(order_id, reserved_items, stock_db):
    """Send compensating transactions for reserved stock with retry."""
    for item_id, quantity in reserved_items:
        for attempt in range(COMPENSATION_RETRIES):
            await stock_db.xadd("stock-commands", {
                "cmd": "compensate", "order_id": order_id,
                "item_id": item_id, "quantity": str(quantity),
            })
            result = await stock_db.blpop(f"response:{order_id}:stock", timeout=COMPENSATION_TIMEOUT)
            if result is not None:
                response = json.loads(result[1])
                if response.get("status") == "ok":
                    break
                logger.warning(f"Stock compensate failed {order_id}/{item_id}: {response}")
            else:
                logger.warning(f"Stock compensate timeout {order_id}/{item_id} (attempt {attempt+1})")
        else:
            logger.error(f"Stock compensate EXHAUSTED retries for {order_id}/{item_id}")


async def _compensate_payment(order_id, user_id, amount, payment_db):
    """Send compensating transaction for payment with retry."""
    for attempt in range(COMPENSATION_RETRIES):
        await payment_db.xadd("payment-commands", {
            "cmd": "compensate", "order_id": order_id,
            "user_id": user_id, "amount": str(amount),
        })
        result = await payment_db.blpop(f"response:{order_id}:payment", timeout=COMPENSATION_TIMEOUT)
        if result is not None:
            response = json.loads(result[1])
            if response.get("status") == "ok":
                return
            logger.warning(f"Payment compensate failed {order_id}: {response}")
        else:
            logger.warning(f"Payment compensate timeout {order_id} (attempt {attempt+1})")
    logger.error(f"Payment compensate EXHAUSTED retries for {order_id}")


async def _clear_idempotency_keys(order_id, items_quantities, order_entry, stock_db, payment_db):
    """Clear old idempotency keys so a retry can proceed."""
    for item_id in items_quantities:
        await stock_db.xadd("stock-commands", {
            "cmd": "clear_keys", "order_id": order_id,
            "item_id": item_id, "quantity": "0",
        })
        await stock_db.blpop(f"response:{order_id}:stock", timeout=BLPOP_TIMEOUT)
    await payment_db.xadd("payment-commands", {
        "cmd": "clear_keys", "order_id": order_id,
        "user_id": order_entry.user_id, "amount": "0",
    })
    await payment_db.blpop(f"response:{order_id}:payment", timeout=BLPOP_TIMEOUT)
