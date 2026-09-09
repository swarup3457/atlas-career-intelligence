"""Failing-first tests for the redacted candidate SEARCH profile (audit 3.6, prompt s.9).

This file must remain PII-free: any candidate identifiers used for the no-leak assertion are
read from the private source at RUNTIME (never committed here).
"""

from __future__ import annotations

import json
import re

import pytest

from atlas.candidate.importer import import_candidate_evidence
from atlas.candidate.search_profile import (
    CandidateProfileWaitingForHuman,
    load_candidate_search_profile,
)


def _runtime_pii_tokens() -> list[str]:
    """Derive the candidate's name tokens from the private ledger at runtime."""
    res = import_candidate_evidence(write=False)
    tokens: list[str] = []
    for claim in res.ledger.claims():
        text = f"{claim.topic} {claim.normalized_value}"
        m = re.match(r"\s*name\s*:\s*(.+)", text, re.I)
        if m:
            tokens += [w.lower() for w in re.findall(r"[A-Za-z]{3,}", m.group(1))]
    return tokens


def test_real_private_profile_is_loaded_not_synthetic(tmp_path):
    p = load_candidate_search_profile(live=True, private_out=tmp_path / "p.json")
    assert p.synthetic is False
    assert set(p.target_lanes) == {
        "JAVA_BACKEND", "JAVA_FULLSTACK", "REACT_FRONTEND", "DOTNET",
        "ENTERPRISE_HR_PAYROLL_INTEGRATION",
    }
    assert p.skills.get("Java") == "PROFESSIONAL"
    assert p.skills.get("React") == "PROFESSIONAL"
    assert p.total_experience_years == pytest.approx(2.0)
    assert "Bengaluru" in p.preferred_locations


def test_gate_mode_is_private_local_for_real_profile(tmp_path):
    # The candidate-gate mode string must be the canonical PRIVATE_LOCAL for a real profile
    # (machine-gate contract) and SYNTHETIC only for the offline synthetic fallback.
    p = load_candidate_search_profile(live=True, private_out=tmp_path / "p.json")
    assert p.gate_mode == "PRIVATE_LOCAL"
    s = load_candidate_search_profile(
        live=False, allow_synthetic=True, source=tmp_path / "nope.md", private_out=tmp_path / "s.json"
    )
    assert s.gate_mode == "SYNTHETIC"


def test_redacted_profile_has_no_pii(tmp_path):
    p = load_candidate_search_profile(live=True, private_out=tmp_path / "p.json")
    blob = json.dumps(p.redacted_dict()).lower()
    name_tokens = _runtime_pii_tokens()
    leaks = [t for t in name_tokens if t and t in blob]
    assert leaks == [], f"candidate name leaked into redacted profile: {leaks}"
    assert "@" not in blob  # no email


def test_live_mode_missing_profile_is_waiting_for_human(tmp_path):
    missing = tmp_path / "does_not_exist.md"
    out = tmp_path / "private.json"
    with pytest.raises(CandidateProfileWaitingForHuman):
        load_candidate_search_profile(live=True, source=missing, private_out=out)


def test_offline_mode_may_use_synthetic(tmp_path):
    missing = tmp_path / "does_not_exist.md"
    out = tmp_path / "private.json"
    p = load_candidate_search_profile(live=False, allow_synthetic=True, source=missing, private_out=out)
    assert p.synthetic is True
    assert p.target_lanes


def test_hunt_candidate_projection_is_evidence_classed(tmp_path):
    p = load_candidate_search_profile(live=True, private_out=tmp_path / "p.json")
    cand = p.to_hunt_candidate()
    assert cand.evidence
    assert cand.total_experience_years == pytest.approx(2.0)
    assert set(cand.target_lanes) == set(p.target_lanes)


def test_loader_does_not_persist_by_default(monkeypatch):
    from atlas.candidate import importer as imp

    seen = {}
    real = imp.import_candidate_evidence

    def spy(*a, **kw):
        seen["write"] = kw.get("write", True)
        return real(*a, **kw)

    monkeypatch.setattr("atlas.candidate.search_profile.import_candidate_evidence", spy)
    load_candidate_search_profile(live=True)
    assert seen["write"] is False
