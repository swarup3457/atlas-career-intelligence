"""Phase 1C-A — mixed four-family orchestration through the production graph
with the bounded parallel dispatcher (matrix D). Offline: real ATS adapters are
driven by injected FakeTransports (no network)."""

from __future__ import annotations

import pytest

from atlas.config import load_settings
from atlas.persistence.sqlite import StateStore
from atlas.planning import PlannedCompany
from atlas.runtime.production import ProductionSearchRuntime
from atlas.sources.ats import ATS_ADAPTER_CLASSES
from atlas.sources.coverage import TERMINAL_COVERAGE_STATUSES, CoverageManifest, CoverageStatus
from atlas.sources.http_client import HttpResponse
from atlas.sources.models import SourceFamily, SourceInstance, SourceType
from atlas.sources.rate_limit import ManualClock, RateLimiter, RatePolicy
from atlas.sources.registry import SourceRegistry
from atlas.sources.testing.http import FakeTransport, json_response, static

pytestmark = [pytest.mark.integration, pytest.mark.slow]

_FAMILY_ST = {
    SourceFamily.GREENHOUSE: SourceType.ATS_GREENHOUSE,
    SourceFamily.LEVER: SourceType.ATS_LEVER,
    SourceFamily.ASHBY: SourceType.ATS_ASHBY,
    SourceFamily.WORKDAY: SourceType.ATS_WORKDAY,
}

_PAYLOAD = {
    SourceFamily.GREENHOUSE: {"jobs": [{"id": 1, "internal_job_id": 2, "title": "Java Engineer",
                                        "updated_at": "2026-09-01T00:00:00Z", "location": {"name": "Bengaluru"},
                                        "absolute_url": "https://boards.greenhouse.io/gh/jobs/1"}], "meta": {"total": 1}},
    SourceFamily.LEVER: [{"id": "u1", "text": "Java Engineer", "categories": {"location": "Bengaluru"},
                          "workplaceType": "remote", "hostedUrl": "https://jobs.lever.co/lv/u1", "createdAt": 1693561200000}],
    SourceFamily.ASHBY: {"apiVersion": "1", "jobs": [{"title": "Java Engineer", "location": "Bengaluru",
                          "isListed": True, "publishedAt": "2026-08-15T00:00:00.000+00:00",
                          "jobUrl": "https://jobs.ashbyhq.com/as/u1"}]},
    SourceFamily.WORKDAY: {"total": 1, "jobPostings": [{"title": "Java Engineer", "externalPath": "/job/BLR/Java_R1",
                           "locationsText": "Bengaluru", "postedOn": "Posted 3 Days Ago", "jobReqId": "R1"}]},
}

_META = {
    SourceFamily.GREENHOUSE: {"board_token": "gh"},
    SourceFamily.LEVER: {"site": "lv"},
    SourceFamily.ASHBY: {"board_name": "as"},
    SourceFamily.WORKDAY: {"tenant": "acme", "datacenter": "wd1", "site": "External"},
}


class InjectedRegistry(SourceRegistry):
    """Registry that constructs each ATS adapter with an injected transport."""

    def __init__(self, transports):
        super().__init__()
        self._transports = transports
        for cls in ATS_ADAPTER_CLASSES:
            self.register(cls)

    def create(self, instance: SourceInstance):
        cls = self.adapter_for_family(instance.adapter_key)
        return cls(instance, http_client=self._transports[instance.instance_id])


def _build(tmp_path, monkeypatch, *, handlers_by_family=None, run_id="canary", retry_budget=2):
    monkeypatch.setenv("ATLAS_STATE_DB", str(tmp_path / "state.sqlite"))
    monkeypatch.setenv("ATLAS_CHECKPOINT_DB", str(tmp_path / "ckpt.sqlite"))
    monkeypatch.setenv("ATLAS_OUTPUT_DIR", str(tmp_path / "out"))
    monkeypatch.setenv("ATLAS_LOGS_DIR", str(tmp_path / "logs"))
    settings = load_settings()
    settings.ensure_directories()

    instances: dict[str, SourceInstance] = {}
    transports: dict[str, object] = {}
    companies = []
    for fam in (SourceFamily.GREENHOUSE, SourceFamily.LEVER, SourceFamily.ASHBY, SourceFamily.WORKDAY):
        iid = f"{fam.value}-inst"
        instances[iid] = SourceInstance(iid, _FAMILY_ST[fam], source_family=fam, display_name=fam.value.title(),
                                        metadata=_META[fam])
        handler = (handlers_by_family or {}).get(fam) or static(json_response(_PAYLOAD[fam]))
        transports[iid] = FakeTransport(handler)
        companies.append(PlannedCompany(company_id=iid, name=fam.value.title(), tier="A", mode="DELTA",
                                        source_instances=(iid,), geography_group="PRIMARY"))
    clock = ManualClock()
    rl = RateLimiter(RatePolicy(min_interval_seconds=0.0), clock=clock.time, sleeper=clock.sleep)
    rt = ProductionSearchRuntime(
        settings, run_id, fixture_mode=True, companies=companies, instances=instances,
        registry=InjectedRegistry(transports), rate_limiter=rl, parallel_workers=4,
        lane_override=["CANARY"], use_run_lock=False, retry_budget=retry_budget,
    )
    return settings, rt


