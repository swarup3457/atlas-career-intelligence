"""Phase 1A: source domain model tests."""

from __future__ import annotations

import pytest

from atlas.sources.models import (
    ActiveState,
    Capability,
    DetailRequest,
    DiscoveryResult,
    SearchRequest,
    SearchResult,
    SourceInstance,
    SourceType,
    WorkMode,
    ZeroResultKind,
)

pytestmark = pytest.mark.unit


def _result(**kw) -> DiscoveryResult:
    base = dict(source_type=SourceType.FAKE, source_instance="inst", title="Engineer")
    base.update(kw)
    return DiscoveryResult(**base)


def test_source_evidence_level_rename_keeps_backward_alias():
    # Build spec 19 / P1-8: the generic source ladder was renamed to
    # SourceEvidenceLevel to disambiguate it from the business VerificationLevel;
    # the old name remains a backward-compatible alias to the same enum.
    from atlas.sources.models import SourceEvidenceLevel, VerificationLevel

    assert VerificationLevel is SourceEvidenceLevel
    assert SourceEvidenceLevel.PORTAL_LIVE.value == "PORTAL_LIVE"


def test_content_hash_excludes_observational_fields():
    a = _result(discovered_at="2026-01-01T00:00:00Z", confidence=0.5)
    b = _result(discovered_at="2026-09-09T00:00:00Z", confidence=0.9)
    assert a.content_hash() == b.content_hash()


def test_content_hash_changes_with_substantive_change():
    a = _result(title="Engineer")
    b = _result(title="Senior Engineer")
    assert a.content_hash() != b.content_hash()


def test_source_identity_none_without_anchor():
    assert _result().source_identity() is None
    assert _result(source_job_id="J1").source_identity() == "FAKE::inst::J1"


def test_confidence_out_of_range_rejected():
    with pytest.raises(ValueError):
        _result(confidence=1.5)


def test_missing_instance_rejected():
    with pytest.raises(ValueError):
        DiscoveryResult(source_type=SourceType.FAKE, source_instance="")


def test_skills_coerced_to_tuple():
    r = _result(skills=["python", "sql"])
    assert r.skills == ("python", "sql")


def test_unknown_values_stay_unknown_not_invented():
    r = _result()
    assert r.work_mode == WorkMode.UNKNOWN
    assert r.is_active == ActiveState.UNKNOWN
    d = r.to_dict()
    assert d["company"] is None and d["location"] is None and d["deadline"] is None


def test_search_request_validation():
    with pytest.raises(ValueError):
        SearchRequest(page=0)
    with pytest.raises(ValueError):
        SearchRequest(limit=0)
    with pytest.raises(ValueError):
        SearchRequest(recency_days=-1)
    SearchRequest(query="x", recency_days=7)  # ok


def test_detail_request_requires_anchor():
    with pytest.raises(ValueError):
        DetailRequest()
    DetailRequest(url="https://x/y")  # ok


def test_search_result_count_and_zero_kind_default():
    sr = SearchResult()
    assert sr.count == 0
    assert sr.zero_result_kind == ZeroResultKind.NOT_APPLICABLE


def test_source_instance_to_dict_roundtrip_fields():
    inst = SourceInstance(
        instance_id="acme-wd",
        source_type=SourceType.ATS_WORKDAY,
        capability_overrides=frozenset({Capability.DETAIL}),
        auth_ref="ACME_TOKEN",
    )
    d = inst.to_dict()
    assert d["instance_id"] == "acme-wd"
    assert d["source_type"] == "ATS_WORKDAY"
    assert d["capability_overrides"] == ["DETAIL"]
    assert d["auth_ref"] == "ACME_TOKEN"
