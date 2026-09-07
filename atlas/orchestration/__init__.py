"""Atlas orchestration package: generic LangGraph-based governor.

Owns queue state, retries, checkpoints, and completion accounting so LLM
controllers never need to track progress themselves.
"""

from atlas.orchestration.state import QueueState, initial_queue_state
from atlas.orchestration.checkpoints import open_checkpointer, thread_config
from atlas.orchestration.graph import build_graph, run_to_completion
from atlas.orchestration import retry
from atlas.orchestration import events
from atlas.orchestration import metrics
from atlas.orchestration.run_lock import RunLock, RunLockStatus, RUN_ALREADY_ACTIVE, RUN_LOCK_ACQUIRED
from atlas.orchestration.shutdown import GracefulShutdown

__all__ = [
    "QueueState",
    "initial_queue_state",
    "open_checkpointer",
    "thread_config",
    "build_graph",
    "run_to_completion",
    "retry",
    "events",
    "metrics",
    "RunLock",
    "RunLockStatus",
    "RUN_ALREADY_ACTIVE",
    "RUN_LOCK_ACQUIRED",
    "GracefulShutdown",
]
