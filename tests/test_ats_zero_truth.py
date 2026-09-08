"""Phase 1C-A FINAL stabilization — list-only zero results preserve health truth
(build spec 8).

Failing-first regression: in the shared Greenhouse/Ashby list-only board path a
zero locally-staged result was ALWAYS mapped to ATTEMPTED_ZERO, discarding the
board's real health — so a parser/schema failure, an anti-bot access limitation,
or a rate-limit looked identical to a genuinely empty board.
"""

from __future__ import annotations

import pytest

from atlas.models import ErrorCategory
from atlas.persistence.sqlite import StateStore
from atlas.sources.adapter import AdapterError, SourceAdapter
from atlas.sources.child_executor import BoardSnapshotCache, CoverageChildExecutor
from atlas.sources.coverage import CoverageStatus, CoverageTask
from atlas.sources.executor import RateLimitedExecutor
from atlas.sources.health import SourceHealth, SourceHealthState
from atlas.sources.models import (
    Capability, SearchRequest, SearchResult, SourceInstance, SourceType, ZeroResultKind,
)
from atlas.sources.rate_limit import RateLimiter, RatePolicy
from atlas.sources.registry import SourceRegistry

pytestmark = pytest.mark.integration


class _ListBoard(SourceAdapter):
    """A list-only (SEARCH, no PAGINATION) board whose single response is
    configured per-instance to exercise each zero-result health class."""

    source_type = SourceType.FAKE
    CAPABILITIES = frozenset({Capability.SEARCH})
    adapter_version = "board-1"
    parser_version = "board-parser-1"

    def health_check(self) -> SourceHealth:
        return SourceHealth(SourceHealthState.HEALTHY, "ok")

    def search(self, request: SearchRequest) -> SearchResult:
        mode = self.instance.metadata.get("mode")
        if mode == "empty":
            return SearchResult(results=(), zero_result_kind=ZeroResultKind.TRUSTED_ZERO)
        if mode == "unresolved":
            return SearchResult(results=(), parse_findings=("selector drift: no cards",),
                                zero_result_kind=ZeroResultKind.EXTRACTION_UNRESOLVED)
        if mode == "anti_bot":
            raise AdapterError(ErrorCategory.ANTI_BOT, "interstitial challenge")
        if mode == "rate_limited":
            raise AdapterError(ErrorCategory.HTTP_429, "429 slow down", retry_after=1)
        raise AssertionError(f"unknown mode {mode!r}")


def _run(tmp_path, mode):
    store = StateStore(tmp_path / f"{mode}.sqlite"); store.create_run("run", "none")
    reg = SourceRegistry(); reg.register(_ListBoard)
    inst = SourceInstance("brd", SourceType.FAKE, metadata={"mode": mode})
    ex = CoverageChildExecutor(
        store, reg, {"brd": inst}, run_id="run", executor=RateLimitedExecutor(RateLimiter(RatePolicy())),
        snapshot_cache=BoardSnapshotCache(), retry_budget=0,
    )
    out = ex.execute(CoverageTask(coverage_id="c1", source_instance="brd", lane="JAVA_BACKEND", query_key="PRIMARY"))
    store.close()
    return out


def test_healthy_empty_board_is_attempted_zero(tmp_path):
    assert _run(tmp_path, "empty").status == CoverageStatus.ATTEMPTED_ZERO


def test_unresolved_board_is_not_attempted_zero(tmp_path):
    # A parser/schema failure that yields zero rows must NOT masquerade as a
    # genuinely empty board.
    assert _run(tmp_path, "unresolved").status == CoverageStatus.EXTRACTION_UNRESOLVED


def test_anti_bot_board_is_access_limited(tmp_path):
    assert _run(tmp_path, "anti_bot").status == CoverageStatus.ACCESS_LIMITED


def test_rate_limited_board_preserves_rate_limited(tmp_path):
    assert _run(tmp_path, "rate_limited").status == CoverageStatus.RATE_LIMITED
