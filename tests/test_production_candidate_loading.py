"""Phase 1B.1 — production candidate evidence loading (build spec 16 / P0-7).

Fixture mode uses the synthetic (PII-free) ledger and REPORTS that it did so.
Production mode requires a private, versioned candidate snapshot loaded from a
gitignored path, records only its hash, and NEVER silently substitutes
synthetic evidence when the snapshot is missing/invalid.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from atlas.config import load_settings
from atlas.orchestration.production_state import ProductionTerminalState
from atlas.runtime.production import ProductionSearchRuntime

pytestmark = pytest.mark.integration

_PRIVATE_PROFILE = Path(r"C:\Atlas-Agent-Import\06_CANDIDATE_PROFILE_VERIFIED.md")


def _settings(tmp_path, **overrides):
    kwargs = dict(
        state_db=tmp_path / "state" / "atlas_state.sqlite",
        checkpoint_db=tmp_path / "state" / "atlas_checkpoints.sqlite",
        output_dir=tmp_path / "output",
        logs_dir=tmp_path / "logs",
        browser_profile=tmp_path / "profile",
        agents_dir=tmp_path / "agents",
        skills_dir=tmp_path / "skills",
    )
    kwargs.update(overrides)
    s = load_settings(**kwargs)
    s.ensure_directories()
    return s


def test_fixture_mode_uses_synthetic_and_reports_it(tmp_path):
    res = ProductionSearchRuntime(_settings(tmp_path), "fix-cand").run()
    assert res.terminal_state == ProductionTerminalState.COMPLETE.value
    assert res.manifest["synthetic_candidate_evidence"] is True
    assert res.candidate_snapshot == "synthetic"


def test_production_mode_missing_snapshot_waits_for_human(tmp_path):
    rt = ProductionSearchRuntime(
        _settings(tmp_path), "prod-nosnap", fixture_mode=False,
        candidate_snapshot_path=tmp_path / "missing.private.json",
    )
    res = rt.run()
    # Never silently synthetic; a missing private snapshot waits for a human.
    assert res.terminal_state == ProductionTerminalState.WAITING_FOR_HUMAN.value


def test_production_mode_loads_private_snapshot_hash_only(tmp_path):
    snap = tmp_path / "cand.private.json"
    payload = {
        "schema_version": 1,
        "provenance": {"source_sha256": "abc", "synthetic": False},
        "claims": [{
            "claim_id": "c1", "topic": "Java", "normalized_value": "professional",
            "evidence_class": "PROFESSIONAL", "source_document_id": "resume",
            "scope": "employment", "confidence": 0.85, "conflict_state": "NONE", "notes": "",
        }],
    }
    snap.write_text(json.dumps(payload), encoding="utf-8")
    rt = ProductionSearchRuntime(
        _settings(tmp_path), "prod-snap", fixture_mode=False, candidate_snapshot_path=snap,
    )
    res = rt.run()
    assert res.terminal_state == ProductionTerminalState.COMPLETE.value
    assert res.manifest["synthetic_candidate_evidence"] is False
    # Only a hash is recorded — never the claims themselves.
    assert res.candidate_snapshot and res.candidate_snapshot != "synthetic"
    assert len(res.candidate_snapshot) <= 32


@pytest.mark.skipif(not _PRIVATE_PROFILE.exists(), reason="private profile not present on this machine")
def test_private_profile_parses_structurally_output_not_committed():
    # PRIVATE LOCAL acceptance test (build spec 16): runs against the ACTUAL
    # verified-profile markdown when present, validates parser STRUCTURE only,
    # and never prints content or writes committed output.
    from atlas.candidate.importer import parse_private_profile

    text = _PRIVATE_PROFILE.read_text(encoding="utf-8", errors="ignore")
    ledger = parse_private_profile(text)
    # Structural assertions only — no content is surfaced.
    assert len(ledger.claims()) > 0
    assert all(c.claim_id.startswith("parsed::") for c in ledger.claims())
    assert all(c.source_document_id.startswith("private_profile::") for c in ledger.claims())
