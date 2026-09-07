"""Developer-only safe reset helper (Phase 0.95).

:func:`safe_reset` clears *disposable* Atlas working directories during
development. It is built to make an accidental catastrophe impossible:

* It NEVER accepts arbitrary caller paths. Callers pass *purpose names*
  from a hardcoded allow-list (:data:`RESET_PURPOSES`); an unknown purpose
  is refused. There is deliberately **no** purpose that maps to the
  authenticated browser profiles, ``.venv``, ``fixtures/real``, the live
  ``state/`` databases, or any source directory — so those simply cannot
  be selected.
* Every resolved target is then re-checked by :func:`_assert_safe_target`,
  which hard-refuses (raises :class:`ResetSafetyError`) if the target is,
  contains, or lives inside a protected path, equals the project root, or
  resolves outside the project root. This defense-in-depth means even a
  future mis-configured purpose cannot delete something protected.

Targets are resolved relative to a caller-provided ``project_root`` so
tests can exercise real deletion against a disposable ``tmp_path`` layout
that mimics the project, never against the real ``C:\\Atlas`` tree.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import shutil
from typing import Iterable

# The ONLY purposes that can ever be cleared, mapped to fixed relative
# subpaths. Everything not listed here is unreachable by design.
RESET_PURPOSES: dict[str, str] = {
    "generated_fixtures": "fixtures/generated",
    "phase09_output": "output/phase09",
    "demo_output": "output/demo",
    "backups": "output/backups",
}

# Paths that must never be deleted, even if some purpose ever resolved to
# them. Names are relative to project_root.
_PROTECTED_RELATIVE = (
    ".browser-profile-chrome",
    ".browser-profile",
    "fixtures/real",
    ".venv",
    "state",
    "atlas",
    "tests",
    "docs",
    "config",
)


class ResetSafetyError(RuntimeError):
    """Raised when safe_reset is asked to touch a protected/unsafe path."""


@dataclass
class ResetResult:
    deleted: list[str] = field(default_factory=list)
    skipped_absent: list[str] = field(default_factory=list)

    def render(self) -> str:
        lines = [f"safe_reset: deleted {len(self.deleted)}, skipped {len(self.skipped_absent)}"]
        for d in self.deleted:
            lines.append(f"  deleted: {d}")
        for s in self.skipped_absent:
            lines.append(f"  skipped (absent): {s}")
        return "\n".join(lines)


def _is_relative_to(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def _assert_safe_target(target: Path, project_root: Path) -> None:
    """Refuse if ``target`` is/contains/lives-inside a protected path."""
    target = target.resolve()
    root = project_root.resolve()

    # Must be strictly inside the project root.
    if target == root:
        raise ResetSafetyError(f"Refusing to delete the project root itself: {target}")
    if root not in target.parents:
        raise ResetSafetyError(
            f"Refusing to delete {target}: resolves outside project_root {root}."
        )

    for rel in _PROTECTED_RELATIVE:
        protected = (root / rel).resolve()
        if target == protected:
            raise ResetSafetyError(f"Refusing to delete protected path: {target}")
        if _is_relative_to(target, protected):
            raise ResetSafetyError(
                f"Refusing to delete {target}: lives inside protected path {protected}."
            )
        if _is_relative_to(protected, target):
            raise ResetSafetyError(
                f"Refusing to delete {target}: it contains protected path {protected}."
            )


def safe_reset(purposes: Iterable[str], project_root: Path) -> ResetResult:
    """Clear the disposable directories named by ``purposes``.

    ``purposes`` is an iterable of keys from :data:`RESET_PURPOSES`. Any
    unknown key raises :class:`ResetSafetyError` before anything is
    deleted. Targets are resolved under ``project_root``.
    """
    project_root = Path(project_root)
    purposes = list(purposes)

    # Resolve + validate EVERY purpose before deleting anything, so a bad
    # entry aborts the whole operation (no partial destruction).
    resolved: list[tuple[str, Path]] = []
    for purpose in purposes:
        if purpose not in RESET_PURPOSES:
            raise ResetSafetyError(
                f"Unknown/unsupported reset purpose {purpose!r}. "
                f"Allowed: {sorted(RESET_PURPOSES)}."
            )
        target = (project_root / RESET_PURPOSES[purpose]).resolve()
        _assert_safe_target(target, project_root)
        resolved.append((purpose, target))

    result = ResetResult()
    for purpose, target in resolved:
        if target.exists():
            shutil.rmtree(target)
            result.deleted.append(f"{purpose} -> {target}")
        else:
            result.skipped_absent.append(f"{purpose} -> {target}")
    return result


__all__ = ["RESET_PURPOSES", "ResetSafetyError", "ResetResult", "safe_reset"]
