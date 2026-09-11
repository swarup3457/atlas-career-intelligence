"""Discovery front-half tests (PRODUCTION R1 §9–§10): evidence + batch planning."""

from __future__ import annotations

import json
from pathlib import Path

from atlas.discovery.models import DiscoveryBatch, JobLead
from atlas.discovery.service import DiscoveryService, plan_verification_batches, write_discovery_evidence


def _lead(index: int) -> JobLead:
    return JobLead(
        "freehire", "api", "java india", "Java Developer", "Co", "Bengaluru, India",
        "2026-09-01", f"https://jobs/{index}", canonical_url=f"https://jobs/{index}",
        skills=("java", "spring"), provider_id=f"p{index}",
    )


class _FakeProvider:
    name = "fake"

    def discover(self, queries: list[str]) -> DiscoveryBatch:
        return DiscoveryBatch("fake", "OK", tuple(_lead(i) for i in range(20)), tuple(queries))


def test_write_evidence_and_plan_batches(tmp_path: Path) -> None:
    result = DiscoveryService([_FakeProvider()]).run(["java india"])
    assert len(result.queued_leads) == 20

    evidence = write_discovery_evidence(tmp_path / "evidence", result, ["java india"])
    raw = json.loads((evidence / "freehire_raw.json").read_text(encoding="utf-8"))
    queued = json.loads((evidence / "freehire_queued.json").read_text(encoding="utf-8"))
    assert len(raw) == 20 and len(queued) == 20
    assert json.loads((evidence / "discovery_queries.json").read_text(encoding="utf-8")) == {"queries": ["java india"]}

    batches = plan_verification_batches(result.queued_leads, max_leads=8)
    assert [len(b) for b in batches] == [8, 8, 4]  # never exceeds max, nothing dropped
    assert sum(len(b) for b in batches) == 20
