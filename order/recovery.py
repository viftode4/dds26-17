"""
Crash Recovery for the Order Service (WAL-backed).

Recovery strategy:
1. Read incomplete sagas from the WAL stream
2. For each, determine the last known step
3. Compensate as needed based on what was reserved
4. Also falls back to pending_orders set for non-WAL sagas (2PC)
"""
import json
import logging
from collections import defaultdict

from msgspec import msgpack

import wal

logger = logging.getLogger("order-service")

TIMEOUT = 10


async def run_recovery(db, stock_db, payment_db, tx_mode: str):
    """Scan for incomplete transactions and attempt recovery."""
    from app import OrderValue

    logger.info(f"Running startup recovery (mode: {tx_mode})...")
    recovered = 0

    # WAL-based recovery for sagas
    if tx_mode == "saga":
        try:
            incomplete = await wal.get_incomplete_sagas(db)
            for entry in incomplete:
                saga_id = entry.get('saga_id', '')
                step = entry.get('step', '')
                logger.warning(f"WAL recovery: saga {saga_id} at step '{step}'")
                await _recover_from_wal(saga_id, entry, db, stock_db, payment_db)
                recovered += 1
        except Exception as e:
            logger.error(f"WAL recovery error: {e}")

    # Fallback: pending_orders set (covers 2PC and pre-WAL sagas)
    try:
        cursor = 0
        while True:
            cursor, pending_ids = await db.sscan("pending_orders", cursor, count=100)
            for key in pending_ids:
                key_str = key.decode() if isinstance(key, bytes) else key
                try:
                    raw = await db.get(key_str)
                    if raw is None:
                        await db.srem("pending_orders", key_str)
                        continue
                    order = msgpack.decode(raw, type=OrderValue)
                except Exception:
                    continue

                if order.checkout_status not in ("PENDING",):
                    await db.srem("pending_orders", key_str)
                    continue

                logger.warning(f"Pending-set recovery: {key_str} (step: {order.checkout_step})")
                items_quantities = defaultdict(int)
                for item_id, quantity in order.items:
                    items_quantities[item_id] += quantity

                if tx_mode == "saga":
                    await _recover_saga_fallback(key_str, order, items_quantities, db, stock_db, payment_db)
                else:
                    await _recover_2pc(key_str, order, items_quantities, db, stock_db, payment_db)
                await db.srem("pending_orders", key_str)
                recovered += 1
            if cursor == 0:
                break
    except Exception as e:
        logger.error(f"Pending-set recovery error: {e}")

    logger.info(f"Recovery complete: {recovered} transactions recovered")


async def _recover_from_wal(saga_id, wal_entry, db, stock_db, payment_db):
    """Recover a saga using WAL state — cancel any reservations made."""
    from app import OrderValue, save_order

    step = wal_entry.get('step', '')

    # Determine what was reserved based on WAL step
    items_raw = wal_entry.get('items', '[]')
    try:
        items = json.loads(items_raw) if items_raw != '[]' else []
    except (json.JSONDecodeError, TypeError):
        items = []

    # If we were in the middle of reserving, or past it — cancel all reserved items
    cancel_steps = {"reserving_item", "item_reserved", "stock_reserved",
                    "reserving_payment", "payment_reserved", "confirming",
                    "cancelling_stock", "cancelling_all"}

    if step in cancel_steps:
        for item in items:
            if isinstance(item, (list, tuple)) and len(item) == 2:
                item_id, quantity = str(item[0]), int(item[1])
            else:
                continue
            await stock_db.xadd("stock-commands", {
                "cmd": "compensate", "order_id": saga_id,
                "item_id": item_id, "quantity": str(quantity),
            })
            await stock_db.blpop(f"response:{saga_id}:stock", timeout=TIMEOUT)

        # Cancel payment if we got that far
        if step in {"reserving_payment", "payment_reserved", "confirming", "cancelling_all"}:
            user_id = wal_entry.get('user_id', '')
            amount = wal_entry.get('total_cost', '0')
            if user_id:
                await payment_db.xadd("payment-commands", {
                    "cmd": "compensate", "order_id": saga_id,
                    "user_id": user_id, "amount": str(amount),
                })
                await payment_db.blpop(f"response:{saga_id}:payment", timeout=TIMEOUT)

    # Update order state
    try:
        raw = await db.get(saga_id)
        if raw:
            order = msgpack.decode(raw, type=OrderValue)
            order.checkout_status = "ABORTED"
            order.checkout_step = "RECOVERED"
            await save_order(db, saga_id, order)
    except Exception as e:
        logger.error(f"Failed to update order {saga_id}: {e}")

    # Mark WAL entry as recovered
    await wal.log_step(db, saga_id, "recovered")
    await db.srem("pending_orders", saga_id)
    logger.info(f"WAL recovery complete for {saga_id}: ABORTED")


