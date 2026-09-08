"""Phase 1B — source architecture corrections (build spec section 7)."""

from __future__ import annotations

import pytest

from atlas.company.tenant import extract_site, extract_tenant
from atlas.sources.adapter import CapabilityNotSupported, SourceAdapter
from atlas.sources.fingerprint import fingerprint_ats
from atlas.sources.health import SourceHealth, SourceHealthState
from atlas.sources.models import (
    Capability,
    DiscoverRequest,
    DiscoverResult,
    SearchRequest,
    SearchResult,
    SourceCategory,
    SourceFamily,
    SourceInstance,
    SourceType,
    category_for_source_type,
    family_for_source_type,
)
from atlas.sources.registry import (
    AmbiguousSourceTypeError,
    DuplicateRegistrationError,
    SourceRegistry,
)

pytestmark = pytest.mark.unit


# --- 7.1 taxonomy: multiple adapters coexist in one broad category --------
class _LinkedInLike(SourceAdapter):
    source_type = SourceType.PORTAL_LARGE
    source_family = SourceFamily.LINKEDIN
    CAPABILITIES = frozenset({Capability.SEARCH})
    adapter_version = "1.0.0"
    parser_version = "1.0.0"

    def health_check(self) -> SourceHealth:
        return SourceHealth(SourceHealthState.HEALTHY)

    def search(self, request: SearchRequest) -> SearchResult:
        return SearchResult()


class _NaukriLike(SourceAdapter):
    source_type = SourceType.PORTAL_LARGE
    source_family = SourceFamily.NAUKRI
    CAPABILITIES = frozenset({Capability.SEARCH})
    adapter_version = "1.0.0"
    parser_version = "1.0.0"

    def health_check(self) -> SourceHealth:
        return SourceHealth(SourceHealthState.HEALTHY)

    def search(self, request: SearchRequest) -> SearchResult:
        return SearchResult()


def test_two_portals_share_a_category_but_coexist():
    reg = SourceRegistry()
    reg.register(_LinkedInLike)
    reg.register(_NaukriLike)
    assert reg.is_registered_family(SourceFamily.LINKEDIN)
    assert reg.is_registered_family(SourceFamily.NAUKRI)
    assert _LinkedInLike.category() == SourceCategory.PORTAL
    assert _NaukriLike.category() == SourceCategory.PORTAL
    # Both are PORTAL_LARGE source_type; resolving by broad type is ambiguous.
    with pytest.raises(AmbiguousSourceTypeError):
        reg.adapter_for(SourceType.PORTAL_LARGE)


def test_adapter_key_is_unique_registry_identity():
    reg = SourceRegistry()
    reg.register(_LinkedInLike)

    class _OtherLinkedIn(_LinkedInLike):
        pass  # same family LINKEDIN

    with pytest.raises(DuplicateRegistrationError):
        reg.register(_OtherLinkedIn)


def test_create_resolves_by_instance_family():
    reg = SourceRegistry()
    reg.register(_LinkedInLike)
    reg.register(_NaukriLike)
    li = SourceInstance(
        instance_id="linkedin-in",
        source_type=SourceType.PORTAL_LARGE,
        source_family=SourceFamily.LINKEDIN,
    )
    nk = SourceInstance(
        instance_id="naukri-in",
        source_type=SourceType.PORTAL_LARGE,
        source_family=SourceFamily.NAUKRI,
    )
    assert isinstance(reg.create(li), _LinkedInLike)
    assert isinstance(reg.create(nk), _NaukriLike)


# --- 7.2 Ashby -------------------------------------------------------------
def test_ashby_is_in_taxonomy():
    assert SourceType.ATS_ASHBY.value == "ATS_ASHBY"
    assert family_for_source_type(SourceType.ATS_ASHBY) == SourceFamily.ASHBY
    assert category_for_source_type(SourceType.ATS_ASHBY) == SourceCategory.ATS


def test_ashby_fingerprint_and_tenant():
    fp = fingerprint_ats("https://jobs.ashbyhq.com/acme")
    assert fp.matched and fp.source_type == SourceType.ATS_ASHBY
    assert extract_tenant(SourceType.ATS_ASHBY, "https://jobs.ashbyhq.com/acme/some-role") == "acme"


