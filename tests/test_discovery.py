from __future__ import annotations

from atlas.discovery.canonicalize import deduplicate
from atlas.discovery.models import DiscoveryBatch, JobLead
from atlas.discovery.prefilter import prefilter
from atlas.discovery.service import DiscoveryService


def lead(**overrides):
    data = dict(provider="test", mechanism="fixture-free", query="java", title="Java Backend Engineer", company="Acme", location="Bengaluru, India", posted_date="2026-09-10", source_url="https://jobs.example/1", canonical_url="https://jobs.example/1", description_available=True)
    data.update(overrides)
    return JobLead(**data)


def test_dedup_prefers_description_rich_copy():
    assert len(deduplicate([lead(description_available=False), lead(description_available=True)])) == 1


def test_prefilter_rejects_support_and_foreign():
    assert prefilter(lead(title="Support Engineer", location="Bengaluru, India")).prefilter_status == "REJECTED"
    assert prefilter(lead(location="Toronto, Canada")).prefilter_status == "REJECTED"


def test_provider_failure_isolated():
    class Broken:
        name = "broken"
        def discover(self, queries): raise RuntimeError("down")
    class Good:
        name = "good"
        def discover(self, queries): return DiscoveryBatch("good", "OK", (lead(),))
    result = DiscoveryService([Broken(), Good()]).run(["java"])
    assert len(result.queued_leads) == 1
    assert any("broken" in error for error in result.errors)
