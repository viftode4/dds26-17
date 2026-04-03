import asyncio
import json
import os
import random
import uuid
from contextlib import asynccontextmanager

import httpx
import redis.asyncio as aioredis
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.routing import Route

from common.config import create_redis_connection, create_replica_connection, wait_for_redis, subscribe_failover_invalidation
from common.nats_transport import NatsTransport, NatsOrchestratorTransport
from common.logging import setup_logging, get_logger
from common.result import wait_for_result
from orchestrator import (
    Orchestrator,
    TransactionDefinition,
    Step,
    LeaderElection,
    RecoveryWorker,
)


log = get_logger("checkout")

DB_ERROR_STR = "DB error"

FINALIZE_HASH = "checkout:pending_finalize"

ORDER_BASES = [
    b.strip()
    for b in os.environ.get(
        "ORDER_INTERNAL_BASES",
        "http://order-service-1:5000,http://order-service-2:5000",
    ).split(",")
    if b.strip()
]

db: aioredis.Redis | None = None
db_read: aioredis.Redis | None = None
orchestrator: Orchestrator | None = None
leader_election: LeaderElection | None = None
recovery_worker: RecoveryWorker | None = None
_leader_task: asyncio.Task | None = None
_order_client: httpx.AsyncClient | None = None
_nats_transport: NatsTransport | None = None


def stock_payload(saga_id: str, action: str, context: dict) -> dict:
    items = context.get("items", [])
    cmd: dict[str, str] = {}
    if items:
        cmd["items"] = json.dumps(items)
    cmd["ttl"] = str(context.get("_reservation_ttl", 60))
    return cmd


def payment_payload(saga_id: str, action: str, context: dict) -> dict:
    return {
        "user_id": context.get("user_id", ""),
        "amount": str(context.get("total_cost", 0)),
        "ttl": str(context.get("_reservation_ttl", 60)),
    }


checkout_tx = TransactionDefinition(
    name="checkout",
    steps=[
        Step(
            name="reserve_stock",
            service="stock",
            payload_builder=stock_payload,
        ),
        Step(
            name="reserve_payment",
            service="payment",
            payload_builder=payment_payload,
        ),
    ],
)


async def _order_post(path: str, body: dict) -> httpx.Response:
    base = random.choice(ORDER_BASES)
    return await _order_client.post(
        f"{base}{path}",
        json=body,
        timeout=httpx.Timeout(30.0, connect=5.0),
    )


async def _reconcile_finalize():
    """Best-effort: complete order rows after saga success if HTTP finalize was lost."""
    if not db:
        return
    try:
        pending = await db.hgetall(FINALIZE_HASH)
    except aioredis.RedisError:
        return
    for saga_id, order_id in pending.items():
        try:
            r = await _order_post(
                f"/internal/checkout/complete/{order_id}",
                {"saga_id": saga_id},
            )
            if r.status_code == 200:
                await db.hdel(FINALIZE_HASH, saga_id)
        except Exception as e:
            log.debug("reconcile_finalize attempt failed", saga_id=saga_id, error=str(e))


async def _leadership_loop():
    while True:
        try:
            acquired = await leader_election.acquire()
            if acquired:
                log.info("Checkout coordinator is LEADER (recovery / reconciliation)")
                await recovery_worker.recover_incomplete_sagas()
                await recovery_worker.start_reconciliation()
                while leader_election.is_leader:
                    await asyncio.sleep(1)
                log.warning("Lost leadership, stopping maintenance workers")
                await recovery_worker.stop()
            else:
                await asyncio.sleep(2)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.error("Leadership loop error", error=str(e))
            await asyncio.sleep(2)


@asynccontextmanager
async def lifespan(app):
    global db, db_read, orchestrator, leader_election, recovery_worker
    global _leader_task, _order_client, _nats_transport

    setup_logging("checkout-service")

    db = create_redis_connection(prefix="", decode_responses=True)
    await wait_for_redis(db, "checkout-db")

    db_read = create_replica_connection(prefix="", decode_responses=True)

    await asyncio.gather(*[db.ping() for _ in range(16)])

    _order_client = httpx.AsyncClient()

    _nats_transport = NatsTransport(os.environ.get("NATS_URL", "nats://nats:4222"))
    await _nats_transport.connect()
    orch_transport = NatsOrchestratorTransport(_nats_transport)

    orchestrator = Orchestrator(
        order_db=db,
        transport=orch_transport,
        definitions=[checkout_tx],
        protocol="auto",
    )
    await orchestrator.start()

    leader_election = LeaderElection(db)
    recovery_worker = RecoveryWorker(
        wal=orchestrator.wal,
        transport=orch_transport,
        definitions=orchestrator.definitions,
        reconcile_fn=_reconcile_finalize,
    )

    _leader_task = asyncio.create_task(_leadership_loop())

    failover_task = await subscribe_failover_invalidation(
        db, db_read, service_name="checkout")
    log.info("Checkout coordinator started")

    yield

    if failover_task:
        failover_task.cancel()
    if _leader_task:
        _leader_task.cancel()
        try:
            await _leader_task
        except asyncio.CancelledError:
            pass
    if leader_election:
        await leader_election.stop()
    if recovery_worker:
        await recovery_worker.stop()
    if orchestrator:
        await orchestrator.stop()
    if _nats_transport:
        await _nats_transport.close()
    if _order_client:
        await _order_client.aclose()
    if db_read:
        await db_read.aclose()
    if db:
        await db.aclose()


