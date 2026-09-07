"""Phase 1A: SourceAdapter V1 typed contract tests."""

from __future__ import annotations

import pytest

from atlas.sources.adapter import CapabilityNotSupported, SourceAdapter
from atlas.sources.health import SourceHealth, SourceHealthState
from atlas.sources.models import (
    Capability,
    DetailRequest,
    SearchRequest,
    SearchResult,
    SourceInstance,
    SourceType,
)

pytestmark = pytest.mark.unit


class _SearchOnly(SourceAdapter):
    source_type = SourceType.FAKE
    CAPABILITIES = frozenset({Capability.SEARCH})
    adapter_version = "1.0.0"
    parser_version = "1.0.0"

    def health_check(self) -> SourceHealth:
        return SourceHealth(SourceHealthState.HEALTHY)

    def search(self, request: SearchRequest) -> SearchResult:
        return SearchResult()


def _instance(stype=SourceType.FAKE) -> SourceInstance:
    return SourceInstance(instance_id="x", source_type=stype)


def test_unsupported_operation_raises_capability_not_supported():
    adapter = _SearchOnly(_instance())
    with pytest.raises(CapabilityNotSupported):
        adapter.fetch_detail(DetailRequest(url="https://x/y"))


def test_supports_and_capabilities_include_overrides():
    inst = SourceInstance(
        instance_id="x", source_type=SourceType.FAKE,
        capability_overrides=frozenset({Capability.DETAIL}),
    )
    adapter = _SearchOnly(inst)
    assert adapter.supports(Capability.SEARCH)
    assert adapter.supports(Capability.DETAIL)  # via override


def test_instance_source_type_mismatch_rejected():
    with pytest.raises(ValueError):
        _SearchOnly(_instance(SourceType.ATS_WORKDAY))


def test_missing_versions_rejected():
    class _NoVersion(_SearchOnly):
        adapter_version = ""

    with pytest.raises(ValueError):
        _NoVersion(_instance())


def test_describe_is_deterministic_and_nonsensitive():
    d = _SearchOnly(_instance()).describe()
    assert d["instance_id"] == "x"
    assert d["adapter_version"] == "1.0.0"
    assert d["capabilities"] == ["SEARCH"]
    assert "auth" not in str(d).lower()
