"""Implementation-matrix integrity check (PRODUCTION R1 §1B).

Every Atlas module and test the matrix cites must exist, so the audit can never
drift into an unfounded essay.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
MATRIX = REPO / "AI_JOB_SEARCH_IMPLEMENTATION_MATRIX.md"

_MODULE = re.compile(r"`(atlas/[\w/]+\.py)")
_TEST_REF = re.compile(r"(tests/[\w/]+\.py)::(\w+)")


def test_matrix_exists_and_is_a_table() -> None:
    text = MATRIX.read_text(encoding="utf-8")
    assert "| Pattern |" in text
    assert text.count("|") > 100  # a real table, not prose


def test_every_cited_atlas_module_exists() -> None:
    text = MATRIX.read_text(encoding="utf-8")
    modules = sorted(set(_MODULE.findall(text)))
    assert len(modules) >= 8
    missing = [module for module in modules if not (REPO / module).exists()]
    assert not missing, f"matrix cites missing modules: {missing}"


def test_every_cited_test_exists() -> None:
    text = MATRIX.read_text(encoding="utf-8")
    refs = sorted(set(_TEST_REF.findall(text)))
    assert len(refs) >= 8
    missing = []
    for rel_path, func in refs:
        path = REPO / rel_path
        if not path.exists() or func not in path.read_text(encoding="utf-8"):
            missing.append(f"{rel_path}::{func}")
    assert not missing, f"matrix cites missing tests: {missing}"
