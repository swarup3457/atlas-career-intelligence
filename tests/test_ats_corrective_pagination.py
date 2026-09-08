"""Phase 1C-A CORRECTIVE gate — durable pagination/cursor resume, real query
compilation, shared board snapshot fan-out, and full observation provenance
(build spec 10/11/12).

Uses purpose-built paginating and list-only test adapters (the shared FakeAdapter
returns the same page every time, so it cannot exercise real multi-page
pagination). Each test is a failing-first regression against a specific defect:
before this gate a child became terminal after ONE page (discarding has_more),
sent the lane enum as the literal query, refetched a list board once per lane,
and dropped most typed observation fields during staging.
"""

from __future__ import annotations

import pytest

from atlas.persistence.sqlite import StateStore
from atlas.planning.query_compiler import SearchQueryCompiler
from atlas.policy.loader import load_policy
from atlas.sources.adapter import SourceAdapter, new_result_base
from atlas.sources.child_executor import BoardSnapshotCache, CoverageChildExecutor
from atlas.sources.coverage import CoverageStatus, CoverageTask
from atlas.sources.executor import RateLimitedExecutor
from atlas.sources.health import SourceHealth, SourceHealthState
from atlas.sources.models import (
    ActiveState,
    Capability,
    DiscoveryResult,
    SearchRequest,
    SearchResult,
    SourceInstance,
    SourceType,
    VerificationLevel,
    WorkMode,
    ZeroResultKind,
)
from atlas.sources.registry import SourceRegistry

pytestmark = pytest.mark.integration


# --- test adapters ----------------------------------------------------------
class PagingAdapter(SourceAdapter):
    """A cursor-paginated source returning DISTINCT postings per page."""

    source_type = SourceType.FAKE
    CAPABILITIES = frozenset({Capability.SEARCH, Capability.PAGINATION, Capability.DETAIL})
    adapter_version = "paging-1"
    parser_version = "paging-parser-1"

    def __init__(self, instance):
        super().__init__(instance)
        md = instance.metadata
        self.total = int(md.get("total", 7))
        self.page_size = int(md.get("page_size", 3))
        self.loop = bool(md.get("loop", False))
        self.total_shift = bool(md.get("total_shift", False))
        self.calls = 0

    def health_check(self) -> SourceHealth:
        return SourceHealth(SourceHealthState.HEALTHY, "ok")

    def _item(self, i: int) -> DiscoveryResult:
        return DiscoveryResult(**new_result_base(
            self, source_job_id=f"{self.instance_id}-{i}",
            canonical_url=f"https://x/{self.instance_id}/{i}", company="Acme",
            title=f"Java Developer {i}", location="Bengaluru", work_mode=WorkMode.REMOTE,
            posted_at="2026-09-01", is_active=ActiveState.ACTIVE,
            verification_level=VerificationLevel.OFFICIAL_SEARCH_LIVE,
            skills=("Java", "Spring"), salary_text="₹20L", employment_type="Full-time",
            experience_text="4-6 years", deadline="2026-12-01", updated_at="2026-09-02",
            confidence=0.9))

    def search(self, request: SearchRequest) -> SearchResult:
        self.calls += 1
        offset = int(request.cursor) if request.cursor else 0
        items = tuple(self._item(i) for i in range(offset, min(offset + self.page_size, self.total)))
        has_more = (offset + self.page_size) < self.total
        if self.loop:
            next_cursor = "0" if has_more else None  # always points back → loop
        else:
            next_cursor = str(offset + self.page_size) if has_more else None
        total = self.total + 5 if (self.total_shift and offset > 0) else self.total
        return SearchResult(results=items, page=request.page, has_more=has_more,
                            next_cursor=next_cursor, total_reported=total,
                            zero_result_kind=ZeroResultKind.NOT_APPLICABLE)


