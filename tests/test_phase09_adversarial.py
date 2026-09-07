"""Phase 0.9 — adversarial generation & scenario tests.

Verifies the A..AU catalog is complete, generation is deterministic and
never mutates the immutable original, malformed workbooks are handled
without crashing, and every non-performance scenario is detected by the
pipeline as expected.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from atlas.data_integrity import adversarial as adv
from atlas.data_integrity.ingestion import ingest_workbook
from atlas.data_integrity.mapping import default_mapping
from atlas.data_integrity.pipeline import run_scenario

pytestmark = [pytest.mark.integration]


@pytest.fixture
def base_rows(real_fixture):
    return adv.extract_sheet_rows(real_fixture, "All_Jobs")


# ---------------------------------------------------------------------------
# Catalog completeness
# ---------------------------------------------------------------------------
def test_catalog_is_exactly_A_through_AU():
    codes = [s.code for s in adv.SCENARIOS]
    expected = [chr(c) for c in range(ord("A"), ord("Z") + 1)] + [
        "A" + chr(c) for c in range(ord("A"), ord("U") + 1)
    ]
    assert codes == expected
    assert len(codes) == 47


# ---------------------------------------------------------------------------
# Never mutate the original + deterministic generation
# ---------------------------------------------------------------------------
def test_generation_never_mutates_original(real_fixture, base_rows, tmp_path):
    before = adv.sha256_file(real_fixture)
    for scenario in adv.SCENARIOS:
        if scenario.category == "performance":
            continue
        adv.generate_scenario_copy(base_rows, scenario, tmp_path)
    # generate one expansion too
    adv.generate_expansion(base_rows, 500, tmp_path)
    after = adv.sha256_file(real_fixture)
    assert before == after


def test_generation_mutation_is_deterministic(base_rows):
    # The mutation logic itself is a pure, deterministic function of the base
    # rows + seed (independent of any workbook serialization).
    import random

    scenario = adv.scenario_by_code("AU")
    spec1 = scenario.build(base_rows, random.Random("123:AU"))
    spec2 = scenario.build(base_rows, random.Random("123:AU"))
    assert spec1 == spec2


def test_expansion_mutation_is_deterministic(base_rows):
    import random

    from atlas.data_integrity.adversarial import _expansion_spec

    spec1 = _expansion_spec(base_rows, 500, random.Random("7:EXP:500"))
    spec2 = _expansion_spec(base_rows, 500, random.Random("7:EXP:500"))
    assert spec1 == spec2


def test_generated_files_have_identical_data(base_rows, tmp_path):
    # Round-trip determinism: two generations of the same seed produce files
    # whose *data* is identical when read back.
    d1 = tmp_path / "a"; d2 = tmp_path / "b"
    d1.mkdir(); d2.mkdir()
    p1 = adv.generate_expansion(base_rows, 500, d1, seed=7)
    p2 = adv.generate_expansion(base_rows, 500, d2, seed=7)
    assert adv.extract_sheet_rows(p1, "All_Jobs") == adv.extract_sheet_rows(p2, "All_Jobs")
    a1 = adv.generate_scenario_copy(base_rows, adv.scenario_by_code("AU"), d1, seed=123)
    a2 = adv.generate_scenario_copy(base_rows, adv.scenario_by_code("AU"), d2, seed=123)
    assert adv.extract_sheet_rows(a1, "All_Jobs") == adv.extract_sheet_rows(a2, "All_Jobs")


def test_generated_copy_differs_from_original(real_fixture, base_rows, tmp_path):
    scenario = adv.scenario_by_code("A")
    p = adv.generate_scenario_copy(base_rows, scenario, tmp_path)
    assert adv.sha256_file(p) != adv.sha256_file(real_fixture)


# ---------------------------------------------------------------------------
# Malformed workbooks are handled, not crashed on
# ---------------------------------------------------------------------------
def test_ingest_non_xlsx_bytes_returns_load_error(tmp_path):
    bad = tmp_path / "not_really.xlsx"
    bad.write_bytes(b"this is definitely not a workbook")
    result = ingest_workbook(bad, mapping=default_mapping())
    assert result.load_error is not None
    assert result.records == []


def test_ingest_truncated_zip_returns_load_error(tmp_path):
    # a real zip that is not a valid xlsx
    bad = tmp_path / "empty.xlsx"
    with zipfile.ZipFile(bad, "w") as zf:
        zf.writestr("hello.txt", "world")
    result = ingest_workbook(bad, mapping=default_mapping())
    assert result.load_error is not None


def test_ingest_missing_file_returns_load_error(tmp_path):
    result = ingest_workbook(tmp_path / "nope.xlsx", mapping=default_mapping())
    assert result.load_error is not None


# ---------------------------------------------------------------------------
# Every non-performance scenario is detected as expected
# ---------------------------------------------------------------------------
def test_all_non_performance_scenarios_pass(base_rows, tmp_path):
    m = default_mapping()
    failures = []
    for scenario in adv.SCENARIOS:
        if scenario.category == "performance":
            continue
        row = run_scenario(scenario, base_rows, tmp_path, mapping=m)
        if not row["passed"]:
            failures.append((row["code"], row["missing_signals"], row["finding_codes"], row["relationships"]))
    assert not failures, f"scenarios failed: {failures}"


@pytest.mark.parametrize("code", ["A", "G", "P", "Q", "AQ", "AG", "AH", "N", "AM", "AN"])
def test_representative_scenarios_individually(code, base_rows, tmp_path):
    m = default_mapping()
    scenario = adv.scenario_by_code(code)
    row = run_scenario(scenario, base_rows, tmp_path, mapping=m)
    assert row["passed"], f"{code} missing {row['missing_signals']}"
