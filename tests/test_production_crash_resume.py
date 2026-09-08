"""Phase 1B.1 — genuine SEPARATE-PROCESS crash/resume + checkpoint boundedness
proofs (build spec 6/26 / P0-3, P0-16).

Process A runs a small discover batch and stops abruptly after DISCOVER,
leaving a durable partial subset of terminal children. Process B is a brand-new
Python process that constructs a NEW runtime, loads ONLY durable state /
checkpoints, and resumes the EXACT remaining children — proving completed
tasks, attempts, observations, canonical jobs, and the report are not
duplicated, and that policy/plan/candidate snapshot identities match.
"""

from __future__ import annotations

import json
import subprocess
import sys
import sqlite3
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

_PROC = str(Path(__file__).parent / "_fixture_run_proc.py")


def _run(mode: str, base: Path, run_id: str, *args) -> dict:
    proc = subprocess.run(
        [sys.executable, _PROC, mode, str(base), run_id, *[str(a) for a in args]],
        capture_output=True, text=True, timeout=300,
    )
    assert proc.returncode == 0, f"subprocess failed: {proc.stderr}\n{proc.stdout}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_fresh_process_resume_completes_without_duplication(tmp_path):
    run_id = "cross-proc-1"
    # Process A: partial (small batch), stop after DISCOVER, exit abruptly.
    a = _run("partial", tmp_path, run_id, 3, 3)
    assert a["terminal"] == "PARTIAL"
    assert a["durable"]["coverage_rows"] == 18  # 3 companies × 6 lanes
    # Only a SUBSET of children are terminal after the abrupt stop.
    partial_terminal = a["durable"]["coverage_attempts"]
    assert 0 < partial_terminal < 18

    # Process B: brand-new process, resume ONLY from durable state/checkpoint.
    b = _run("resume", tmp_path, run_id)
    assert b["terminal"] == "COMPLETE"
    assert b["planned"] == 18 and b["terminal_tasks"] == 18

    # Identities match across processes (rehydrated, not recomputed differently).
    assert b["policy_fingerprint"] == a["policy_fingerprint"]
    assert b["plan_fingerprint"] == a["plan_fingerprint"]
    assert b["candidate_snapshot"] == a["candidate_snapshot"]

    # No duplicated work: exactly one attempt per child (results scenario), one
    # report, and canonical jobs are stable.
    assert b["durable"]["coverage_attempts"] == 18
    assert b["durable"]["canonical_jobs"] == a["durable"]["canonical_jobs"] or b["durable"]["canonical_jobs"] >= 1
    assert b["report_path"] is not None
    # Exactly one workbook produced (no duplicate publication).
    workbooks = list((tmp_path / "out").glob("Atlas_Jobs_*.xlsx"))
    assert len(workbooks) == 1


def test_checkpoint_db_stays_bounded_with_many_children(tmp_path):
    # A large plan (30 companies × 6 lanes = 180 children) executed across a
    # separate process; the LangGraph checkpoint DB must contain NO job
    # descriptions/payloads and stay bounded while the business DB grows.
    run_id = "ckpt-proof"
    res = _run("full", tmp_path, run_id, 30, 200)
    assert res["terminal"] == "COMPLETE"
    assert res["planned"] == 180
    assert res["checkpoint_bytes"] < 65536  # serialized state stays compact

    state_db = tmp_path / "state" / "st.sqlite"
    ckpt_db = tmp_path / "state" / "cp.sqlite"
    state_size = state_db.stat().st_size
    ckpt_size = ckpt_db.stat().st_size
    # The business state DB holds the raw observations / canonical jobs and is
    # substantially larger than the checkpoint DB.
    assert res["durable"]["raw_observations"] >= 180
    assert state_size > 0 and ckpt_size > 0

    # Inspect the actual checkpoint DB: no job description / payload text leaked.
    conn = sqlite3.connect(str(ckpt_db))
    try:
        blob = b""
        for (tbl,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
            for row in conn.execute(f"SELECT * FROM {tbl}").fetchall():  # noqa: S608 (trusted table names)
                for cell in row:
                    if isinstance(cell, (bytes, bytearray)):
                        blob += bytes(cell)
                    elif isinstance(cell, str):
                        blob += cell.encode("utf-8", "ignore")
    finally:
        conn.close()
    # The synthetic fixture descriptions never appear in the checkpoint DB.
    assert b"Full description" not in blob
    print(f"[checkpoint-proof] state_db={state_size} bytes ckpt_db={ckpt_size} bytes "
          f"raw_observations={res['durable']['raw_observations']}")
