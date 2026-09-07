"""Pytest coverage for the abstract source adapter contract (Phase 0.5
spec section 15). No concrete Workday/Greenhouse/Lever/LinkedIn/Naukri
adapter is implemented - only the contract itself is tested."""

from __future__ import annotations

import pytest

from atlas.sources.base import BaseSource, SourceHealth

pytestmark = pytest.mark.unit


def test_base_source_cannot_be_instantiated_directly():
    with pytest.raises(TypeError):
        BaseSource()  # type: ignore[abstract]


def test_concrete_source_must_implement_all_four_capabilities():
    class IncompleteSource(BaseSource):
        source_type = "incomplete"

        def discover(self, company: str):
            return []

    with pytest.raises(TypeError):
        IncompleteSource()  # type: ignore[abstract]


def test_concrete_source_with_all_capabilities_can_be_instantiated():
    class StubSource(BaseSource):
        source_type = "stub"
        name = "stub-source"

        def discover(self, company: str):
            return [{"url": f"https://example.com/{company}", "label": "stub"}]

        def search(self, company: str, keywords: str):
            return []

        def fetch_detail(self, job_url: str):
            return {}

        def health_check(self) -> SourceHealth:
            return SourceHealth(healthy=True, detail="stub always healthy")

    source = StubSource()
    assert source.discover("Acme") == [{"url": "https://example.com/Acme", "label": "stub"}]
    assert source.search("Acme", "engineer") == []
    assert source.fetch_detail("https://example.com/job/1") == {}
    health = source.health_check()
    assert health.healthy is True
