"""Phase 1A.5: company career-source discovery pipeline tests."""

from __future__ import annotations

import pytest

from atlas.company.discovery import (
    build_source_instance,
    discover_career_endpoints,
    mark_source_replaced,
    register_employer,
    run_fingerprint_pipeline,
)
from atlas.company.models import CompanyObservation, DiscoveryMethod, RelationshipState, SourceConfidence
from atlas.company.registry import CompanyRegistry
from atlas.persistence.sqlite import StateStore
from atlas.sources.models import SourceType

pytestmark = pytest.mark.unit

_C = DiscoveryMethod.CONFIRMED_IDENTITY


@pytest.fixture
def registry(tmp_path):
    with StateStore(tmp_path / "s.sqlite") as store:
        yield CompanyRegistry(store)


def test_greenhouse_discovery(registry):
    r = register_employer(registry, CompanyObservation(
        name="Acme", official_domain="acme.com",
        careers_url="https://boards.greenhouse.io/acme", method=_C))
    assert r.source_registered is True
    assert r.relationship.source_type == SourceType.ATS_GREENHOUSE.value
    assert r.relationship.tenant == "acme"


def test_workday_via_redirect(registry):
    r = register_employer(registry, CompanyObservation(
        name="Globex", official_domain="globex.com",
        careers_url="https://globex.com/careers",
        redirect_url="https://globex.wd1.myworkdayjobs.com/External",
        method=DiscoveryMethod.VALIDATED_REDIRECT))
    assert r.relationship.source_type == SourceType.ATS_WORKDAY.value
    assert r.relationship.tenant == "globex"


def test_custom_careers_page_is_company_career(registry):
    r = register_employer(registry, CompanyObservation(
        name="Umbrella", official_domain="umbrella.co",
        careers_url="https://careers.umbrella.co/openings", method=_C))
    assert r.relationship.source_type == SourceType.COMPANY_CAREER.value


def test_multiple_ats_both_current(registry):
    register_employer(registry, CompanyObservation(
        name="MultiCo", official_domain="multico.com",
        careers_url="https://multico.wd1.myworkdayjobs.com/Global", method=_C))
    r2 = register_employer(registry, CompanyObservation(
        name="MultiCo", official_domain="multico.com",
        careers_url="https://boards.greenhouse.io/multicoindia", method=_C))
    rels = registry.list_relationships(r2.company.company_id)
    assert len(rels) == 2
    assert all(rel.is_current for rel in rels)


def test_ats_migration_preserves_history(registry):
    old = register_employer(registry, CompanyObservation(
        name="OldCo", official_domain="oldco.com",
        careers_url="https://oldco.taleo.net/careersection/x", method=_C))
    new = register_employer(registry, CompanyObservation(
        name="OldCo", official_domain="oldco.com",
        careers_url="https://oldco.wd1.myworkdayjobs.com/New", method=_C))
    assert mark_source_replaced(registry, old.company.company_id, old.source_instance.instance_id) is True
    rels = {r.instance_id: r for r in registry.list_relationships(old.company.company_id)}
    assert len(rels) == 2  # history retained, not destroyed
    assert rels[old.source_instance.instance_id].state == RelationshipState.REPLACED
    assert rels[new.source_instance.instance_id].state == RelationshipState.DISCOVERED


def test_domain_safety_untrusted_posting_registers_no_source(registry):
    r = register_employer(registry, CompanyObservation(
        name="Phish Inc", careers_url="https://phish.example/register",
        method=DiscoveryMethod.UNTRUSTED_POSTING))
    assert r.source_registered is False
    assert r.rejected_untrusted is True
    assert r.company.official_domain is None  # never registered from posting text
    assert registry.store.count_source_relationships() == 0


def test_idempotent_rediscovery(registry):
    obs = CompanyObservation(name="Acme", official_domain="acme.com",
                             careers_url="https://boards.greenhouse.io/acme", method=_C)
    register_employer(registry, obs)
    n_companies = registry.store.count_companies()
    n_rels = registry.store.count_source_relationships()
    register_employer(registry, obs)
    assert registry.store.count_companies() == n_companies
    assert registry.store.count_source_relationships() == n_rels


def test_factory_builds_disabled_instance_with_family_caps():
    inst = build_source_instance("co-1", SourceType.ATS_GREENHOUSE, "https://boards.greenhouse.io/acme", "acme")
    assert inst.enabled is False  # no runnable adapter yet
    assert inst.tenant == "acme"
    assert len(inst.capability_overrides) >= 1


def test_fingerprint_pipeline_confidence():
    outcome = run_fingerprint_pipeline("co-1", DiscoveryMethod.FINGERPRINT,
                                       careers_url="https://boards.greenhouse.io/acme")
    assert outcome.source_type == SourceType.ATS_GREENHOUSE
    assert outcome.observation.confidence in (SourceConfidence.STRONG, SourceConfidence.TENTATIVE)


def test_career_endpoint_discovery_contract():
    resolved = discover_career_endpoints("Acme", known_domain="acme.com")
    assert resolved.status == "RESOLVED"
    assert all(c.url.startswith("https://acme.com/") for c in resolved.candidates)
    unresolved = discover_career_endpoints("Mystery")
    assert unresolved.status == "UNRESOLVED"
