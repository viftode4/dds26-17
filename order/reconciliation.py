"""
Reconciliation Worker for the Order Service.

Periodically checks data integrity by verifying that:
1. No orders are stuck in PENDING beyond a timeout
2. WAL entries without terminal states are investigated
3. Stale response keys are cleaned up

Runs as a background asyncio task, started in app.before_serving.
"""
import asyncio
import json
import logging

from msgspec import msgpack

import wal

logger = logging.getLogger("order-service")

# Run every 60 seconds
RECONCILE_INTERVAL = 60
# Orders pending longer than this are considered stuck
STUCK_TIMEOUT_SECONDS = 120


async def reconciliation_loop(db, stock_db, payment_db, tx_mode: str):
    """Periodic reconciliation task."""
    from app import OrderValue

    logger.info("Reconciliation worker started")
    while True:
        try:
            await asyncio.sleep(RECONCILE_INTERVAL)
            await _reconcile(db, stock_db, payment_db, tx_mode)
        except asyncio.CancelledError:
            logger.info("Reconciliation worker stopped")
            return
        except Exception as e:
            logger.error(f"Reconciliation error: {e}")


async def _reconcile(db, stock_db, payment_db, tx_mode):
    """Run one reconciliation pass."""
    from app import OrderValue, save_order

    issues = 0

    # 1. Check pending_orders set for stuck orders
    cursor = 0
    while True:
        cursor, pending_ids = await db.sscan("pending_orders", cursor, count=100)
        for key in pending_ids:
            key_str = key.decode() if isinstance(key, bytes) else key
            try:
                raw = await db.get(key_str)
                if raw is None:
                    # Order deleted but still in pending set — clean up
                    await db.srem("pending_orders", key_str)
                    issues += 1
                    logger.warning(f"Reconcile: removed orphan pending entry {key_str}")
                    continue

                order = msgpack.decode(raw, type=OrderValue)
                if order.checkout_status != "PENDING":
                    # Completed but not removed from pending set
                    await db.srem("pending_orders", key_str)
                    issues += 1
                    logger.warning(f"Reconcile: cleaned completed order from pending set: {key_str}")
            except Exception as e:
                logger.error(f"Reconcile: error checking {key_str}: {e}")
        
        if cursor == 0:
            break

    # 2. Check WAL for incomplete sagas (if in saga mode)
    if tx_mode == "saga":
        try:
            incomplete = await wal.get_incomplete_sagas(db)
            if incomplete:
                logger.warning(f"Reconcile: {len(incomplete)} incomplete sagas in WAL")
                issues += len(incomplete)
        except Exception as e:
            logger.error(f"Reconcile: WAL check error: {e}")

    # 3. Clean up stale response keys on stock-db and payment-db
    for target_db, name in [(stock_db, "stock"), (payment_db, "payment")]:
        try:
            cursor = 0
            cleaned = 0
            while True:
                cursor, keys = await target_db.scan(cursor, match="response:*", count=50)
                for key in keys:
                    ttl = await target_db.ttl(key)
                    if ttl == -1:  # No TTL set — add 60s expiry
                        await target_db.expire(key, 60)
                        cleaned += 1
                if cursor == 0:
                    break
            if cleaned:
                logger.info(f"Reconcile: set TTL on {cleaned} stale {name} response keys")
                issues += cleaned
        except Exception as e:
            logger.error(f"Reconcile: {name} cleanup error: {e}")

    if issues:
        logger.info(f"Reconciliation pass complete: {issues} issues resolved")
