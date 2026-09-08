"""Standalone process entry point for genuine separate-process crash/resume
proofs (build spec 6/26). Not a pytest module — invoked as a subprocess:

    python tests/_fixture_run_proc.py <mode> <base_dir> <run_id> [n_companies] [batch]

Modes:
    partial  — run with a small discover batch, stop after DISCOVER (abrupt),
               leaving a durable partial subset of terminal children.
    resume   — construct a NEW runtime and resume the remaining children.
    full     — run to completion in one process.

Prints a JSON line with the run's terminal state and durable counters so the
parent test can assert exact resume without duplicated side effects.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from atlas.config import load_settings
from atlas.orchestration.production_state import ProductionPhase
from atlas.persistence.sqlite import StateStore
from atlas.runtime.production import ProductionSearchRuntime, default_fixture_topology


def _settings(base: Path):
    s = load_settings(
        state_db=base / "state" / "st.sqlite",
        checkpoint_db=base / "state" / "cp.sqlite",
        output_dir=base / "out",
        logs_dir=base / "logs",
        browser_profile=base / "prof",
        agents_dir=base / "agents",
        skills_dir=base / "skills",
    )
    s.ensure_directories()
    return s


def _durable_counts(base: Path, run_id: str) -> dict:
    with StateStore(base / "state" / "st.sqlite") as store:
        cov = store.list_coverage(run_id)
        total_attempts = sum(len(store.list_coverage_attempts(c["coverage_id"])) for c in cov)
        return {
            "coverage_rows": len(cov),
            "coverage_attempts": total_attempts,
            "raw_observations": store.count_raw_observations(run_id),
            "canonical_jobs": store.count_canonical_jobs(),
        }


def main() -> None:
    mode = sys.argv[1]
    base = Path(sys.argv[2])
    run_id = sys.argv[3]
    n_companies = int(sys.argv[4]) if len(sys.argv) > 4 else 3
    batch = int(sys.argv[5]) if len(sys.argv) > 5 else 3
    instances, companies = default_fixture_topology(n_companies)
    settings = _settings(base)

    if mode == "partial":
        rt = ProductionSearchRuntime(
            settings, run_id, instances=instances, companies=companies,
            discover_batch=batch, stop_after_phase=ProductionPhase.DISCOVER, use_run_lock=True,
        )
        res = rt.run()
    elif mode == "resume":
        rt = ProductionSearchRuntime(settings, run_id, instances=instances, companies=companies, use_run_lock=True)
        res = rt.resume()
    else:
        rt = ProductionSearchRuntime(settings, run_id, instances=instances, companies=companies, use_run_lock=True)
        res = rt.run()

    out = {
        "terminal": res.terminal_state,
        "planned": res.planned_tasks,
        "terminal_tasks": res.terminal_tasks,
        "policy_fingerprint": res.policy_fingerprint,
        "plan_fingerprint": res.plan_fingerprint,
        "candidate_snapshot": res.candidate_snapshot,
        "report_path": res.report_path,
        "checkpoint_bytes": res.checkpoint_bytes,
        "durable": _durable_counts(base, run_id),
    }
    print(json.dumps(out))


if __name__ == "__main__":
    main()
