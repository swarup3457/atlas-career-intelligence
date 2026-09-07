"""Shared pytest fixtures for the Atlas offline test suite.

No fixture here touches the real Atlas state DB, checkpoint DB, or
authenticated browser profile (C:\\Atlas\\.browser-profile-chrome) -
everything uses pytest's `tmp_path`.
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def project_root() -> Path:
    return Path(__file__).resolve().parents[1]


@pytest.fixture
def real_fixture(project_root: Path) -> Path:
    """Path to the immutable Phase 0.9 fixture workbook.

    Skips the test cleanly if the fixture is not present on this machine, so
    the suite stays runnable in environments without the real data file.
    """
    path = project_root / "fixtures" / "real" / "Atlas_Jobs_2026-08-13.xlsx"
    if not path.exists():
        pytest.skip(f"real fixture not available at {path}")
    return path


EXPECTED_FIXTURE_SHA256 = (
    "440B9587610FBA5B8C0E61498FE52EE92A5139749F1F29D57ED7734A2A25B8AC".lower()
)
