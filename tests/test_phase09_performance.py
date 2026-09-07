"""Phase 0.9 — expansion / performance tests (500 / 5,000 / 20,000 rows).

Generates deterministic large workbooks from the fixture and measures real
ingest timings and throughput. Marked ``slow`` (still runs by default; the
20k case is the heaviest). Offline, tmp_path only, never mutates the
original.
"""

from __future__ import annotations

import pytest

from atlas.data_integrity import adversarial as adv
from atlas.data_integrity.mapping import default_mapping
from atlas.data_integrity.pipeline import run_expansion

pytestmark = [pytest.mark.integration, pytest.mark.slow]


@pytest.fixture
def base_rows(real_fixture):
    return adv.extract_sheet_rows(real_fixture, "All_Jobs")


@pytest.mark.parametrize("n", [500, 5000, 20000])
def test_expansion_ingests_all_rows_with_timing(n, base_rows, tmp_path, capsys):
    m = default_mapping()
    perf = run_expansion(n, base_rows, tmp_path, mapping=m)
    assert perf["records_ingested"] == n
    assert perf["passed"] is True
    assert perf["ingest_seconds"] > 0.0  # a real, measured timing
    assert perf["rows_per_second"] > 0.0
    with capsys.disabled():
        print(
            f"\n[phase09-perf] rows={n:>6} "
            f"ingest={perf['ingest_seconds']:.3f}s "
            f"throughput={perf['rows_per_second']:.0f} rows/s "
            f"bytes={perf['file_bytes']}"
        )


def test_expansion_scaling_is_monotonic(base_rows, tmp_path):
    m = default_mapping()
    t500 = run_expansion(500, base_rows, tmp_path, mapping=m)["ingest_seconds"]
    t5000 = run_expansion(5000, base_rows, tmp_path, mapping=m)["ingest_seconds"]
    # 10x the rows should cost materially more than the 500-row baseline
    assert t5000 > t500


def test_expansion_never_mutates_original(real_fixture, base_rows, tmp_path):
    before = adv.sha256_file(real_fixture)
    run_expansion(500, base_rows, tmp_path, mapping=default_mapping())
    assert adv.sha256_file(real_fixture) == before