# --- 7.3 DISCOVER capability ----------------------------------------------
class _DiscoverableAdapter(SourceAdapter):
    source_type = SourceType.ATS_GREENHOUSE
    CAPABILITIES = frozenset({Capability.DISCOVER, Capability.SEARCH})
    adapter_version = "1.0.0"
    parser_version = "1.0.0"

    def health_check(self) -> SourceHealth:
        return SourceHealth(SourceHealthState.HEALTHY)

    def discover(self, request: DiscoverRequest) -> DiscoverResult:
        return DiscoverResult()

    def search(self, request: SearchRequest) -> SearchResult:
        return SearchResult()


class _SearchOnlyAdapter(SourceAdapter):
    source_type = SourceType.ATS_LEVER
    CAPABILITIES = frozenset({Capability.SEARCH})
    adapter_version = "1.0.0"
    parser_version = "1.0.0"

    def health_check(self) -> SourceHealth:
        return SourceHealth(SourceHealthState.HEALTHY)

    def search(self, request: SearchRequest) -> SearchResult:
        return SearchResult()


def test_discover_capability_is_distinct_from_search():
    disc = _DiscoverableAdapter(
        SourceInstance(instance_id="gh", source_type=SourceType.ATS_GREENHOUSE)
    )
    assert disc.supports(Capability.DISCOVER)
    assert isinstance(disc.discover(DiscoverRequest(company="Acme")), DiscoverResult)

    # A SEARCH-only adapter must NOT be treated as discovery-capable: the
    # default discover() raises for the DISCOVER capability, not SEARCH.
    so = _SearchOnlyAdapter(
        SourceInstance(instance_id="lv", source_type=SourceType.ATS_LEVER)
    )
    assert not so.supports(Capability.DISCOVER)
    with pytest.raises(CapabilityNotSupported) as exc:
        so.discover(DiscoverRequest(company="Acme"))
    assert exc.value.capability == Capability.DISCOVER


# --- 7.5 Workday tenant + site --------------------------------------------
def test_workday_tenant_and_site_are_separate():
    url = "https://acme.wd5.myworkdayjobs.com/en-US/External/job/Bengaluru/Engineer_R123"
    assert extract_tenant(SourceType.ATS_WORKDAY, url) == "acme"
    assert extract_site(SourceType.ATS_WORKDAY, url) == "external"
    cxs = "https://acme.wd5.myworkdayjobs.com/wday/cxs/acme/Careers/jobs"
    assert extract_tenant(SourceType.ATS_WORKDAY, cxs) == "acme"
    assert extract_site(SourceType.ATS_WORKDAY, cxs) == "careers"
    # Non-Workday families have no distinct site.
    assert extract_site(SourceType.ATS_LEVER, "https://jobs.lever.co/acme") is None


def test_source_instance_carries_site():
    inst = SourceInstance(
        instance_id="acme-wd",
        source_type=SourceType.ATS_WORKDAY,
        tenant="acme",
        site="External",
    )
    assert inst.tenant == "acme"
    assert inst.site == "External"
    assert inst.to_dict()["site"] == "External"


# --- 7.6 capability removals ----------------------------------------------
class _FamilyDefaultsAdapter(SourceAdapter):
    source_type = SourceType.ATS_WORKDAY
    CAPABILITIES = frozenset({Capability.SEARCH, Capability.DETAIL, Capability.POSTED_DATE})
    adapter_version = "1.0.0"
    parser_version = "1.0.0"

    def health_check(self) -> SourceHealth:
        return SourceHealth(SourceHealthState.HEALTHY)

    def search(self, request: SearchRequest) -> SearchResult:
        return SearchResult()


def test_capability_removals_drop_a_family_default():
    inst = SourceInstance(
        instance_id="acme-wd",
        source_type=SourceType.ATS_WORKDAY,
        capability_removals=frozenset({Capability.POSTED_DATE}),
        capability_overrides=frozenset({Capability.RECENCY_FILTER}),
    )
    adapter = _FamilyDefaultsAdapter(inst)
    caps = adapter.capabilities()
    assert Capability.POSTED_DATE not in caps  # removed
    assert Capability.RECENCY_FILTER in caps  # added
    assert Capability.SEARCH in caps  # untouched default
    assert not adapter.supports(Capability.POSTED_DATE)
