"""Phase 0.9 — core data-integrity unit tests (offline, NullController).

Covers normalizers (determinism + idempotency), the configurable mapping /
alias layer, ingestion of the real fixture, the validation finding model,
candidate identity relationships, and auditable status transitions. Every
test is fully offline and touches only tmp_path or the read-only fixture.
"""

from __future__ import annotations

import datetime

import pytest

from atlas.controllers.base import NullController
from atlas.data_integrity import normalizers as N
from atlas.data_integrity.identity import IdentityResolver, Relationship, compute_identity_key
from atlas.data_integrity.ingestion import ingest_workbook
from atlas.data_integrity.mapping import MappingConfig, default_mapping
from atlas.data_integrity.statuses import (
    CanonicalStatus,
    RecordStatus,
    StatusTransitionError,
    apply_transition,
    can_transition,
)
from atlas.data_integrity.validation import Action, Severity, Validator

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Normalizers: deterministic + idempotent
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "name,value",
    [
        ("whitespace", "  a\t b\n c  "),
        ("text", "  Hello  World  "),
        ("company", "  Acme   Corp  "),
        ("lower", "MixedCase"),
        ("url", "HTTPS://Example.COM/Path/?q=1#frag"),
        ("domain", "https://WWW.Acme.com/careers"),
        ("date", "13/08/2026"),
        ("date", datetime.datetime(2026, 8, 13, 0, 0, 0)),
        ("int", "42"),
        ("score", "86%"),
        ("bool", "Yes"),
        ("token", "  JPMorgan  Chase!! "),
    ],
)
def test_normalizers_are_idempotent(name, value):
    once = N.apply_normalizer(name, value)
    twice = N.apply_normalizer(name, once)
    assert once == twice


def test_normalizers_are_deterministic():
    for _ in range(5):
        assert N.normalize_url("HTTPS://Example.COM/p/#x") == "https://example.com/p"
        assert N.normalize_domain("https://WWW.Acme.com/careers") == "acme.com"
        assert N.identity_token("  Café  Ltd!! ") == "cafe ltd"
        assert N.normalize_date("Aug 13, 2026") == "2026-08-13"
        assert N.normalize_score("86%") == 86
        assert N.normalize_bool("TRUE") is True


def test_header_key_folds_case_punctuation_and_underscores():
    assert N.header_key("Company Name") == N.header_key("company_name")
    assert N.header_key("COMPANY-NAME") == N.header_key("company name")


# ---------------------------------------------------------------------------
# Mapping / alias resolution
# ---------------------------------------------------------------------------
def test_mapping_resolves_sheets_and_column_aliases():
    m = default_mapping()
    schema = m.resolve_sheet("All_Jobs")
    assert schema is not None and schema.entity_type == "job"
    # canonical + alias both resolve to the same field
    assert m.resolve_column(schema, "Company") == "company"
    assert m.resolve_column(schema, "Company_Name") == "company"
    assert m.resolve_column(schema, "Match Score") == "match_score"
    assert m.resolve_column(schema, "Totally Unknown Column") is None


def test_mapping_is_configurable_from_dict():
    cfg = MappingConfig.from_dict(
        {
            "version": "9",
            "entities": [
                {
                    "entity_type": "widget",
                    "sheets": ["Widgets"],
                    "identity": ["sku"],
                    "fields": [
                        {"canonical": "sku", "aliases": ["SKU", "Sku_Id"], "required": True},
                        {"canonical": "price", "normalizer": "int", "dtype": "int"},
                    ],
                }
            ],
        }
    )
    schema = cfg.resolve_sheet("widgets")
    assert schema.entity_type == "widget"
    assert cfg.resolve_column(schema, "Sku Id") == "sku"
    assert cfg.version == "9"


# ---------------------------------------------------------------------------
# Ingestion on the real fixture
# ---------------------------------------------------------------------------
def test_ingest_fixture_resolves_all_sheets(real_fixture):
    m = default_mapping()
    result = ingest_workbook(real_fixture, mapping=m, clock=lambda: "T")
    assert result.load_error is None
    assert {s.sheet_name for s in result.sheets} == {
        "All_Jobs", "New_Companies", "Company_Coverage", "Source_Coverage",
        "Closed_or_Rejected", "Resume_Tailoring", "Recruiter_Contacts", "Run_Summary",
    }
    assert all(s.resolved for s in result.sheets)
    by_sheet = {s.sheet_name: s for s in result.sheets}
    assert by_sheet["All_Jobs"].data_row_count == 25
    assert by_sheet["All_Jobs"].record_count == 25
    # 82 total records across the workbook
    assert len(result.records) == 82


