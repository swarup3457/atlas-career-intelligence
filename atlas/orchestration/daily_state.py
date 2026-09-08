"""Compact state + phase model for the ONE root daily LangGraph graph
(Phase 1E/F §5.1 / §11).

The daily run is governed by a single durable LangGraph graph whose phases are
checkpointed. As with the production graph, only COMPACT state is checkpointed
(run id, phase, completed phases, counters, small status flags) — never job
descriptions or payloads. Terminal outcomes are truthful and never conflated
with COMPLETE.
"""

from __future__ import annotations

import enum
import json
from typing import Optional, TypedDict


class DailyPhase(str, enum.Enum):
    INITIALIZE = "INITIALIZE"
    LOAD_POLICY = "LOAD_POLICY"
    LOAD_CANDIDATE = "LOAD_CANDIDATE"
    PLAN = "PLAN"
    EXECUTE_OFFICIAL = "EXECUTE_OFFICIAL"
    EXECUTE_MARKET = "EXECUTE_MARKET"
    CANONICALIZE = "CANONICALIZE"
    TRIAGE = "TRIAGE"
    DEEP_EVALUATE = "DEEP_EVALUATE"
    BUILD_PACKS = "BUILD_PACKS"
    PERSIST = "PERSIST"
    BUILD_REPORT = "BUILD_REPORT"
    PUBLISH_LATEST = "PUBLISH_LATEST"
    COMPLETE = "COMPLETE"


DAILY_PHASE_ORDER: tuple[DailyPhase, ...] = (
    DailyPhase.INITIALIZE,
    DailyPhase.LOAD_POLICY,
    DailyPhase.LOAD_CANDIDATE,
    DailyPhase.PLAN,
    DailyPhase.EXECUTE_OFFICIAL,
    DailyPhase.EXECUTE_MARKET,
    DailyPhase.CANONICALIZE,
    DailyPhase.TRIAGE,
    DailyPhase.DEEP_EVALUATE,
    DailyPhase.BUILD_PACKS,
    DailyPhase.PERSIST,
    DailyPhase.BUILD_REPORT,
    DailyPhase.PUBLISH_LATEST,
    DailyPhase.COMPLETE,
)


class DailyTerminalState(str, enum.Enum):
    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"
    WAITING_FOR_HUMAN = "WAITING_FOR_HUMAN"
    FAILED = "FAILED"


def next_daily_phase(phase: DailyPhase) -> Optional[DailyPhase]:
    idx = DAILY_PHASE_ORDER.index(phase)
    if idx + 1 < len(DAILY_PHASE_ORDER):
        return DAILY_PHASE_ORDER[idx + 1]
    return None


class DailyState(TypedDict, total=False):
    run_id: str
    started_at: str
    live: bool
    phase: str
    phases_completed: list[str]
    counters: dict[str, int]
    jobs_discovered: int
    jobs_ranked: int
    selected: int
    packs_built: int
    report_valid: Optional[bool]
    latest_updated: Optional[bool]
    status: Optional[str]
    terminal_state: Optional[str]
    notes: list[str]


def initial_daily_state(run_id: str, *, live: bool = False, started_at: str = "") -> DailyState:
    return DailyState(
        run_id=run_id, started_at=started_at, live=live,
        phase=DailyPhase.INITIALIZE.value, phases_completed=[], counters={},
        jobs_discovered=0, jobs_ranked=0, selected=0, packs_built=0,
        report_valid=None, latest_updated=None, status=None,
        terminal_state=None, notes=[],
    )


_FORBIDDEN_KEYS = frozenset(
    {"jobs", "descriptions", "html", "raw", "payload", "payloads", "results", "evaluations"}
)


def assert_daily_compact(state: DailyState, *, max_bytes: int = 32768) -> None:
    for key in state:
        if key in _FORBIDDEN_KEYS:
            raise ValueError(f"forbidden bulky field {key!r} in daily checkpoint state")
    size = len(json.dumps(state, sort_keys=True, ensure_ascii=True, default=str).encode("utf-8"))
    if size > max_bytes:
        raise ValueError(f"daily checkpoint too large: {size} > {max_bytes} bytes")


def daily_checkpoint_size(state: DailyState) -> int:
    return len(json.dumps(state, sort_keys=True, ensure_ascii=True, default=str).encode("utf-8"))


__all__ = [
    "DailyPhase", "DAILY_PHASE_ORDER", "DailyTerminalState",
    "next_daily_phase", "DailyState", "initial_daily_state",
    "assert_daily_compact", "daily_checkpoint_size",
]