async def _recover_saga_fallback(order_id, order, items_quantities, db, stock_db, payment_db):
    """Fallback SAGA recovery (no WAL available)."""
    from app import save_order

    if order.checkout_step in ("STOCK", "PAYMENT", "STOCK_FAILED", "PAYMENT_FAILED"):
        for item_id, quantity in items_quantities.items():
            await stock_db.xadd("stock-commands", {
                "cmd": "compensate", "order_id": order_id,
                "item_id": item_id, "quantity": str(quantity),
            })
            await stock_db.blpop(f"response:{order_id}:stock", timeout=TIMEOUT)
        if order.checkout_step in ("PAYMENT", "PAYMENT_FAILED"):
            await payment_db.xadd("payment-commands", {
                "cmd": "compensate", "order_id": order_id,
                "user_id": order.user_id, "amount": str(order.total_cost),
            })
            await payment_db.blpop(f"response:{order_id}:payment", timeout=TIMEOUT)

    order.checkout_status = "ABORTED"
    order.checkout_step = "RECOVERED"
    await save_order(db, order_id, order)
    logger.info(f"SAGA fallback recovery for {order_id}: ABORTED")


async def _recover_2pc(order_id, order, items_quantities, db, stock_db, payment_db):
    """
    Recover 2PC by enforcing the coordinator's last durable decision.

    If the coordinator crashed AFTER writing checkout_step=COMMIT, we must
    re-send commits to all participants (commit is idempotent).
    Otherwise we abort all participants in order to be safe.
    """
    from app import save_order

    if order.checkout_step == "COMMIT":
        # Coordinator had already decided COMMIT — enforce it everywhere.
        logger.warning(f"2PC recovery: enforcing COMMIT for {order_id}")
        for item_id, quantity in items_quantities.items():
            await stock_db.xadd("stock-commands", {
                "cmd": "commit", "order_id": order_id,
                "item_id": item_id, "quantity": str(quantity),
            })
            await stock_db.blpop(f"response:{order_id}:stock", timeout=TIMEOUT)

        await payment_db.xadd("payment-commands", {
            "cmd": "commit", "order_id": order_id,
            "user_id": order.user_id, "amount": str(order.total_cost),
        })
        await payment_db.blpop(f"response:{order_id}:payment", timeout=TIMEOUT)

        order.checkout_status = "COMMITTED"
        order.checkout_step = "DONE"
        order.paid = True
        await save_order(db, order_id, order)
        logger.info(f"2PC recovery for {order_id}: COMMITTED")
    else:
        # Coordinator had not yet decided, or had decided ABORT — enforce abort.
        logger.warning(f"2PC recovery: enforcing ABORT for {order_id} (step={order.checkout_step})")
        for item_id, quantity in items_quantities.items():
            await stock_db.xadd("stock-commands", {
                "cmd": "abort", "order_id": order_id,
                "item_id": item_id, "quantity": str(quantity),
            })
            await stock_db.blpop(f"response:{order_id}:stock", timeout=TIMEOUT)

        await payment_db.xadd("payment-commands", {
            "cmd": "abort", "order_id": order_id,
            "user_id": order.user_id, "amount": str(order.total_cost),
        })
        await payment_db.blpop(f"response:{order_id}:payment", timeout=TIMEOUT)

        order.checkout_status = "ABORTED"
        order.checkout_step = "RECOVERED"
        await save_order(db, order_id, order)
        logger.info(f"2PC recovery for {order_id}: ABORTED")
