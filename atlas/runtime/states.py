"""Atlas run-level state machine (Phase 0.75 spec section 3).

Explicit, deterministic run states and transitions. Nothing in this
module ever consults an LLM/controller to decide the run lifecycle -
transitions are pure functions of durable, already-computed facts
(planned/completed/remaining/retry-pending/waiting-for-human counts),
never free-form model output.
"""

from __future__ import annotations

import enum


class RunState(str, enum.Enum):
    INITIALIZING = "INITIALIZING"
    RUNNING = "RUNNING"
    WAITING_FOR_HUMAN = "WAITING_FOR_HUMAN"
    PARTIAL = "PARTIAL"
    RESUMING = "RESUMING"
    FINALIZING = "FINALIZING"
    COMPLETE = "COMPLETE"
    FAILED = "FAILED"


# Deterministic allowed-transition table. Any transition not listed here
# is rejected by `transition()` - this is the single source of truth for
# "what run-lifecycle moves are legal", so nothing else (including a
# future LLM controller) can invent an illegal lifecycle jump.
ALLOWED_TRANSITIONS: dict[RunState, frozenset[RunState]] = {
    RunState.INITIALIZING: frozenset({RunState.RUNNING, RunState.FAILED}),
    RunState.RUNNING: frozenset(
        {
            RunState.RUNNING,
            RunState.PARTIAL,
            RunState.FINALIZING,
            RunState.WAITING_FOR_HUMAN,
            RunState.FAILED,
        }
    ),
    RunState.WAITING_FOR_HUMAN: frozenset(
        {RunState.RUNNING, RunState.RESUMING, RunState.PARTIAL, RunState.FAILED}
    ),
    RunState.PARTIAL: frozenset({RunState.RESUMING, RunState.FAILED}),
    RunState.RESUMING: frozenset(
        {RunState.RUNNING, RunState.FINALIZING, RunState.WAITING_FOR_HUMAN, RunState.FAILED}
    ),
    RunState.FINALIZING: frozenset(
        {RunState.COMPLETE, RunState.WAITING_FOR_HUMAN, RunState.PARTIAL, RunState.FAILED}
    ),
    RunState.COMPLETE: frozenset(set()),  # terminal
    RunState.FAILED: frozenset(set()),  # terminal
}

TERMINAL_RUN_STATES: frozenset[RunState] = frozenset({RunState.COMPLETE, RunState.FAILED})


class IllegalRunStateTransition(RuntimeError):
    """Raised when a caller attempts a run-state transition that is not
    in the deterministic ALLOWED_TRANSITIONS table."""


def transition(current: RunState, target: RunState) -> RunState:
    """Validate and return the new run state.

    Deterministic: depends only on the (current, target) pair, never on
    free-form model output. Raises IllegalRunStateTransition for any
    transition not explicitly allowed above (including no-ops that are
    not explicitly listed, so accidental self-loops on terminal states
    are also rejected).
    """
    allowed = ALLOWED_TRANSITIONS.get(current, frozenset())
    if target not in allowed:
        raise IllegalRunStateTransition(
            f"Illegal run-state transition: {current.value} -> {target.value} "
            f"(allowed from {current.value}: {sorted(s.value for s in allowed)})"
        )
    return target


def is_terminal(state: RunState) -> bool:
    return state in TERMINAL_RUN_STATES
