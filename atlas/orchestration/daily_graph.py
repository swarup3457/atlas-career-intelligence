"""The ONE root daily LangGraph graph (Phase 1E/F §5.1).

A single checkpointed graph governs a whole daily run. Like the production
graph, it uses one ``advance`` node re-invoked once per phase; state is
checkpointed after every phase so a crashed run resumes at the first
nonterminal phase and NEVER reruns a completed phase. The runtime — not any
prose or model — decides the truthful terminal state.
"""

from __future__ import annotations

from typing import Callable, Mapping

from langgraph.graph import END, StateGraph

from atlas.orchestration.daily_state import (
    DailyPhase,
    DailyState,
    DailyTerminalState,
    assert_daily_compact,
    next_daily_phase,
)

DailyHandler = Callable[[DailyState, object], DailyState]


class MissingDailyHandlerError(RuntimeError):
    pass


_MANDATORY_PHASES = tuple(p for p in DailyPhase if p != DailyPhase.COMPLETE)


def make_daily_advance_node(handlers: Mapping[DailyPhase, DailyHandler], runtime: object):
    def advance(state: DailyState) -> DailyState:
        phase = DailyPhase(state.get("phase", DailyPhase.INITIALIZE.value))
        if state.get("terminal_state"):
            return state
        if phase == DailyPhase.COMPLETE:
            state["terminal_state"] = DailyTerminalState.COMPLETE.value
            return state

        handler = handlers.get(phase)
        if handler is not None:
            state = handler(state, runtime)

        repeat = bool(state.pop("_repeat_phase", False))
        assert_daily_compact(state)

        if state.get("terminal_state"):
            return state

        if repeat:
            state["phase"] = phase.value
            return state

        completed = list(state.get("phases_completed", []))
        completed.append(phase.value)
        state["phases_completed"] = completed

        nxt = next_daily_phase(phase)
        if nxt is None or nxt == DailyPhase.COMPLETE:
            resolver = getattr(runtime, "resolve_terminal", None)
            terminal = DailyTerminalState.COMPLETE
            if resolver is not None:
                terminal = resolver(state)
            state["terminal_state"] = terminal.value
            if terminal == DailyTerminalState.COMPLETE:
                state["phase"] = DailyPhase.COMPLETE.value
        else:
            state["phase"] = nxt.value
        return state

    return advance


def build_daily_graph(handlers: Mapping[DailyPhase, DailyHandler], runtime: object):
    missing = [p.value for p in _MANDATORY_PHASES if p not in handlers]
    if missing:
        raise MissingDailyHandlerError(f"daily graph missing phase handlers: {missing}")
    builder = StateGraph(DailyState)
    builder.add_node("advance", make_daily_advance_node(handlers, runtime))
    builder.set_entry_point("advance")
    builder.add_edge("advance", END)
    return builder


def daily_is_terminal(state: DailyState) -> bool:
    return bool(state.get("terminal_state"))


__all__ = [
    "DailyHandler", "MissingDailyHandlerError",
    "make_daily_advance_node", "build_daily_graph", "daily_is_terminal",
]
