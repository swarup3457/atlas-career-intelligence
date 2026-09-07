"""Atlas local agent/skill specification loader.

Provides infrastructure to discover and validate local agent definition
files (agents/*.agent.md) and skill definitions (skills/<name>/SKILL.md)
so that, later, the real business rules from the existing ChatGPT
Workspace Atlas Agent can be migrated into this structure systematically.

Today's placeholder files under agents/ and skills/ intentionally contain
ONLY minimal metadata — no invented final Atlas business rules. See
docs/AGENT_SKILL_MIGRATION.md.

File format: a YAML frontmatter block (delimited by `---` lines) followed
by free-form Markdown body.

    ---
    name: ORCHESTRATOR
    description: Coordinates the overall Atlas run.
    status: SCAFFOLDED
    version: 0.1.0
    ---
    # Orchestrator

    Free-form documentation body...
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?(.*)\Z", re.DOTALL)

REQUIRED_FIELDS = ("name", "description")
VALID_STATUSES = {"PROVEN", "IMPLEMENTED", "SCAFFOLDED", "NOT_YET_BUILT"}


class SpecValidationError(ValueError):
    """Raised when an agent/skill spec file fails schema validation."""


@dataclass
class SpecFile:
    path: Path
    metadata: dict[str, Any]
    body: str

    @property
    def name(self) -> str:
        return self.metadata["name"]

    @property
    def description(self) -> str:
        return self.metadata["description"]

    @property
    def status(self) -> str:
        return self.metadata.get("status", "NOT_YET_BUILT")


def parse_spec_file(path: Path) -> SpecFile:
    """Parse one .agent.md / SKILL.md file into a SpecFile, validating the
    required minimal metadata schema."""
    text = path.read_text(encoding="utf-8")
    match = _FRONTMATTER_RE.match(text)
    if not match:
        raise SpecValidationError(
            f"{path}: missing YAML frontmatter block (expected file to start with '---')."
        )
    frontmatter_raw, body = match.group(1), match.group(2)
    metadata = yaml.safe_load(frontmatter_raw) or {}
    if not isinstance(metadata, dict):
        raise SpecValidationError(f"{path}: frontmatter must be a YAML mapping.")

    missing = [f for f in REQUIRED_FIELDS if f not in metadata or not metadata[f]]
    if missing:
        raise SpecValidationError(
            f"{path}: missing required frontmatter field(s): {', '.join(missing)}."
        )

    status = metadata.get("status", "NOT_YET_BUILT")
    if status not in VALID_STATUSES:
        raise SpecValidationError(
            f"{path}: invalid status '{status}'. Must be one of {sorted(VALID_STATUSES)}."
        )

    return SpecFile(path=path, metadata=metadata, body=body.strip())


def load_agents(agents_dir: Path) -> list[SpecFile]:
    """Load and validate every *.agent.md file under agents_dir."""
    agents_dir = Path(agents_dir)
    if not agents_dir.exists():
        return []
    return [parse_spec_file(p) for p in sorted(agents_dir.glob("*.agent.md"))]


def load_skills(skills_dir: Path) -> list[SpecFile]:
    """Load and validate every skills/<name>/SKILL.md file."""
    skills_dir = Path(skills_dir)
    if not skills_dir.exists():
        return []
    return [parse_spec_file(p) for p in sorted(skills_dir.glob("*/SKILL.md"))]