def test_ingest_preserves_raw_normalized_and_provenance(real_fixture):
    m = default_mapping()
    result = ingest_workbook(real_fixture, mapping=m, clock=lambda: "T")
    job = next(r for r in result.records if r.entity_type == "job")
    assert job.provenance.sheet_name in ("All_Jobs", "Closed_or_Rejected")
    assert job.provenance.row_index >= 2
    # raw and normalized are both retained
    fv = job.fields["company"]
    assert fv.raw is not None
    assert fv.normalized is not None
    assert fv.normalizer == "company"


def test_ingest_captures_unknown_columns(real_fixture):
    m = default_mapping()
    result = ingest_workbook(real_fixture, mapping=m, clock=lambda: "T")
    run = next(s for s in result.sheets if s.sheet_name == "Run_Summary")
    # Run_Summary has many columns we intentionally do not map -> unknown
    assert len(run.unknown_columns) > 0


# ---------------------------------------------------------------------------
# Validation finding model
# ---------------------------------------------------------------------------
def test_validation_on_fixture_flags_but_does_not_quarantine(real_fixture):
    m = default_mapping()
    result = ingest_workbook(real_fixture, mapping=m, clock=lambda: "T")
    report = Validator(m).validate(result.records)
    sev = report.counts_by_severity()
    # clean real data: nothing quarantined/rejected, no ERROR/CRITICAL
    assert report.quarantined == []
    assert report.rejected == []
    assert sev["ERROR"] == 0 and sev["CRITICAL"] == 0
    # but the messy URL/enum columns are surfaced as warnings
    codes = report.counts_by_code()
    assert "MALFORMED_URL" in codes
    assert "UNKNOWN_ENUM_VALUE" in codes


def test_validation_ordering_is_deterministic(real_fixture):
    m = default_mapping()
    result = ingest_workbook(real_fixture, mapping=m, clock=lambda: "T")
    a = [f.to_dict() for f in Validator(m).validate(result.records).findings]
    b = [f.to_dict() for f in Validator(m).validate(result.records).findings]
    assert a == b


def test_action_and_severity_ordering():
    assert Action.REJECT > Action.QUARANTINE > Action.FLAG > Action.ACCEPT
    assert Severity.CRITICAL > Severity.ERROR > Severity.WARNING > Severity.INFO


# ---------------------------------------------------------------------------
# Identity relationships
# ---------------------------------------------------------------------------
def test_identity_key_folds_case_and_whitespace():
    m = default_mapping()
    result = ingest_workbook  # not used; direct record construction below
    from atlas.data_integrity.records import FieldValue, IngestionRecord, Provenance

    def make(company, job_id):
        prov = Provenance("f", "All_Jobs", "job", 2, "T")
        rec = IngestionRecord("r", "job", prov)
        rec.fields["company"] = FieldValue("company", company, N.normalize_company(company), "company")
        rec.fields["job_id"] = FieldValue("job_id", job_id, N.normalize_text(job_id), "text")
        return rec

    k1 = compute_identity_key(make("Acme Corp", "J1"), m)
    k2 = compute_identity_key(make("  ACME   corp ", "J1"), m)
    assert k1 == k2


def test_identity_resolution_on_fixture_has_conflict(real_fixture):
    m = default_mapping()
    result = ingest_workbook(real_fixture, mapping=m, clock=lambda: "T")
    resolution = IdentityResolver(m).resolve(result.records)
    assert resolution.unidentified == []
    counts = resolution.relationship_counts()
    # the real data contains a genuine same-id/different-role conflict
    assert counts[Relationship.CONFLICT.value] >= 1


# ---------------------------------------------------------------------------
# Statuses & transitions
# ---------------------------------------------------------------------------
def test_legal_and_illegal_record_transitions():
    assert can_transition(RecordStatus.INGESTED, RecordStatus.NORMALIZED)
    assert not can_transition(RecordStatus.INGESTED, RecordStatus.CANONICAL)
    t = apply_transition("r1", RecordStatus.INGESTED, RecordStatus.NORMALIZED, "ok")
    assert t.to_status == "NORMALIZED"
    with pytest.raises(StatusTransitionError):
        apply_transition("r1", RecordStatus.INGESTED, RecordStatus.CANONICAL, "bad")


def test_canonical_repost_and_closure_transitions_are_legal():
    assert can_transition(CanonicalStatus.ACTIVE, CanonicalStatus.CLOSED, axis="canonical")
    assert can_transition(CanonicalStatus.CLOSED, CanonicalStatus.ACTIVE, axis="canonical")
    assert not can_transition(CanonicalStatus.SUPERSEDED, CanonicalStatus.CLOSED, axis="canonical")


def test_null_controller_is_offline_noop():
    # The whole subsystem runs with controller=none; prove it is inert.
    from atlas.controllers.base import ControllerRequest

    c = NullController()
    resp = c.classify(ControllerRequest(prompt="anything"))
    assert resp.metadata["noop"] is True
