"""Phase 1A.5: CompanyRegistry + merge-safety tests."""

from __future__ import annotations

import pytest

from atlas.company.models import CompanyObservation, DiscoveryMethod, RelationshipState, SourceConfidence
from atlas.company.registry import CompanyRegistry
from atlas.persistence.sqlite import StateStore

pytestmark = pytest.mark.unit

_C = DiscoveryMethod.CONFIRMED_IDENTITY


@pytest.fixture
def registry(tmp_path):
    with StateStore(tmp_path / "s.sqlite") as store:
        yield CompanyRegistry(store)


def test_register_and_idempotent_same_domain(registry):
    a = registry.register_company(CompanyObservation(name="Acme Corp", official_domain="acme.com", method=_C))
    b = registry.register_company(CompanyObservation(name="Acme Inc.", official_domain="acme.com", method=_C))
    assert a.company_id == b.company_id
    assert registry.store.count_companies() == 1


def test_domain_variant_same_company(registry):
    a = registry.register_company(CompanyObservation(name="Acme", official_domain="acme.com", method=_C))
    b = registry.register_company(CompanyObservation(name="Acme", official_domain="https://www.acme.com/careers", method=_C))
    assert a.company_id == b.company_id


def test_different_domain_not_merged(registry):
    a = registry.register_company(CompanyObservation(name="Delta", official_domain="delta-air.com", method=_C))
    b = registry.register_company(CompanyObservation(name="Delta", official_domain="delta-faucet.com", method=_C))
    assert a.company_id != b.company_id
    assert registry.store.count_companies() == 2


def test_explicit_alias_resolves(registry):
    ibm = registry.register_company(CompanyObservation(name="IBM", official_domain="ibm.com", method=_C))
    registry.add_alias(ibm.company_id, "International Business Machines")
    found = registry.find_company("International Business Machines")
    assert found is not None and found.company_id == ibm.company_id


def test_display_name_change_is_stable(registry):
    a = registry.register_company(CompanyObservation(name="Globex", official_domain="globex.com", method=_C))
    b = registry.register_company(CompanyObservation(name="Globex Worldwide Ltd", official_domain="globex.com", method=_C))
    assert a.company_id == b.company_id
    company = registry.get_company(a.company_id)
    # the variant name is retained as an alias, never a second company
    assert any("Globex Worldwide" in al for al in company.aliases)


def test_ambiguous_no_domain_is_unresolved(registry):
    registry.register_company(CompanyObservation(name="Zeta", official_domain="zeta.com", method=_C))
    registry.register_company(CompanyObservation(name="Zeta", official_domain="zeta.org", method=_C))
    match = registry.resolve("Zeta")  # no domain -> which one?
    assert match.ambiguous is True
    assert match.company_id is None


def test_unicode_company_name(registry):
    c = registry.register_company(CompanyObservation(name="Café Solutions", official_domain="cafe.example", method=_C))
    assert registry.get_company(c.company_id).identity_key == "cafe solutions"


def test_attach_and_list_relationships(registry):
    c = registry.register_company(CompanyObservation(name="Acme", official_domain="acme.com", method=_C))
    rel = registry.attach_source_instance(
        c.company_id, "acme--ats_workday--acme", "ATS_WORKDAY", tenant="acme",
        state=RelationshipState.DISCOVERED, confidence=SourceConfidence.STRONG,
    )
    rels = registry.list_relationships(c.company_id)
    assert len(rels) == 1 and rels[0].relationship_id == rel.relationship_id
    # idempotent re-attach
    registry.attach_source_instance(c.company_id, "acme--ats_workday--acme", "ATS_WORKDAY", tenant="acme")
    assert len(registry.list_relationships(c.company_id)) == 1


def test_update_verification(registry):
    c = registry.register_company(CompanyObservation(name="Acme", official_domain="acme.com", method=_C))
    registry.update_verification(c.company_id, status="ACTIVE")
    assert registry.get_company(c.company_id).last_verified_at is not None
