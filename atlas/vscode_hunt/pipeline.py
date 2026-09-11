"""Deterministic end-to-end assembly (PRODUCTION R1 §9–§10, §15).

Ties the already-built pieces together with no live browsing:
discovery evidence + committed worker results -> the report trust boundary ->
cumulative canonical history -> the run workbook + the cumulative master workbook.

This is orchestration over existing modules, not a new framework: discovery
(dedup/prefilter) lives in :mod:`atlas.discovery`, the trust boundary in
:mod:`atlas.reporting.trust_boundary`, and cumulative history / the master
workbook in :mod:`atlas.vscode_hunt.history`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from atlas.discovery.report import build_discovery_workbook, load_results_by_task
from atlas.persistence.sqlite import StateStore
from atlas.reporting.trust_boundary import ValidatedJob, partition_jobs

from .history import build_master_workbook, record_run_jobs


def collect_validated_jobs(live_root: Path) -> list[ValidatedJob]:
    """Every worker acceptance proposal that independently passes the Python gate."""
    validated: list[ValidatedJob] = []
    for result in load_results_by_task(live_root):
        accepted, _rejected = partition_jobs(
            result.get("jobs", []),
            official_domain=str(result.get("official_domain", "")),
            company=str(result.get("company", result.get("company_id", ""))),
        )
        validated.extend(accepted)
    return validated


def assemble_run_outputs(
    *,
    store: StateStore,
    run_id: str,
    evidence_root: Path,
    live_root: Path,
    output_root: Path,
    discovery_source: str = "vscode_hunt",
) -> dict[str, Any]:
    """Record validated jobs into cumulative history, then build both workbooks.

    Returns the two workbook paths, the validated count, and the history summary.
    The run workbook stays run-scoped; the master workbook is cumulative so prior
    verified jobs never disappear.
    """
    validated = collect_validated_jobs(live_root)
    history = record_run_jobs(store, run_id=run_id, validated_jobs=validated, discovery_source=discovery_source)
    run_workbook = build_discovery_workbook(
        evidence_root=evidence_root, live_root=live_root, run_id=run_id, output_root=output_root,
    )
    master_workbook = build_master_workbook(store, output_root)
    return {
        "run_id": run_id,
        "validated_count": len(validated),
        "history": history,
        "run_workbook": str(run_workbook),
        "master_workbook": str(master_workbook),
    }
