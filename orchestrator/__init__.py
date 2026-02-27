# Orchestrator framework for hybrid 2PC/Saga transaction coordination
#
# Public API — importable as: from orchestrator import Orchestrator, ...
# Package name for pip install: wdm-orchestrator
#
# Usage:
#   from orchestrator import Orchestrator, TransactionDefinition, Step, checkout_tx
#   orch = Orchestrator(order_db, service_dbs, [checkout_tx], protocol="auto")
#   await orch.start()
#   result = await orch.execute("checkout", context)

from orchestrator.core import Orchestrator
from orchestrator.definition import TransactionDefinition, Step, checkout_tx
from orchestrator.executor import TwoPCExecutor, SagaExecutor, OutboxReader, CircuitBreaker
from orchestrator.wal import WALEngine
from orchestrator.recovery import RecoveryWorker
from orchestrator.leader import LeaderElection
from orchestrator.metrics import MetricsCollector, LatencyHistogram

__all__ = [
    "Orchestrator",
    "TransactionDefinition",
    "Step",
    "checkout_tx",
    "TwoPCExecutor",
    "SagaExecutor",
    "OutboxReader",
    "CircuitBreaker",
    "WALEngine",
    "RecoveryWorker",
    "LeaderElection",
    "MetricsCollector",
    "LatencyHistogram",
]
