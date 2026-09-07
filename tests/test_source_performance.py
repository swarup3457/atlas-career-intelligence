"""Phase 1A: deterministic offline performance smoke (reports timings).

Not a promise of performance — just a guard that the framework scales
linearly and has no pathological hot spot. Bounds are deliberately loose.
"""

from __future__ import annotations

import time

import pytest

from atlas.persistence.sqlite import StateStore
from atlas.sources.models import ActiveState, DiscoveryResult, SourceInstance, SourceType
from atlas.sources.provenance import ingest_discovery_results
from atlas.sources.registry import SourceRegistry
from atlas.sources.testing.fake import FakeAdapter

pytestmark = [pytest.mark.integration, pytest.mark.slow]


def _dr(i: int) -> DiscoveryResult:
    return DiscoveryResult(
        source_type=SourceType.FAKE, source_instance=f"inst-{i % 100}", source_job_id=f"J{i}",
        company=f"Company {i}", title="Engineer", location="Remote, India",
        is_active=ActiveState.ACTIVE, adapter_version="1.0", parser_version="1.0",
        source_url=f"https://fake/{i}",
    )


def test_registry_create_100_instances_fast():
    reg = SourceRegistry()
    reg.register(FakeAdapter)
    instances = [SourceInstance(instance_id=f"i{i}", source_type=SourceType.FAKE) for i in range(100)]
    start = time.monotonic()
    adapters = reg.create_all(instances)
    elapsed = time.monotonic() - start
    print(f"[perf] registry.create_all(100) = {elapsed:.4f}s")
    assert len(adapters) == 100
    assert elapsed < 2.0


def test_normalize_10000_results_fast():
    start = time.monotonic()
    total = 0
    for i in range(10000):
        total += len(_dr(i).content_hash())
    elapsed = time.monotonic() - start
    print(f"[perf] content_hash x10000 = {elapsed:.3f}s")
    assert total > 0
    assert elapsed < 10.0


def test_canonicalize_1000_results(tmp_path):
    results = [_dr(i) for i in range(1000)]
    with StateStore(tmp_path / "s.sqlite") as store:
        start = time.monotonic()
        summary = ingest_discovery_results(store, results)
        elapsed = time.monotonic() - start
        print(f"[perf] ingest 1000 discovery results = {elapsed:.3f}s "
              f"(canonical={summary.canonical_created}, obs={summary.observations_added})")
        assert summary.canonical_created == 1000
        assert elapsed < 20.0
