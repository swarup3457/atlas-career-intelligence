"""Shared helpers for the Phase 0.95 offline test suite.

Everything here builds *disposable* Settings rooted entirely at a pytest
``tmp_path`` so no test ever touches the real Atlas state DBs, checkpoint
DBs, authenticated browser profile, or the immutable ``fixtures/real``
data file.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from atlas.config import Settings, load_settings


def make_settings(root: Path, **overrides: Any) -> Settings:
    """Build a fully-disposable Settings whose every path lives under ``root``."""
    kwargs: dict[str, Any] = dict(
        state_db=root / "state" / "atlas_state.sqlite",
        checkpoint_db=root / "state" / "atlas_checkpoints.sqlite",
        output_dir=root / "output",
        logs_dir=root / "logs",
        browser_profile=root / "profile",
        agents_dir=root / "agents",
        skills_dir=root / "skills",
        batch_size=5,
        retry_budget=2,
    )
    kwargs.update(overrides)
    settings = load_settings(**kwargs)
    settings.ensure_directories()
    return settings


def seed_agents_and_skills(settings: Settings) -> None:
    """Put a little deterministic content into agents/ and skills/."""
    (settings.agents_dir / "company_researcher.md").write_text(
        "# Company Researcher\nDeterministic demo agent definition.\n", encoding="utf-8"
    )
    (settings.agents_dir / "nested").mkdir(parents=True, exist_ok=True)
    (settings.agents_dir / "nested" / "helper.md").write_text("nested helper\n", encoding="utf-8")
    (settings.skills_dir / "extract_jobs.md").write_text(
        "# Extract Jobs\nDeterministic demo skill definition.\n", encoding="utf-8"
    )
