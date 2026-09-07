"""Phase 1A: dynamic source registry tests."""

from __future__ import annotations

import pytest

from atlas.sources.adapter import SourceAdapter
from atlas.sources.health import SourceHealth, SourceHealthState
from atlas.sources.models import Capability, SearchRequest, SearchResult, SourceInstance, SourceType
from atlas.sources.registry import DuplicateRegistrationError, SourceRegistry, UnknownSourceTypeError

pytestmark = pytest.mark.unit


class _AdapterA(SourceAdapter):
    source_type = SourceType.FAKE
    CAPABILITIES = frozenset({Capability.SEARCH})
    adapter_version = "1.0.0"
    parser_version = "1.0.0"

    def health_check(self) -> SourceHealth:
        return SourceHealth(SourceHealthState.HEALTHY)

    def search(self, request: SearchRequest) -> SearchResult:
        return SearchResult()


class _AdapterB(SourceAdapter):
    source_type = SourceType.FIXTURE
    CAPABILITIES = frozenset({Capability.SEARCH, Capability.DETAIL})
    adapter_version = "1.0.0"
    parser_version = "1.0.0"

    def health_check(self) -> SourceHealth:
        return SourceHealth(SourceHealthState.HEALTHY)


def _reg() -> SourceRegistry:
    r = SourceRegistry()
    r.register(_AdapterA)
    r.register(_AdapterB)
    return r


def test_register_and_create():
    reg = _reg()
    inst = SourceInstance(instance_id="fk", source_type=SourceType.FAKE)
    adapter = reg.create(inst)
    assert isinstance(adapter, _AdapterA)
    assert adapter.instance_id == "fk"


def test_duplicate_registration_rejected():
    reg = SourceRegistry()
    reg.register(_AdapterA)

    class _Other(_AdapterA):
        pass

    with pytest.raises(DuplicateRegistrationError):
        reg.register(_Other)  # same source_type FAKE


def test_reregistering_same_class_is_idempotent():
    reg = SourceRegistry()
    reg.register(_AdapterA)
    reg.register(_AdapterA)  # no error
    assert reg.registered_types() == [SourceType.FAKE]


def test_unknown_source_type_raises():
    reg = SourceRegistry()
    with pytest.raises(UnknownSourceTypeError):
        reg.adapter_for(SourceType.ATS_LEVER)


def test_create_all_filters_disabled_and_sorts():
    reg = _reg()
    instances = [
        SourceInstance(instance_id="b", source_type=SourceType.FIXTURE),
        SourceInstance(instance_id="a", source_type=SourceType.FAKE),
        SourceInstance(instance_id="c", source_type=SourceType.FAKE, enabled=False),
    ]
    adapters = reg.create_all(instances)
    ids = [a.instance_id for a in adapters]
    assert "c" not in ids  # disabled filtered out
    assert ids == sorted(ids)  # deterministic order


def test_with_capability_query():
    reg = _reg()
    adapters = reg.create_all(
        [
            SourceInstance(instance_id="a", source_type=SourceType.FAKE),
            SourceInstance(instance_id="b", source_type=SourceType.FIXTURE),
        ]
    )
    detail_capable = SourceRegistry.with_capability(adapters, Capability.DETAIL)
    assert [a.instance_id for a in detail_capable] == ["b"]


def test_describe_lists_registered():
    reg = _reg()
    desc = reg.describe()
    assert {d["source_type"] for d in desc} == {"FAKE", "FIXTURE"}


def test_register_requires_capabilities():
    class _NoCaps(SourceAdapter):
        source_type = SourceType.SEARCH_FALLBACK
        adapter_version = "1.0.0"
        parser_version = "1.0.0"

        def health_check(self) -> SourceHealth:
            return SourceHealth()

    with pytest.raises(ValueError):
        SourceRegistry().register(_NoCaps)
