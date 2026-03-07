# Orchestrator framework for hybrid 2PC/Saga transaction coordination
#
# Public API — importable as: from orchestrator import Orchestrator, ...
# Package name for pip install: wdm-orchestrator
#
# Usage:
#   from orchestrator import Orchestrator, TransactionDefinition, Step
#   orch = Orchestrator(order_db, transport, [my_tx], protocol="auto")
#   result = await orch.execute("my_tx", context)

from orchestrator.core import Orchestrator
from orchestrator.definition import TransactionDefinition, Step
from orchestrator.executor import TwoPCExecutor, SagaExecutor, CircuitBreaker
from orchestrator.wal import WALEngine
from orchestrator.recovery import RecoveryWorker
from orchestrator.leader import LeaderElection
from orchestrator.metrics import MetricsCollector, LatencyHistogram
from orchestrator.transport import Transport

__all__ = [
    "Orchestrator",
    "TransactionDefinition",
    "Step",
    "TwoPCExecutor",
    "SagaExecutor",
    "CircuitBreaker",
    "WALEngine",
    "RecoveryWorker",
    "LeaderElection",
    "MetricsCollector",
    "LatencyHistogram",
    "Transport",
]