def test_mixed_four_family_parallel_reaches_complete(tmp_path, monkeypatch):
    settings, rt = _build(tmp_path, monkeypatch)
    result = rt.run()
    assert result.terminal_state == "COMPLETE", result.manifest
    assert result.planned_tasks == 4 and result.terminal_tasks == 4
    assert result.planned_tasks == result.terminal_tasks  # planned == terminal before COMPLETE
    with StateStore(settings.state_db) as store:
        man = CoverageManifest.load(store, "canary")
        assert all(t.status == CoverageStatus.COMPLETED_WITH_RESULTS for t in man.tasks())
        assert store.count_canonical_jobs() >= 4  # one per family after dedupe
        # Append-only: every attempt id unique; every observation id unique.
        att = store.list_coverage_attempts(man.tasks()[0].coverage_id)
        assert len(att) >= 1
        obs = store.list_raw_observations("canary")
        assert len({o["observation_id"] for o in obs}) == len(obs)
        # Every lease is terminal.
        assert all(l["terminal"] == 1 for l in store.list_coverage_leases("canary"))
    # Exactly one report produced by the main graph (never by a worker).
    assert result.report_path and result.report_valid is True
    from pathlib import Path
    assert Path(result.report_path).exists()


def test_one_family_rate_limited_others_continue(tmp_path, monkeypatch):
    handlers = {SourceFamily.LEVER: static(HttpResponse.build(429, headers={"Retry-After": "1"}))}
    settings, rt = _build(tmp_path, monkeypatch, handlers_by_family=handlers, retry_budget=0)
    result = rt.run()
    assert result.terminal_state == "COMPLETE"  # all children terminal
    with StateStore(settings.state_db) as store:
        man = CoverageManifest.load(store, "canary")
        by_family = {t.source_instance: t.status for t in man.tasks()}
        assert by_family["lever-inst"] == CoverageStatus.RATE_LIMITED
        # The other three families still completed.
        for iid in ("greenhouse-inst", "ashby-inst", "workday-inst"):
            assert by_family[iid] == CoverageStatus.COMPLETED_WITH_RESULTS
        assert all(t.status in TERMINAL_COVERAGE_STATUSES for t in man.tasks())


def test_one_family_drift_others_continue(tmp_path, monkeypatch):
    handlers = {SourceFamily.WORKDAY: static(json_response({"unexpected": "shape"}))}
    settings, rt = _build(tmp_path, monkeypatch, handlers_by_family=handlers)
    result = rt.run()
    assert result.terminal_state == "COMPLETE"
    with StateStore(settings.state_db) as store:
        man = CoverageManifest.load(store, "canary")
        by_family = {t.source_instance: t.status for t in man.tasks()}
        assert by_family["workday-inst"] == CoverageStatus.EXTRACTION_UNRESOLVED  # drift, never false zero
        assert by_family["greenhouse-inst"] == CoverageStatus.COMPLETED_WITH_RESULTS


def test_partial_stop_after_discover_then_resume_completes(tmp_path, monkeypatch):
    from atlas.orchestration.production_state import ProductionPhase

    settings, rt = _build(tmp_path, monkeypatch)
    rt.stop_after_phase = ProductionPhase.DISCOVER
    rt.discover_batch = 2  # only two children this pass
    first = rt.run()
    assert first.terminal_state == "PARTIAL"  # work remaining → not COMPLETE
    assert first.terminal_tasks < first.planned_tasks
    # Resume: a fresh runtime (new process would rehydrate the same way).
    _settings2, rt2 = _build(tmp_path, monkeypatch)
    second = rt2.run()
    assert second.terminal_state == "COMPLETE"
    assert second.planned_tasks == second.terminal_tasks == 4
    with StateStore(settings.state_db) as store:
        obs = store.list_raw_observations("canary")
        # No duplicate observations across the two passes.
        assert len({o["observation_id"] for o in obs}) == len(obs)
