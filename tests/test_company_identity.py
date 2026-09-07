"""Phase 1A.5: company identity normalization + id derivation tests."""

from __future__ import annotations

import pytest

from atlas.company.identity import (
    canonical_company_name,
    company_identity_key,
    confidence_for_method,
    confidence_from_fingerprint,
    derive_company_id,
    derive_instance_id,
    normalize_domain,
)
from atlas.company.models import DiscoveryMethod, SourceConfidence

pytestmark = pytest.mark.unit


def test_legal_suffix_stripping_matches_variants():
    assert company_identity_key("Acme") == company_identity_key("Acme Inc.")
    assert company_identity_key("Acme") == company_identity_key("Acme Corporation")
    assert company_identity_key("JPMorgan Chase") == company_identity_key("JPMorgan Chase & Co.")


def test_abbreviation_expansion_not_auto_matched():
    # No hard-coded alias DB: IBM vs the expansion are NOT the same key.
    assert company_identity_key("IBM") != company_identity_key("International Business Machines")


def test_unicode_accents_folded_in_identity_key():
    assert company_identity_key("Café Solutions") == "cafe solutions"


def test_canonical_name_preserves_case():
    assert canonical_company_name("  Acme   Corp ") == "Acme Corp"


def test_normalize_domain():
    assert normalize_domain("https://www.Acme.com/careers") == "acme.com"
    assert normalize_domain("ACME.COM") == "acme.com"
    assert normalize_domain("sub.acme.co.uk") == "sub.acme.co.uk"
    assert normalize_domain("Acme") is None  # a bare name is not a domain
    assert normalize_domain(None) is None


def test_company_id_deterministic_and_domain_anchored():
    a = derive_company_id("acme", "acme.com")
    b = derive_company_id("acme corp", "acme.com")  # different name, same domain
    assert a == b  # domain anchors the id (display-name change is safe)
    c = derive_company_id("acme", None)
    assert c != a  # name-based differs from domain-based


def test_instance_id_deterministic_by_tenant():
    i1 = derive_instance_id("co-1", "ATS_WORKDAY", tenant="acme")
    i2 = derive_instance_id("co-1", "ATS_WORKDAY", tenant="acme")
    i3 = derive_instance_id("co-1", "ATS_WORKDAY", tenant="other")
    assert i1 == i2 and i1 != i3


def test_confidence_from_fingerprint_buckets():
    assert confidence_from_fingerprint(0.95) == SourceConfidence.STRONG
    assert confidence_from_fingerprint(0.85) == SourceConfidence.TENTATIVE
    assert confidence_from_fingerprint(0.70) == SourceConfidence.TENTATIVE
    assert confidence_from_fingerprint(0.50) == SourceConfidence.UNKNOWN


def test_confidence_for_method():
    assert confidence_for_method(DiscoveryMethod.CONFIRMED_IDENTITY) == SourceConfidence.CONFIRMED
    assert confidence_for_method(DiscoveryMethod.VALIDATED_REDIRECT) == SourceConfidence.STRONG
    assert confidence_for_method(DiscoveryMethod.UNTRUSTED_POSTING) == SourceConfidence.UNKNOWN
