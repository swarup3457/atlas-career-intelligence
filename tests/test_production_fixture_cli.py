"""Phase 1B.1 — offline production-fixture CLI (build spec 24).

`atlas production-fixture run/resume/status` executes the SAME production
integration path with synthetic, zero-network fixtures, supports a deliberate
partial stop + resume, prints sealed-plan planned/terminal counts and the
report/checkpoint references, and never enables live production.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

pytestmark = pytest.mark.integration


def _env(tmp_path):
    env = dict(os.environ)
    state = tmp_path / "state"
    env.update(
        ATLAS_STATE_DB=str(state / "st.sqlite"),
        ATLAS_CHECKPOINT_DB=str(state / "cp.sqlite"),
        ATLAS_OUTPUT_DIR=str(tmp_path / "out"),
        ATLAS_LOGS_DIR=str(tmp_path / "logs"),
        ATLAS_BROWSER_PROFILE=str(tmp_path / "prof"),
        ATLAS_AGENTS_DIR=str(tmp_path / "agents"),
        ATLAS_SKILLS_DIR=str(tmp_path / "skills"),
    )
    return env


def _cli(env, *args):
    proc = subprocess.run(
        [sys.executable, "-m", "atlas.cli", "production-fixture", *args],
        capture_output=True, text=True, env=env, timeout=180,
    )
    return proc


def test_cli_run_status_partial_resume(tmp_path):
    env = _env(tmp_path)

    # Deliberate partial stop after DISCOVER.
    r1 = _cli(env, "run", "--run-id", "cli1", "--companies", "2", "--stop-after-discover")
    assert r1.returncode == 0, r1.stderr
    assert "SYNTHETIC DATA ONLY" in r1.stdout
    assert "STATUS=PARTIAL" in r1.stdout
    assert "PLANNED_CHILD_TASKS=12" in r1.stdout
    assert "live production remains DISABLED" in r1.stdout

    # status reflects the sealed plan and coverage.
    rs = _cli(env, "status", "--run-id", "cli1")
    assert rs.returncode == 0
    assert "PLAN_STATE=SEALED" in rs.stdout
    assert "COVERAGE_PLANNED=12" in rs.stdout

    # resume completes.
    r2 = _cli(env, "resume", "--run-id", "cli1", "--companies", "2")
    assert r2.returncode == 0, r2.stderr
    assert "STATUS=COMPLETE" in r2.stdout
    assert "TERMINAL_CHILD_TASKS=12" in r2.stdout
    assert "REPORT=" in r2.stdout and "VALID=True" in r2.stdout

    # Exactly one workbook produced.
    workbooks = list((tmp_path / "out").glob("Atlas_Jobs_*.xlsx"))
    assert len(workbooks) == 1