class ListBoardAdapter(SourceAdapter):
    """A list-only whole-board source (no PAGINATION) — mixed lanes/locations."""

    source_type = SourceType.FAKE
    CAPABILITIES = frozenset({Capability.SEARCH})
    adapter_version = "board-1"
    parser_version = "board-parser-1"
    _fetches: dict = {}

    def __init__(self, instance):
        super().__init__(instance)
        self.board = [
            ("Java Developer", "Bengaluru"),
            ("Senior Java Backend Engineer", "Hyderabad"),
            ("React Frontend Engineer", "Bengaluru"),
            ("Sales Executive", "Mumbai"),
            (".NET Developer", "Pune"),
        ]

    def health_check(self) -> SourceHealth:
        return SourceHealth(SourceHealthState.HEALTHY, "ok")

    def search(self, request: SearchRequest) -> SearchResult:
        ListBoardAdapter._fetches[self.instance_id] = ListBoardAdapter._fetches.get(self.instance_id, 0) + 1
        results = tuple(
            DiscoveryResult(**new_result_base(
                self, source_job_id=f"{self.instance_id}-{i}",
                canonical_url=f"https://b/{self.instance_id}/{i}", company="Acme",
                title=t, location=loc, is_active=ActiveState.ACTIVE,
                verification_level=VerificationLevel.OFFICIAL_SEARCH_LIVE))
            for i, (t, loc) in enumerate(self.board)
        )
        return SearchResult(results=results, has_more=False, total_reported=len(results),
                            zero_result_kind=ZeroResultKind.NOT_APPLICABLE)


def _reg(adapter_cls) -> SourceRegistry:
    reg = SourceRegistry()
    reg.register(adapter_cls)
    return reg


def _store(tmp_path, run_id="run", name="s.sqlite"):
    store = StateStore(tmp_path / name)
    store.create_run(run_id, "none")
    return store


# ---------------------------------------------------------------------------
# §11 — query compilation (unit)
# ---------------------------------------------------------------------------
def test_compiler_emits_real_terms_not_enum_labels():
    b = load_policy()
    c = SearchQueryCompiler(b.lanes, b.geography, policy_version=b.short_fingerprint)
    jb = c.compile("JAVA_BACKEND", "PRIMARY")
    assert jb.primary_query and "JAVA_BACKEND" not in jb.query_terms
    assert jb.is_relevant("Java Developer") and not jb.is_relevant("Sales Executive")
    assert not jb.is_relevant("React Frontend Engineer")  # lane independence
    rf = c.compile("REACT_FRONTEND", "PRIMARY")
    assert rf.is_relevant("React Developer") and not rf.is_relevant("Java Developer")
    dn = c.compile("DOTNET", "PRIMARY") if "DOTNET" in b.lanes else None
    # Geography aliases.
    assert jb.location_in_group("Bangalore", b.geography)
    assert jb.location_in_group("Bengaluru", b.geography)
    assert not jb.location_in_group("Remote", b.geography)  # not worldwide
    sec = c.compile("JAVA_BACKEND", "SECONDARY")
    assert sec.location_in_group("Remote India", b.geography)
    assert not jb.location_in_group("Remote India", b.geography)


# ---------------------------------------------------------------------------
# §10 — durable pagination + resume
# ---------------------------------------------------------------------------
def _executor(store, tmp_path, **kw):
    inst = SourceInstance("i0", SourceType.FAKE, metadata=kw.pop("md", {}))
    return CoverageChildExecutor(store, _reg(PagingAdapter), {"i0": inst}, run_id="run",
                                 executor=RateLimitedExecutor(), **kw), inst


def test_all_pages_are_fetched_no_silent_truncation(tmp_path):
    store = _store(tmp_path)
    inst = SourceInstance("i0", SourceType.FAKE, metadata={"total": 7, "page_size": 3})
    ex = CoverageChildExecutor(store, _reg(PagingAdapter), {"i0": inst}, run_id="run")
    task = CoverageTask(coverage_id="c1", source_instance="i0", lane="JAVA_BACKEND", query_key="PRIMARY")
    outcome = ex.execute(task)
    assert outcome.status == CoverageStatus.COMPLETED_WITH_RESULTS
    assert store.count_raw_observations("run") == 7  # every page staged, none discarded
    pages = store.list_coverage_pages("run", "c1")
    assert len([p for p in pages if p["status"] == "DONE"]) == 3  # 3 pages of size 3 (7 items)
    assert not store.coverage_pages_outstanding("run", "c1")
    store.close()


