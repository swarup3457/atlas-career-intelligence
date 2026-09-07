"""Phase 1A.5: company domain model tests."""

from __future__ import annotations

import pytest

from atlas.company.models import (
    Company,
    CompanyDiscoveryTask,
    CompanyStatus,
    DiscoveryMethod,
    RelationshipState,
    SourceConfidence,
    SourceDiscoveryObservation,
    TRUSTED_DISCOVERY_METHODS,
    VerificationState,
)

pytestmark = pytest.mark.unit


def test_untrusted_posting_is_not_a_trusted_method():
    assert DiscoveryMethod.UNTRUSTED_POSTING not in TRUSTED_DISCOVERY_METHODS
    for m in (DiscoveryMethod.EXPLICIT_CONFIG, DiscoveryMethod.CONFIRMED_IDENTITY,
              DiscoveryMethod.CANDIDATE_URL, DiscoveryMethod.VALIDATED_REDIRECT,
              DiscoveryMethod.FINGERPRINT):
        assert m in TRUSTED_DISCOVERY_METHODS


def test_observation_is_trusted_flag():
    trusted = SourceDiscoveryObservation("o1", "c1", DiscoveryMethod.CONFIRMED_IDENTITY)
    untrusted = SourceDiscoveryObservation("o2", "c1", DiscoveryMethod.UNTRUSTED_POSTING)
    assert trusted.is_trusted is True
    assert untrusted.is_trusted is False


def test_company_to_dict_roundtrip():
    c = Company(company_id="co-1", canonical_name="Acme", identity_key="acme",
                status=CompanyStatus.ACTIVE, aliases=("Acme Inc",))
    d = c.to_dict()
    assert d["company_id"] == "co-1"
    assert d["status"] == "ACTIVE"
    assert d["aliases"] == ["Acme Inc"]


def test_discovery_task_priority_is_neutral_default():
    t = CompanyDiscoveryTask(company_name="Acme")
    assert t.priority == 0
    assert t.attempt_count == 0


def test_state_enums_are_distinct_concepts():
    assert RelationshipState.DISCOVERED.value == "DISCOVERED"
    assert VerificationState.UNVERIFIED.value == "UNVERIFIED"
    assert SourceConfidence.CONFIRMED.value == "CONFIRMED"