async def _finalize_order(order_id: str, saga_id: str) -> bool:
    """POST complete to order-service with retries; clears finalize hash on success."""
    deadline = asyncio.get_event_loop().time() + 35.0
    backoff = 0.1
    while asyncio.get_event_loop().time() < deadline:
        try:
            r = await _order_post(
                f"/internal/checkout/complete/{order_id}",
                {"saga_id": saga_id},
            )
            if r.status_code == 200:
                await db.hdel(FINALIZE_HASH, saga_id)
                return True
        except Exception as e:
            log.warning("complete_checkout HTTP error", error=str(e))
        await asyncio.sleep(min(backoff, 2.0))
        backoff *= 1.5
    return False


async def checkout(request: Request):
    order_id = request.path_params["order_id"]
    saga_id = str(uuid.uuid4())

    try:
        r = await _order_post(
            f"/internal/checkout/begin/{order_id}",
            {"saga_id": saga_id},
        )
    except httpx.HTTPError as e:
        log.warning("begin_checkout failed", error=str(e))
        raise HTTPException(400, detail=DB_ERROR_STR)

    if r.status_code == 404:
        raise HTTPException(400, detail=f"Order: {order_id} not found!")
    if r.status_code == 409:
        raise HTTPException(400, detail="Checkout already in progress")
    if r.status_code != 200:
        try:
            err = r.json().get("error", r.text)
        except Exception:
            err = r.text
        raise HTTPException(400, detail=str(err))

    data = r.json()
    if data.get("already_paid"):
        return PlainTextResponse("Checkout successful")
    dup = data.get("duplicate")
    if dup == "success":
        return PlainTextResponse("Checkout successful")
    if dup == "failed":
        raise HTTPException(400, detail=f"Checkout failed: {data.get('error', 'unknown')}")
    if dup == "processing":
        other = data.get("saga_id", "")
        if other:
            result = await wait_for_result(db, other, timeout=30.0)
            if result.get("status") == "success":
                # Saga may be terminal on checkout-db while order finalize HTTP was lost.
                await _finalize_order(order_id, other)
                return PlainTextResponse("Checkout successful")
            raise HTTPException(400, detail=f"Checkout failed: {result.get('error', 'unknown')}")
        raise HTTPException(400, detail="Checkout already in progress")

    if not data.get("acquired"):
        raise HTTPException(400, detail="Checkout conflict")

    context = {
        "order_id": order_id,
        "user_id": data["user_id"],
        "items": data["items"],
        "total_cost": data["total_cost"],
    }
    result = await orchestrator.execute("checkout", context, saga_id_override=saga_id)

    if result.get("status") == "success":
        await db.hset(FINALIZE_HASH, saga_id, order_id)
        ok = await _finalize_order(order_id, saga_id)
        if not ok:
            log.error("Order finalize did not confirm in time; left for reconciliation",
                      order_id=order_id, saga_id=saga_id)
        log.info("Checkout successful", order_id=order_id, saga_id=saga_id,
                 protocol=result.get("protocol"))
        return PlainTextResponse("Checkout successful")

    try:
        await _order_post(
            f"/internal/checkout/release/{order_id}",
            {"saga_id": saga_id},
        )
    except httpx.HTTPError:
        pass
    error = result.get("error", "unknown")
    log.info("Checkout failed", order_id=order_id, saga_id=saga_id, error=error)
    raise HTTPException(400, detail=f"Checkout failed: {error}")


async def health(request: Request):
    try:
        await db.ping()
    except Exception:
        raise HTTPException(503, detail="Redis unavailable")
    return JSONResponse({"status": "healthy"})


async def coordinator_health_alias(request: Request):
    return await health(request)


async def metrics(request: Request):
    m = orchestrator.metrics
    abort_rate = m.sliding_abort_rate()
    is_leader = 1 if leader_election and leader_election.is_leader else 0

    lines = [
        "# HELP saga_total Total number of completed sagas",
        "# TYPE saga_total counter",
        f'saga_total{{result="success"}} {m.total_success}',
        f'saga_total{{result="failure"}} {m.total_failure}',
        "",
        "# HELP saga_abort_rate Current sliding window abort rate (100-saga window)",
        "# TYPE saga_abort_rate gauge",
        f"saga_abort_rate {abort_rate:.4f}",
        "",
        "# HELP saga_current_protocol Currently active transaction protocol",
        "# TYPE saga_current_protocol gauge",
        f'saga_current_protocol{{protocol="{m.current_protocol}"}} 1',
        "",
        "# HELP leader_status Whether this instance runs background maintenance",
        "# TYPE leader_status gauge",
        f"leader_status {is_leader}",
        "",
        "# HELP checkout_duration_seconds Checkout saga latency histogram",
        "# TYPE checkout_duration_seconds histogram",
    ]
    lines.extend(m.latency_2pc.prometheus_lines(
        "checkout_duration_seconds", 'protocol="2pc"'
    ))
    lines.extend(m.latency_saga.prometheus_lines(
        "checkout_duration_seconds", 'protocol="saga"'
    ))

    return Response("\n".join(lines) + "\n", status_code=200, media_type="text/plain")


async def http_exception_handler(request, exc):
    return JSONResponse({"error": exc.detail}, status_code=exc.status_code)


routes = [
    Route("/checkout/{order_id}", checkout, methods=["POST"]),
    Route("/__checkout_health", coordinator_health_alias, methods=["GET"]),
    Route("/health", health, methods=["GET"]),
    Route("/metrics", metrics, methods=["GET"]),
]

app = Starlette(
    routes=routes,
    lifespan=lifespan,
    exception_handlers={HTTPException: http_exception_handler},
)
