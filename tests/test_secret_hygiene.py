"""Secret-hygiene audit (Phase 0.5 spec section 11).

Verifies .gitignore protects every sensitive runtime path Atlas is known
to create, WITHOUT inspecting or printing any actual browser
cookies/tokens - this test only checks path patterns.

This uses a small, deliberately-simplified gitignore-pattern matcher
(directory-suffix patterns + fnmatch on the basename/relative path) - not
a full gitignore-spec implementation, but sufficient to prove the
required sensitive paths are covered.
"""

from __future__ import annotations

import fnmatch
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

# Relative paths (as they would appear under the Atlas project root) that
# MUST be ignored - these are exactly the categories called out by spec
# section 11: .browser-profile*, .venv, state/runtime DBs, logs, local
# config with secrets, temporary browser artifacts.
SENSITIVE_PATHS = [
    ".browser-profile/Default/Cookies",
    ".browser-profile-chrome/Default/Cookies",
    ".venv/Scripts/python.exe",
    "state/atlas_state.sqlite",
    "state/atlas_checkpoints.sqlite",
    "state/atlas_run.lock",
    "logs/atlas.log",
    "config/local.yaml",
    ".atlas-browser-manager.lock",
]


def _load_patterns(gitignore_path: Path) -> list[str]:
    patterns = []
    for line in gitignore_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        patterns.append(line)
    return patterns


def _is_ignored(rel_path: str, patterns: list[str]) -> bool:
    parts = Path(rel_path).parts
    for pattern in patterns:
        is_dir_pattern = pattern.endswith("/")
        core = pattern.rstrip("/")
        # Directory pattern: matches if ANY path segment matches the core
        # glob (handles patterns like ".browser-profile*/").
        if is_dir_pattern:
            if any(fnmatch.fnmatch(part, core) for part in parts[:-1]) or fnmatch.fnmatch(parts[0], core):
                return True
            continue
        # File/basename pattern (e.g. "*.sqlite", ".atlas-browser-manager.lock").
        if fnmatch.fnmatch(Path(rel_path).name, core):
            return True
        # Path-qualified pattern (contains a slash) - match against the
        # full relative path.
        if "/" in core and fnmatch.fnmatch(rel_path, core):
            return True
    return False


@pytest.fixture
def gitignore_patterns(project_root):
    return _load_patterns(project_root / ".gitignore")


@pytest.mark.parametrize("sensitive_path", SENSITIVE_PATHS)
def test_sensitive_runtime_path_is_gitignored(sensitive_path, gitignore_patterns):
    assert _is_ignored(sensitive_path, gitignore_patterns), (
        f"{sensitive_path!r} is NOT covered by .gitignore - secret hygiene regression"
    )


def test_gitignore_does_not_accidentally_ignore_source_code(gitignore_patterns):
    """Sanity check the other direction: ensure the broad patterns above
    have not accidentally swallowed real source files."""
    must_not_be_ignored = [
        "atlas/__init__.py",
        "atlas/config/__init__.py",
        "tests/test_secret_hygiene.py",
        "pyproject.toml",
        "docs/OPERATIONS.md",
    ]
    for path in must_not_be_ignored:
        assert not _is_ignored(path, gitignore_patterns), f"{path!r} should NOT be gitignored"
