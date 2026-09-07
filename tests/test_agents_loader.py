"""Pytest coverage for atlas.agents_loader (Phase 0.5 spec section 2)."""

from __future__ import annotations

from pathlib import Path

import pytest

from atlas.agents_loader import SpecValidationError, load_agents, load_skills

pytestmark = pytest.mark.unit

EXPECTED_AGENT_NAMES = {
    "ORCHESTRATOR", "COMPANY_DISCOVERY", "CAREER_SEARCH", "ATS_SEARCH",
    "PORTAL_SEARCH", "VERIFICATION", "DEDUPLICATION", "CANDIDATE_MATCH", "REPORTING",
}


def test_load_agents_returns_expected_specs(project_root):
    agents = load_agents(project_root / "agents")
    assert len(agents) == 9
    assert {a.name for a in agents} == EXPECTED_AGENT_NAMES


def test_load_skills_returns_at_least_one(project_root):
    skills = load_skills(project_root / "skills")
    assert len(skills) >= 1


def test_load_agents_rejects_malformed_spec(tmp_path):
    bad_dir = tmp_path / "agents_bad"
    bad_dir.mkdir()
    (bad_dir / "BAD.agent.md").write_text("---\nstatus: SCAFFOLDED\n---\nbody", encoding="utf-8")
    with pytest.raises(SpecValidationError):
        load_agents(bad_dir)


def test_load_agents_empty_dir_returns_empty_list(tmp_path):
    empty_dir = tmp_path / "agents_empty"
    empty_dir.mkdir()
    assert load_agents(empty_dir) == []