def test_crash_after_page1_resumes_in_new_process_without_duplicates(tmp_path):
    db = tmp_path / "s.sqlite"
    store = StateStore(db); store.create_run("run", "none")
    inst = SourceInstance("i0", SourceType.FAKE, metadata={"total": 7, "page_size": 3})
    calls = {"n": 0}

    def crash(task, attempt):
        calls["n"] += 1
        if calls["n"] == 2:  # page 1 ok (call 1), crash entering page 2 (call 2)
            raise RuntimeError("boom mid-pagination")

    ex = CoverageChildExecutor(store, _reg(PagingAdapter), {"i0": inst}, run_id="run", crash_hook=crash)
    task = CoverageTask(coverage_id="c1", source_instance="i0", lane="JAVA_BACKEND", query_key="PRIMARY")
    with pytest.raises(RuntimeError):
        ex.execute(task)
    obs_after_crash = store.count_raw_observations("run")
    assert obs_after_crash == 3  # only page 1 staged
    assert store.coverage_pages_outstanding("run", "c1")  # a required page remains
    store.close()

    # New process: fresh store + executor, no crash hook → resumes at page 2.
    store2 = StateStore(db)
    ex2 = CoverageChildExecutor(store2, _reg(PagingAdapter), {"i0": inst}, run_id="run")
    outcome = ex2.execute(task)
    assert outcome.status == CoverageStatus.COMPLETED_WITH_RESULTS
    assert store2.count_raw_observations("run") == 7  # no duplicates after resume
    assert not store2.coverage_pages_outstanding("run", "c1")
    store2.close()


def test_cursor_loop_is_detected(tmp_path):
    store = _store(tmp_path)
    inst = SourceInstance("i0", SourceType.FAKE, metadata={"total": 9, "page_size": 3, "loop": True})
    ex = CoverageChildExecutor(store, _reg(PagingAdapter), {"i0": inst}, run_id="run")
    outcome = ex.execute(CoverageTask(coverage_id="c1", source_instance="i0", lane="JAVA_BACKEND", query_key="PRIMARY"))
    assert outcome.status == CoverageStatus.EXTRACTION_UNRESOLVED
    assert outcome.detail.get("reason") == "cursor_loop"
    store.close()


def test_total_shift_is_detected_and_reported(tmp_path):
    store = _store(tmp_path)
    inst = SourceInstance("i0", SourceType.FAKE, metadata={"total": 7, "page_size": 3, "total_shift": True})
    ex = CoverageChildExecutor(store, _reg(PagingAdapter), {"i0": inst}, run_id="run")
    outcome = ex.execute(CoverageTask(coverage_id="c1", source_instance="i0", lane="JAVA_BACKEND", query_key="PRIMARY"))
    assert outcome.detail.get("total_shift") is True
    store.close()


def test_budget_exhaustion_is_partial_not_complete(tmp_path):
    store = _store(tmp_path)
    inst = SourceInstance("i0", SourceType.FAKE, metadata={"total": 100, "page_size": 3})
    ex = CoverageChildExecutor(store, _reg(PagingAdapter), {"i0": inst}, run_id="run", max_pages=2)
    outcome = ex.execute(CoverageTask(coverage_id="c1", source_instance="i0", lane="JAVA_BACKEND", query_key="PRIMARY"))
    assert outcome.status == CoverageStatus.PARTIAL_BUDGET  # honest, not COMPLETED
    assert outcome.detail.get("budget_exceeded") is True
    store.close()


