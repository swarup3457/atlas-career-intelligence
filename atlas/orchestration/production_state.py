"""Compact production run state + phase model (Phase 1B, build spec 7.10/7.11).

The generic queue state (:mod:`atlas.orchestration.state`) serializes full
per-item results and an unbounded attempt log into LangGraph state. At real
market scale that bloats the SQLite checkpoint and any model/tool context
(P0-8). The production graph instead persists detailed observations,
results, and attempts into local SQLite / the evidence store, and
checkpoints ONLY:

    * run id
    * policy + plan fingerprints
    * current phase and completed phases
    * planned task IDs
    * counters
    * per-source cursors
    * retry references
    * compact result IDs / status summaries

There are NO job descriptions, source payloads, or unbounded attempt logs
in this state. :func:`checkpoint_size_bytes` measures the serialized size so
a regression test can prove it stays bounded across thousands of
discoveries.
"""

from __future__ import annotations

import enum
import json
from typing import Any, Optional, TypedDict


class ProductionPhase(str, enum.Enum):
    """The deterministic, checkpointed phases of a production run — the
    explicit implementation of file 13's search-first isolation."""

    INITIALIZE = "INITIALIZE"
    LOAD_PRIVATE_PROFILE = "LOAD_PRIVATE_PROFILE"
    LOAD_POLICY = "LOAD_POLICY"
    BUILD_AND_SEAL_PLAN = "BUILD_AND_SEAL_PLAN"
    DISCOVER = "DISCOVER"
    SOURCE_HEALTH_GATE = "SOURCE_HEALTH_GATE"
    DETAIL_HYDRATION = "DETAIL_HYDRATION"
    OFFICIAL_VERIFICATION = "OFFICIAL_VERIFICATION"
    DEDUPE_AND_REPOST_CLASSIFICATION = "DEDUPE_AND_REPOST_CLASSIFICATION"
    CANDIDATE_MATCH = "CANDIDATE_MATCH"
    PERSIST_LOCAL = "PERSIST_LOCAL"
    BUILD_REPORT = "BUILD_REPORT"
    OPTIONAL_REMOTE_AUDIT = "OPTIONAL_REMOTE_AUDIT"
    COMPLETE = "COMPLETE"


# The strict order in which phases execute. Search (DISCOVER) must precede
# any persistence/report/remote-audit phase — persistence/Excel/remote work
# NEVER interleaves with discovery (build spec 7.11 / file 13).
PRODUCTION_PHASE_ORDER: tuple[ProductionPhase, ...] = (
    ProductionPhase.INITIALIZE,
    ProductionPhase.LOAD_PRIVATE_PROFILE,
    ProductionPhase.LOAD_POLICY,
    ProductionPhase.BUILD_AND_SEAL_PLAN,
    ProductionPhase.DISCOVER,
    ProductionPhase.SOURCE_HEALTH_GATE,
    ProductionPhase.DETAIL_HYDRATION,
    ProductionPhase.OFFICIAL_VERIFICATION,
    ProductionPhase.DEDUPE_AND_REPOST_CLASSIFICATION,
    ProductionPhase.CANDIDATE_MATCH,
    ProductionPhase.PERSIST_LOCAL,
    ProductionPhase.BUILD_REPORT,
    ProductionPhase.OPTIONAL_REMOTE_AUDIT,
    ProductionPhase.COMPLETE,
)

# Phases in which persistence / Excel / remote-audit side effects are allowed.
# DISCOVER and the verification/match phases must NOT write the report or
# perform remote audit.
_PERSISTENCE_PHASES: frozenset[ProductionPhase] = frozenset(
    {
        ProductionPhase.PERSIST_LOCAL,
        ProductionPhase.BUILD_REPORT,
        ProductionPhase.OPTIONAL_REMOTE_AUDIT,
    }
)

_SEARCH_PHASES: frozenset[ProductionPhase] = frozenset(
    {ProductionPhase.DISCOVER, ProductionPhase.SOURCE_HEALTH_GATE, ProductionPhase.DETAIL_HYDRATION}
)


def is_search_phase(phase: ProductionPhase) -> bool:
    return phase in _SEARCH_PHASES


def persistence_allowed(phase: ProductionPhase) -> bool:
    return phase in _PERSISTENCE_PHASES


def next_phase(phase: ProductionPhase) -> Optional[ProductionPhase]:
    idx = PRODUCTION_PHASE_ORDER.index(phase)
    if idx + 1 < len(PRODUCTION_PHASE_ORDER):
        return PRODUCTION_PHASE_ORDER[idx + 1]
    return None


class ProductionTerminalState(str, enum.Enum):
    """Truthful terminal outcomes — never conflated with COMPLETE."""

    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"
    WAITING_FOR_HUMAN = "WAITING_FOR_HUMAN"
    FAILED = "FAILED"


class ProductionState(TypedDict, total=False):
    run_id: str
    policy_fingerprint: str
    plan_fingerprint: str
    phase: str
    phases_completed: list[str]
    task_ids: list[str]
    cursors: dict[str, str]
    counters: dict[str, int]
    retry_refs: dict[str, int]
    result_summary: dict[str, str]   # task_id -> compact status string ONLY
    human_waiting: list[str]
    terminal_state: Optional[str]
    notes: list[str]


def initial_production_state(run_id: str) -> ProductionState:
    return ProductionState(
        run_id=run_id,
        policy_fingerprint="",
        plan_fingerprint="",
        phase=ProductionPhase.INITIALIZE.value,
        phases_completed=[],
        task_ids=[],
        cursors={},
        counters={},
        retry_refs={},
        result_summary={},
        human_waiting=[],
        terminal_state=None,
        notes=[],
    )


def bump(state: ProductionState, counter: str, by: int = 1) -> None:
    counters = state.setdefault("counters", {})
    counters[counter] = counters.get(counter, 0) + by


def checkpoint_size_bytes(state: ProductionState) -> int:
    """Serialized size of the compact checkpoint. Used by a regression test
    to prove the checkpoint stays bounded regardless of discovery volume."""
    return len(json.dumps(state, sort_keys=True, ensure_ascii=True, default=str).encode("utf-8"))


# Keys that would indicate full payloads leaking into the checkpoint. The
# production graph must never place these into ProductionState.
FORBIDDEN_CHECKPOINT_KEYS: frozenset[str] = frozenset(
    {"description", "descriptions", "html", "raw", "payload", "payloads", "results", "attempt_log", "jobs"}
)


def assert_compact(state: ProductionState, *, max_bytes: int = 65536) -> None:
    """Raise if the checkpoint carries forbidden bulky fields or exceeds a
    hard size ceiling. A safety net the production graph asserts each phase."""
    for key in state:
        if key in FORBIDDEN_CHECKPOINT_KEYS:
            raise ValueError(f"forbidden bulky field {key!r} in production checkpoint state")
    # result_summary values must be short status strings, never payloads.
    for tid, summary in state.get("result_summary", {}).items():
        if not isinstance(summary, str) or len(summary) > 64:
            raise ValueError(f"result_summary[{tid!r}] must be a short status string, not a payload")
    size = checkpoint_size_bytes(state)
    if size > max_bytes:
        raise ValueError(f"production checkpoint too large: {size} > {max_bytes} bytes")


__all__ = [
    "ProductionPhase",
    "PRODUCTION_PHASE_ORDER",
    "ProductionTerminalState",
    "ProductionState",
    "initial_production_state",
    "is_search_phase",
    "persistence_allowed",
    "next_phase",
    "bump",
    "checkpoint_size_bytes",
    "assert_compact",
    "FORBIDDEN_CHECKPOINT_KEYS",
]
