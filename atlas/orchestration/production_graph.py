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

        # A handler may request re-entry into the SAME phase (batched/resumable
        # work, e.g. paged discovery) by setting ``_repeat_phase``. The flag is
        # transient and never checkpointed.
        repeat = bool(state.pop("_repeat_phase", False))

        assert_compact(state)

        # If a handler declared a terminal outcome (WAITING/FAILED/PARTIAL),
        # stop here — do NOT advance to COMPLETE.
        if state.get("terminal_state"):
            return state

        if repeat:
            # Stay in the current phase; the next invoke re-runs this handler on
            # the remaining work (checkpointed durable progress in between).
            state["phase"] = phase.value
            return state

        completed = list(state.get("phases_completed", []))
        completed.append(phase.value)
        state["phases_completed"] = completed
        nxt = next_phase(phase)
        if nxt is None or nxt == ProductionPhase.COMPLETE:
            # Real terminality gate (build spec 13 / P0-17): the runtime decides
            # the TRUE terminal state (COMPLETE only when the sealed plan's
            # required children are all terminal, persistence/report succeeded,
            # and no required human-blocked child remains). It may fail closed to
            # WAITING_FOR_HUMAN / PARTIAL / FAILED — never a false COMPLETE.
            resolver = getattr(runtime, "resolve_terminal", None)
            terminal = ProductionTerminalState.COMPLETE
            if resolver is not None:
                terminal = resolver(state)
            state["terminal_state"] = terminal.value
            if terminal == ProductionTerminalState.COMPLETE:
                state["phase"] = ProductionPhase.COMPLETE.value
        else:
            state["phase"] = nxt.value
        return state

    return advance_phase


# Mandatory production phases that MUST have a registered handler. A missing
# handler fails closed (build spec 13 / P0-17) — the graph never silently
# advances past a phase with no handler.
_MANDATORY_HANDLER_PHASES: tuple[ProductionPhase, ...] = tuple(
    p for p in ProductionPhase if p != ProductionPhase.COMPLETE
)


class MissingPhaseHandlerError(RuntimeError):
    """Raised when a mandatory production phase has no registered handler."""


def build_production_graph(handlers: Mapping[ProductionPhase, PhaseHandler], runtime: object):
    """Build (uncompiled) the production phase graph. Invoke repeatedly with
    an empty input until ``terminal_state`` is set (checkpoint after each).

    Validates that EVERY mandatory phase has a handler so a run can never
    silently advance past an unimplemented phase (build spec 13)."""
    missing = [p.value for p in _MANDATORY_HANDLER_PHASES if p not in handlers]
    if missing:
        raise MissingPhaseHandlerError(
            f"production graph is missing mandatory phase handlers: {missing}"
        )
    builder = StateGraph(ProductionState)
    builder.add_node("advance_phase", make_advance_node(handlers, runtime))
    builder.set_entry_point("advance_phase")
    builder.add_edge("advance_phase", END)
    return builder


def is_terminal(state: ProductionState) -> bool:
    return bool(state.get("terminal_state"))


__all__ = [
    "PhaseIsolationError",
    "MissingPhaseHandlerError",
    "PhaseContext",
    "PhaseHandler",
    "make_advance_node",
    "build_production_graph",
    "is_terminal",
]