# ---------------------------------------------------------------------------
# §11 — shared board snapshot fan-out (list-only board, many lanes)
# ---------------------------------------------------------------------------
def test_shared_board_snapshot_serves_many_lanes_with_one_fetch(tmp_path):
    ListBoardAdapter._fetches.clear()
    store = _store(tmp_path)
    b = load_policy()
    compiler = SearchQueryCompiler(b.lanes, b.geography, policy_version="pol")
    inst = SourceInstance("brd", SourceType.FAKE)
    cache = BoardSnapshotCache()
    ex = CoverageChildExecutor(store, _reg(ListBoardAdapter), {"brd": inst}, run_id="run",
                               query_compiler=compiler, geography=b.geography, snapshot_cache=cache,
                               apply_relevance=True)
    # Three lanes, all against the SAME board instance.
    java = ex.execute(CoverageTask(coverage_id="cj", source_instance="brd", lane="JAVA_BACKEND", query_key="PRIMARY"))
    react = ex.execute(CoverageTask(coverage_id="cr", source_instance="brd", lane="REACT_FRONTEND", query_key="PRIMARY"))
    dotnet_lane = "DOTNET" if "DOTNET" in b.lanes else "GENERAL_SOFTWARE"
    dn = ex.execute(CoverageTask(coverage_id="cd", source_instance="brd", lane=dotnet_lane, query_key="PRIMARY"))

    # ONE network acquisition served all three lanes.
    assert cache.fetch_count[f"run::brd"] == 1
    assert ListBoardAdapter._fetches["brd"] == 1
    # Each lane evaluated independently from the shared snapshot.
    assert java.status == CoverageStatus.COMPLETED_WITH_RESULTS
    assert react.status == CoverageStatus.COMPLETED_WITH_RESULTS
    # The Java child staged only Java-relevant PRIMARY postings (not React/Sales).
    java_titles = [r["title"] for r in store.list_raw_observations("run") if r["coverage_id"] == "cj"]
    assert any("Java" in t for t in java_titles)
    assert not any("Sales" in t or "React" in t for t in java_titles)
    store.close()


# ---------------------------------------------------------------------------
# §12 — full observation provenance round-trip
# ---------------------------------------------------------------------------
def test_full_observation_provenance_round_trips_and_is_bounded(tmp_path):
    import json
    store = _store(tmp_path)

    class RichAdapter(PagingAdapter):
        def _item(self, i):
            return DiscoveryResult(**new_result_base(
                self, source_job_id=f"{self.instance_id}-{i}",
                canonical_url=f"https://x/{self.instance_id}/{i}", source_url=f"https://x/{self.instance_id}/{i}",
                company="Acme", title=f"Java Developer {i}", location="Bengaluru", work_mode=WorkMode.REMOTE,
                posted_at="2026-09-01", updated_at="2026-09-02", deadline="2026-12-01",
                employment_type="Full-time", experience_text="4-6 years", salary_text="20 LPA",
                skills=("Java", "Spring", "SQL"),
                description="Great role. token=ghp_" + "a" * 40 + " " + "x" * 5000,  # secret + long
                is_active=ActiveState.ACTIVE, verification_level=VerificationLevel.OFFICIAL_SEARCH_LIVE,
                confidence=0.88))

    inst = SourceInstance("i0", SourceType.FAKE, metadata={"total": 1, "page_size": 3})
    ex = CoverageChildExecutor(store, _reg(RichAdapter), {"i0": inst}, run_id="run")
    ex.execute(CoverageTask(coverage_id="c1", source_instance="i0", lane="JAVA_BACKEND", query_key="PRIMARY"))
    rows = store.list_raw_observations("run")
    assert len(rows) == 1
    row = rows[0]
    # Typed columns preserved.
    assert row["title"].startswith("Java Developer") and row["location"] == "Bengaluru"
    assert row["posted_at"] == "2026-09-01" and row["source_job_id"] == "i0-0"
    assert row["adapter_version"] == "paging-1"
    detail = json.loads(row["detail_json"])
    # Full typed provenance preserved in bounded detail.
    for key in ("work_mode", "updated_at", "deadline", "employment_type", "experience_text",
                "salary_text", "skills", "confidence", "date_provenance", "detail_anchor"):
        assert key in detail
    assert detail["skills"] == ["Java", "Spring", "SQL"]
    assert detail["employment_type"] == "Full-time"
    # Description is bounded and secret-redacted — no unbounded HTML, no token.
    frag = detail["description_fragment"] or ""
    assert len(frag) <= 2100 and "ghp_" not in frag
    assert detail["description_len"] > 4000  # original length recorded
    store.close()
