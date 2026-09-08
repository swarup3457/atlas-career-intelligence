"""Phase 1A: add-source developer scaffold tests (dry-run first, no real adapters)."""

from __future__ import annotations

import pytest

from atlas.sources.scaffold import generate_scaffold, render_plan, validate_scaffold_request, write_scaffold

pytestmark = pytest.mark.unit


def test_generate_scaffold_produces_expected_files():
    files = generate_scaffold("Acme Board", "ATS_GREENHOUSE", base_url="https://boards.greenhouse.io/acme")
    paths = set(files)
    assert any(p.endswith("acme_board.py") for p in paths)
    assert any(p.endswith(".yaml") for p in paths)
    assert any(p.startswith("tests/") for p in paths)
    assert any(p.startswith("docs/") for p in paths)
    adapter_src = next(v for k, v in files.items() if k.endswith("acme_board.py"))
    assert "class AcmeBoardAdapter(SourceAdapter)" in adapter_src
    assert "SourceType.ATS_GREENHOUSE" in adapter_src
    assert "NotImplementedError" in adapter_src  # a template, not a working adapter


def test_ats_family_routes_to_ats_dir_with_family_and_category():
    # build spec 23: derive family/category, route ATS to atlas/sources/ats/.
    files = generate_scaffold("Acme Board", "ATS_GREENHOUSE")
    adapter_path = next(k for k in files if k.endswith("acme_board.py"))
    assert adapter_path.startswith("atlas/sources/ats/")
    src = files[adapter_path]
    assert "source_family = SourceFamily.GREENHOUSE" in src
    assert "Capability.DISCOVER" in src  # ATS enumerates entry points
    yaml = next(v for k, v in files.items() if k.endswith(".yaml"))
    assert "adapter_key (family): greenhouse" in yaml
    assert "url_reference" in yaml  # access notes documented


def test_portal_family_routes_to_portals_dir_without_discover():
    files = generate_scaffold("Big Portal", "PORTAL_LARGE")
    adapter_path = next(k for k in files if k.endswith("big_portal.py"))
    assert adapter_path.startswith("atlas/sources/portals/")
    src = files[adapter_path]
    # A generic portal does not guess a DISCOVER capability.
    assert "Capability.DISCOVER" not in src


def test_scaffold_includes_contract_and_health_tests():
    files = generate_scaffold("Acme Board", "ATS_LEVER")
    test_src = next(v for k, v in files.items() if k.startswith("tests/test_source_"))
    assert "run_contract_checks" in test_src
    assert "health_check" in test_src


def test_validate_rejects_unknown_type():
    with pytest.raises(ValueError):
        validate_scaffold_request("X", "ATS_NOPE")


def test_render_plan_writes_nothing(tmp_path):
    plan = render_plan("Acme Board", "PORTAL_LARGE")
    assert "DRY-RUN" in plan
    assert list(tmp_path.iterdir()) == []


def test_write_scaffold_creates_and_refuses_overwrite(tmp_path):
    written = write_scaffold("Acme Board", "ATS_LEVER", tmp_path)
    assert written and all(p.exists() for p in written)
    with pytest.raises(FileExistsError):
        write_scaffold("Acme Board", "ATS_LEVER", tmp_path)


def test_plan_flags_real_family_names():
    plan = render_plan("greenhouse", "ATS_GREENHOUSE")
    assert "TEMPLATE" in plan
