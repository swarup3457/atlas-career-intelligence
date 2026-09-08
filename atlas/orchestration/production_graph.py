"""Atlas production multi-phase graph (Phase 1B, build spec 7.11 / file 13).

The generic single-node queue graph (:mod:`atlas.orchestration.graph`) is
kept for tests and subqueues. This module adds an EXPLICIT production graph
whose checkpointed phases run in a fixed order and enforce search-first
isolation: persistence / Excel / remote-audit side effects are allowed ONLY
in the persistence phases and NEVER during discovery.

Each ``advance_phase`` invoke runs exactly one phase handler and advances the
phase pointer, checkpointing a COMPACT :class:`ProductionState` (IDs,
counters, cursors, phase — never full job descriptions). PARTIAL,
WAITING_FOR_HUMAN and FAILED are distinct terminal outcomes and are never
confused with COMPLETE.
"""

from __future__ import annotations

from typing import Callable, Mapping, Optional

from langgraph.graph import END, StateGraph

from atlas.orchestration.production_state import (
    ProductionPhase,
    ProductionState,
    ProductionTerminalState,
    assert_compact,
    next_phase,
    persistence_allowed,
)


class PhaseIsolationError(RuntimeError):
    """Raised when a handler attempts a persistence/remote side effect during
    a non-persistence (e.g. discovery) phase."""


# A handler mutates and returns the compact ProductionState. It receives an
# opaque context object (the runtime) providing policy/planner/adapters/store.
PhaseHandler = Callable[[ProductionState, object], ProductionState]


class PhaseContext:
    """Tracks whether persistence side effects have occurred, so discovery
    phases can be proven not to have written the report / remote audit."""

    def __init__(self) -> None:
        self.persistence_side_effects: list[str] = []

    def record_side_effect(self, phase: ProductionPhase, what: str) -> None:
        if not persistence_allowed(phase):
            raise PhaseIsolationError(
                f"side effect {what!r} attempted during non-persistence phase {phase.value}"
            )
        self.persistence_side_effects.append(f"{phase.value}:{what}")


def make_advance_node(handlers: Mapping[ProductionPhase, PhaseHandler], runtime: object):
    def advance_phase(state: ProductionState) -> ProductionState:
        phase = ProductionPhase(state.get("phase", ProductionPhase.INITIALIZE.value))
        if state.get("terminal_state"):
            return state
        if phase == ProductionPhase.COMPLETE:
            state["terminal_state"] = ProductionTerminalState.COMPLETE.value
            return state

        handler = handlers.get(phase)
        if handler is not None:
            state = handler(state, runtime)

        assert_compact(state)

        # If a handler declared a terminal outcome (WAITING/FAILED/PARTIAL),
        # stop here — do NOT advance to COMPLETE.
        if state.get("terminal_state"):
            return state

        completed = list(state.get("phases_completed", []))
        completed.append(phase.value)
        state["phases_completed"] = completed
        nxt = next_phase(phase)
        if nxt is None or nxt == ProductionPhase.COMPLETE:
            state["phase"] = ProductionPhase.COMPLETE.value
            state["terminal_state"] = ProductionTerminalState.COMPLETE.value
        else:
            state["phase"] = nxt.value
        return state

    return advance_phase


def build_production_graph(handlers: Mapping[ProductionPhase, PhaseHandler], runtime: object):
    """Build (uncompiled) the production phase graph. Invoke repeatedly with
    an empty input until ``terminal_state`` is set (checkpoint after each)."""
    builder = StateGraph(ProductionState)
    builder.add_node("advance_phase", make_advance_node(handlers, runtime))
    builder.set_entry_point("advance_phase")
    builder.add_edge("advance_phase", END)
    return builder


def is_terminal(state: ProductionState) -> bool:
    return bool(state.get("terminal_state"))


__all__ = [
    "PhaseIsolationError",
    "PhaseContext",
    "PhaseHandler",
    "make_advance_node",
    "build_production_graph",
    "is_terminal",
]
