"""Phase 1B — agent/skill thin-pointer migration tests (build spec 21/24)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SKILLS = REPO / "skills"
AGENTS = REPO / "agents"

pytestmark = pytest.mark.unit

# Language that would indicate a skill still owns Excel operational state.
_WORKBOOK_PATTERNS = (
    re.compile(r"atlas_master_active", re.I),
    re.compile(r"active workbook", re.I),
    re.compile(r"open (?:exactly )?one workbook", re.I),
    re.compile(r"overwrite the active", re.I),
)

# Language that would indicate a skill/agent owns loops/state it must not.
_OWNERSHIP_PATTERNS = (
    re.compile(r"\bowns? (?:the )?(?:retry|retries|coverage|pagination|persistence)\b", re.I),
)


def _skill_files():
    return list(SKILLS.rglob("SKILL.md"))


def test_no_skill_requires_an_active_workbook():
    offenders = []
    for f in _skill_files():
        text = f.read_text(encoding="utf-8")
        for pat in _WORKBOOK_PATTERNS:
            if pat.search(text):
                offenders.append((f.name, pat.pattern))
    assert not offenders, f"skills still reference active-workbook mechanics: {offenders}"


def test_recruiter_skill_remains_blocked():
    f = SKILLS / "recruiter-outreach-prep" / "SKILL.md"
    assert f.exists()
    text = f.read_text(encoding="utf-8")
    assert "BLOCKED_PENDING_POLICY" in text
    assert "05_PUBLIC_RECRUITER_CONTACT_POLICY.md" in text


def test_recommended_reasoning_agents_exist():
    for name in (
        "QUERY_STRATEGIST", "VERIFICATION_REVIEWER", "MATCH_ANALYST",
        "INTERNATIONAL_ELIGIBILITY_REVIEWER", "APPLICATION_BRIEF_WRITER", "TREND_ANALYST",
    ):
        assert (AGENTS / f"{name}.agent.md").exists(), name


def test_reasoning_agents_do_not_own_loops_or_state():
    for f in AGENTS.glob("*.agent.md"):
        text = f.read_text(encoding="utf-8")
        for pat in _OWNERSHIP_PATTERNS:
            assert not pat.search(text), f"{f.name} claims to own a loop/coverage/persistence"
        # each new reasoning agent must explicitly disclaim ownership
    for name in ("QUERY_STRATEGIST", "MATCH_ANALYST", "VERIFICATION_REVIEWER"):
        text = (AGENTS / f"{name}.agent.md").read_text(encoding="utf-8")
        assert "Never owns" in text


def test_agents_md_is_a_pointer_not_a_duplicate():
    f = REPO / "AGENTS.md"
    assert f.exists()
    text = f.read_text(encoding="utf-8")
    # points at canonical policy/architecture instead of restating them
    assert "config/policy" in text
    assert "canonical" in text.lower()
    # it should be short (a pointer), not a giant duplicated policy doc
    assert len(text.splitlines()) < 80


def test_no_skill_or_agent_contains_pii():
    banned = ("dev" + "ati", "swa" + "rup", "profile.pdf")
    for f in list(SKILLS.rglob("*.md")) + list(AGENTS.glob("*.md")):
        low = f.read_text(encoding="utf-8").lower()
        for b in banned:
            assert b not in low, f"{f} contains {b}"
