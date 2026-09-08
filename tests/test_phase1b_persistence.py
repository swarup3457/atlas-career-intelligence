"""Phase 1B — first-class source instance persistence + health granularity."""

from __future__ import annotations

import pytest

from atlas.persistence.sqlite import SCHEMA_VERSION, StateStore
from atlas.sources.health import HealthEvidence, SourceHealthState, classify_health
from atlas.sources.instance_store import load_instance, load_instances, save_instance
from atlas.sources.models import Capability, SourceFamily, SourceInstance, SourceType
from atlas.sources.query_signature import QuerySignature

pytestmark = pytest.mark.unit


def _store(tmp_path) -> StateStore:
    return StateStore(tmp_path / "s.sqlite")


def test_schema_is_v6(tmp_path):
    with _store(tmp_path) as store:
        assert store.schema_version() == SCHEMA_VERSION
        assert SCHEMA_VERSION >= 6


def test_source_instance_persists_idempotently(tmp_path):
    inst = SourceInstance(
        instance_id="acme-wd",
        source_type=SourceType.ATS_WORKDAY,
        tenant="acme",
        site="External",
        company_id="co-acme",
        auth_ref="ACME_WD_TOKEN",
        capability_removals=frozenset({Capability.POSTED_DATE}),
    )
    with _store(tmp_path) as store:
        save_instance(store, inst)
        save_instance(store, inst)  # idempotent
        rows = store.list_source_instances()
        assert len(rows) == 1
        loaded = load_instance(store, "acme-wd")
        assert loaded is not None
        assert loaded.tenant == "acme" and loaded.site == "External"
        assert loaded.adapter_key == SourceFamily.WORKDAY
        assert Capability.POSTED_DATE in loaded.capability_removals
        assert loaded.auth_ref == "ACME_WD_TOKEN"


def test_two_portal_instances_same_category_coexist_in_store(tmp_path):
    li = SourceInstance(instance_id="li", source_type=SourceType.PORTAL_LARGE, source_family=SourceFamily.LINKEDIN)
    nk = SourceInstance(instance_id="nk", source_type=SourceType.PORTAL_LARGE, source_family=SourceFamily.NAUKRI)
    with _store(tmp_path) as store:
        save_instance(store, li)
        save_instance(store, nk)
        loaded = load_instances(store)
        keys = {i.adapter_key for i in loaded}
        assert {SourceFamily.LINKEDIN, SourceFamily.NAUKRI} <= keys


def test_company_source_relationship_references_instance(tmp_path):
    inst = SourceInstance(
        instance_id="acme-wd", source_type=SourceType.ATS_WORKDAY, tenant="acme", company_id="co-acme"
    )
    with _store(tmp_path) as store:
        save_instance(store, inst)
        instances = load_instances(store, company_id="co-acme")
        assert [i.instance_id for i in instances] == ["acme-wd"]


def test_auth_ref_stored_is_a_reference_not_a_secret(tmp_path):
    inst = SourceInstance(instance_id="x", source_type=SourceType.ATS_LEVER, auth_ref="LEVER_API_KEY_REF")
    with _store(tmp_path) as store:
        save_instance(store, inst)
        row = store.get_source_instance("x")
        # A reference name only — no bearer/token value.
        assert row["auth_ref"] == "LEVER_API_KEY_REF"
        assert "bearer" not in (row["auth_ref"] or "").lower()


def test_health_history_keyed_by_signature_avoids_false_drift(tmp_path):
    """A zero for REACT_FRONTEND must not be compared against JAVA_BACKEND
    history (which had strong yields) and flagged as selector drift."""
    java_sig = QuerySignature.build(
        source_instance="li", adapter_key=SourceFamily.LINKEDIN, lane="JAVA_BACKEND", geography_group="PRIMARY"
    ).fingerprint()
    react_sig = QuerySignature.build(
        source_instance="li", adapter_key=SourceFamily.LINKEDIN, lane="REACT_FRONTEND", geography_group="EXPANSION"
    ).fingerprint()
    with _store(tmp_path) as store:
        for i, y in enumerate((42, 38, 47)):
            store.record_source_health(f"j{i}", "li", "HEALTHY", result_count=y, query_signature=java_sig, lane="JAVA_BACKEND")
        # New zero for the *react* signature — its own history is empty.
        react_history = store.recent_yields_for_signature(react_sig)
        assert react_history == []  # not polluted by java history
        verdict = classify_health(
            HealthEvidence(result_count=0), historical_yields=react_history
        )
        # No history for this signature => a zero is NOT false-drift.
        assert verdict.state == SourceHealthState.HEALTHY

        # But a zero on the *java* signature (strong history) IS suspicious.
        java_history = store.recent_yields_for_signature(java_sig)
        assert sorted(java_history) == [38, 42, 47]
        java_verdict = classify_health(HealthEvidence(result_count=0), historical_yields=java_history)
        assert java_verdict.state == SourceHealthState.SELECTOR_DRIFT_SUSPECTED


def test_coverage_attempts_are_append_only(tmp_path):
    with _store(tmp_path) as store:
        store.append_coverage_attempt("a1", "c1", "run-1", "li", "ATTEMPTED_ZERO", jobs_found=0)
        store.append_coverage_attempt("a2", "c1", "run-1", "li", "COMPLETED_WITH_RESULTS", jobs_found=5)
        store.append_coverage_attempt("a1", "c1", "run-1", "li", "ATTEMPTED_ZERO")  # idempotent, ignored
        attempts = store.list_coverage_attempts("c1")
        assert len(attempts) == 2  # both distinct attempts retained, no overwrite
        assert {a["status"] for a in attempts} == {"ATTEMPTED_ZERO", "COMPLETED_WITH_RESULTS"}


def test_coverage_plan_lifecycle_persists(tmp_path):
    with _store(tmp_path) as store:
        store.upsert_coverage_plan("run-1", "BUILDING")
        store.upsert_coverage_plan("run-1", "SEALED", fingerprint="fp", policy_fingerprint="pol", sealed_at="2026-01-01T00:00:00Z")
        row = store.get_coverage_plan("run-1")
        assert row["state"] == "SEALED"
        assert row["fingerprint"] == "fp"
        assert row["policy_fingerprint"] == "pol"
