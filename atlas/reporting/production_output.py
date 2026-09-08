"""Stable production output contract (Phase 1E/F §10).

Every live run publishes exactly one IMMUTABLE run directory under
``production_output_root/runs/<RUN_ID>/`` and, only for a validated
operator-eligible COMPLETE result, atomically updates a ``latest`` pointer. All
outputs are git-ignored (under ``output/``). The workbook is written once, after
fan-in, through the proven atomic + reopen-validated writer
(:func:`atlas.reporting.mapping.write_report`) — never a bare ``wb.save()``.

Contract (per run):

    runs/<RUN_ID>/
      Atlas_Jobs_<RUN_ID>.xlsx        (8 sheets, reopen-validated)
      run_manifest.json               (status + sha256 of every file)
      coverage.json
      source_health.json
      recommendations.json
      portal_leads.json
      verification_summary.json
      application_packs/<JOB_KEY>/...

    latest/
      Atlas_Jobs_LATEST.xlsx
      latest_run.json
      latest_run.txt
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from atlas.config import Settings
from atlas.reporting.mapping import (
    REQUIRED_SHEETS,
    ReportMapping,
    load_report_mapping,
    validate_report,
    write_report,
)

WORKBOOK_PREFIX = "Atlas_Jobs_"
LATEST_WORKBOOK = "Atlas_Jobs_LATEST.xlsx"

# JSON side files (besides the workbook) written into each run directory.
SIDE_FILES = (
    "coverage.json",
    "source_health.json",
    "recommendations.json",
    "portal_leads.json",
    "verification_summary.json",
)

# Terminal statuses that make a run directory immutable (a completed publication).
_IMMUTABLE_STATUSES = frozenset({"COMPLETE"})


class RunAlreadyPublishedError(RuntimeError):
    """Raised when publishing over an already-COMPLETE (immutable) run dir."""


class ProductionOutputError(RuntimeError):
    pass


def _utcnow() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp-{os.getpid()}")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _atomic_copy(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(dst.suffix + f".tmp-{os.getpid()}")
    shutil.copyfile(src, tmp)
    os.replace(tmp, dst)


@dataclass(frozen=True)
class ProductionRunPaths:
    root: Path
    run_id: str

    @property
    def run_dir(self) -> Path:
        return self.root / "runs" / self.run_id

    @property
    def workbook(self) -> Path:
        return self.run_dir / f"{WORKBOOK_PREFIX}{self.run_id}.xlsx"

    @property
    def manifest(self) -> Path:
        return self.run_dir / "run_manifest.json"

    @property
    def application_packs_dir(self) -> Path:
        return self.run_dir / "application_packs"

    def side_file(self, name: str) -> Path:
        return self.run_dir / name


@dataclass(frozen=True)
class LatestPaths:
    root: Path

    @property
    def latest_dir(self) -> Path:
        return self.root / "latest"

    @property
    def workbook(self) -> Path:
        return self.latest_dir / LATEST_WORKBOOK

    @property
    def latest_run_json(self) -> Path:
        return self.latest_dir / "latest_run.json"

    @property
    def latest_run_txt(self) -> Path:
        return self.latest_dir / "latest_run.txt"


@dataclass(frozen=True)
class PublishResult:
    run_id: str
    status: str
    run_dir: str
    workbook_path: str
    report_valid: bool
    latest_updated: bool
    files: Mapping[str, str] = field(default_factory=dict)   # relative name -> sha256
    problems: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id, "status": self.status, "run_dir": self.run_dir,
            "workbook_path": self.workbook_path, "report_valid": self.report_valid,
            "latest_updated": self.latest_updated, "files": dict(self.files),
            "problems": list(self.problems),
        }


class ProductionOutputPublisher:
    """Publishes an immutable run directory and (optionally) updates ``latest``."""

    def __init__(self, settings: Settings, *, mapping: Optional[ReportMapping] = None) -> None:
        self.settings = settings
        self.root = Path(settings.production_output_root)
        self.mapping = mapping or load_report_mapping()

    # -- publication --------------------------------------------------------
    def publish(
        self,
        run_id: str,
        *,
        status: str,
        sheet_data: Mapping[str, Sequence[Mapping[str, Any]]],
        coverage: Optional[Mapping[str, Any]] = None,
        source_health: Optional[Mapping[str, Any]] = None,
        recommendations: Optional[Mapping[str, Any]] = None,
        portal_leads: Optional[Mapping[str, Any]] = None,
        verification_summary: Optional[Mapping[str, Any]] = None,
        manifest_extra: Optional[Mapping[str, Any]] = None,
        eligible_for_latest: bool = True,
    ) -> PublishResult:
        paths = ProductionRunPaths(self.root, run_id)

        # Immutability: refuse to overwrite an already-COMPLETE run directory.
        if paths.manifest.exists():
            try:
                prior = json.loads(paths.manifest.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                prior = {}
            if prior.get("status") in _IMMUTABLE_STATUSES:
                raise RunAlreadyPublishedError(
                    f"run {run_id!r} already published as {prior.get('status')!r}; "
                    "the run directory is immutable"
                )

        paths.run_dir.mkdir(parents=True, exist_ok=True)
        paths.application_packs_dir.mkdir(parents=True, exist_ok=True)

        problems: list[str] = []

        # 1) side JSON files (atomic)
        side_payloads = {
            "coverage.json": coverage or {},
            "source_health.json": source_health or {},
            "recommendations.json": recommendations or {},
            "portal_leads.json": portal_leads or {},
            "verification_summary.json": verification_summary or {},
        }
        for name, payload in side_payloads.items():
            _atomic_write_text(paths.side_file(name), json.dumps(payload, indent=2, sort_keys=True, default=str))

        # 2) the 8-sheet workbook (atomic + reopen-validated)
        data = {sheet: list(sheet_data.get(sheet, [])) for sheet in REQUIRED_SHEETS}
        report_valid = False
        try:
            write_report(self.mapping, paths.workbook, data)
            validation = validate_report(self.mapping, paths.workbook)
            report_valid = validation.ok
            if not validation.ok:
                problems.extend(validation.problems)
        except Exception as exc:  # noqa: BLE001 - report failure is truthful, never COMPLETE
            problems.append(f"workbook write/validate failed: {exc}")

        # A run cannot be COMPLETE if its mandatory workbook is invalid.
        effective_status = status
        if not report_valid and status == "COMPLETE":
            effective_status = "PARTIAL"
            problems.append("workbook invalid -> downgraded COMPLETE to PARTIAL")

        # 3) manifest with sha256 of every published file (except the manifest)
        files: dict[str, str] = {}
        for p in sorted(paths.run_dir.rglob("*")):
            if p.is_file() and p.name != "run_manifest.json" and not p.name.startswith(".tmp") and ".tmp-" not in p.name:
                files[str(p.relative_to(paths.run_dir)).replace("\\", "/")] = _sha256_file(p)
        manifest = {
            "run_id": run_id,
            "status": effective_status,
            "published_at": _utcnow(),
            "report_valid": report_valid,
            "eligible_for_latest": bool(eligible_for_latest),
            "workbook": paths.workbook.name,
            "files": files,
            "problems": problems,
        }
        if manifest_extra:
            manifest["extra"] = dict(manifest_extra)
        _atomic_write_text(paths.manifest, json.dumps(manifest, indent=2, sort_keys=True, default=str))

        # 4) latest pointer — only for a validated, operator-eligible COMPLETE run
        latest_updated = False
        if effective_status == "COMPLETE" and report_valid and eligible_for_latest:
            latest_updated = self._update_latest(run_id, paths, effective_status)

        return PublishResult(
            run_id=run_id, status=effective_status, run_dir=str(paths.run_dir),
            workbook_path=str(paths.workbook), report_valid=report_valid,
            latest_updated=latest_updated, files=files, problems=tuple(problems),
        )

    def _update_latest(self, run_id: str, paths: ProductionRunPaths, status: str) -> bool:
        latest = LatestPaths(self.root)
        latest.latest_dir.mkdir(parents=True, exist_ok=True)
        _atomic_copy(paths.workbook, latest.workbook)
        payload = {
            "run_id": run_id, "status": status, "updated_at": _utcnow(),
            "run_dir": str(paths.run_dir), "workbook": str(paths.workbook),
        }
        _atomic_write_text(latest.latest_run_json, json.dumps(payload, indent=2, sort_keys=True))
        _atomic_write_text(latest.latest_run_txt, run_id + "\n")
        return True

    # -- read helpers -------------------------------------------------------
    def list_runs(self) -> list[dict[str, Any]]:
        runs_dir = self.root / "runs"
        out: list[dict[str, Any]] = []
        if not runs_dir.exists():
            return out
        for child in sorted(runs_dir.iterdir()):
            manifest = child / "run_manifest.json"
            if manifest.is_file():
                try:
                    m = json.loads(manifest.read_text(encoding="utf-8"))
                except (ValueError, OSError):
                    m = {"run_id": child.name, "status": "UNKNOWN"}
                out.append({"run_id": m.get("run_id", child.name), "status": m.get("status"),
                            "published_at": m.get("published_at"), "run_dir": str(child)})
        return out

    def show_run(self, run_id: str) -> Optional[dict[str, Any]]:
        manifest = ProductionRunPaths(self.root, run_id).manifest
        if not manifest.is_file():
            return None
        return json.loads(manifest.read_text(encoding="utf-8"))

    def latest(self) -> Optional[dict[str, Any]]:
        p = LatestPaths(self.root).latest_run_json
        if not p.is_file():
            return None
        return json.loads(p.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# Builders: turn a RankingResult into workbook rows + recommendations payload
# --------------------------------------------------------------------------- #
_APPLY_RECS = {"PRIORITY_APPLY", "STRONG_APPLY", "APPLY_AFTER_TAILORING"}
_CLOSED_RECS = {"REJECT", "CLOSED"}


def recommendations_payload(evaluations: Sequence[Any]) -> dict[str, Any]:
    """Serialize evaluations into recommendations.json. ``Relevant_Discoveries``
    means candidate-relevant AFTER ranking (an apply-family recommendation), not
    every row."""
    rows = [e.to_dict() if hasattr(e, "to_dict") else dict(e) for e in evaluations]
    relevant = sum(1 for r in rows if r.get("recommendation") in _APPLY_RECS)
    not_evaluated = sum(1 for r in rows if r.get("recommendation") == "NOT_EVALUATED")
    return {
        "total": len(rows),
        "relevant_after_ranking": relevant,
        "not_evaluated": not_evaluated,
        "evaluations": rows,
    }


def sheet_data_from_evaluations(
    evaluations: Sequence[Any], jobs_by_key: Mapping[str, Any]
) -> dict[str, list[dict[str, Any]]]:
    """Build All_Jobs / Closed_or_Rejected / Resume_Tailoring canonical rows from
    ranking evaluations. NOT_EVALUATED rows stay VISIBLE in All_Jobs."""
    all_jobs: list[dict[str, Any]] = []
    closed: list[dict[str, Any]] = []
    tailoring: list[dict[str, Any]] = []
    for e in evaluations:
        d = e.to_dict() if hasattr(e, "to_dict") else dict(e)
        job = jobs_by_key.get(d["job_key"])
        all_jobs.append({
            "company": d.get("company", ""), "role_title": d.get("title", ""),
            "location": getattr(job, "location", "") if job else "",
            "lane": d.get("lane") or "",
            "work_mode": getattr(job, "work_mode", "") if job else "",
            "discovery_source": getattr(job, "source_family", "") if job else "",
            "official_apply_url": getattr(job, "url", "") if job else "",
            "freshness_band": d.get("freshness", ""),
            "verification_level": d.get("verification", ""),
            "match_score": d.get("candidate_fit") if d.get("candidate_fit") is not None else "",
            "requirements_matched": ", ".join(d.get("strengths", [])),
            "missing_requirements": ", ".join(d.get("gaps", [])),
            "recommendation": d.get("recommendation", ""),
        })
        if d.get("recommendation") in _CLOSED_RECS:
            closed.append({
                "Company": d.get("company", ""), "Role": d.get("title", ""),
                "Source": getattr(job, "source_family", "") if job else "",
                "Reason": ", ".join(d.get("gaps", [])) or d.get("recommendation", ""),
                "Verification_Status": d.get("verification", ""),
            })
        if d.get("recommendation") in _APPLY_RECS:
            tailoring.append({
                "Company": d.get("company", ""), "Role": d.get("title", ""),
                "Job_ID": d.get("job_key", ""), "Match_Score": d.get("candidate_fit", ""),
                "Supported_Keywords": ", ".join(d.get("strengths", [])),
                "Genuine_Gaps": ", ".join(d.get("gaps", [])),
                "Suggested_Resume_Filename": f"resume_{d.get('job_key','')}.docx",
            })
    return {"All_Jobs": all_jobs, "Closed_or_Rejected": closed, "Resume_Tailoring": tailoring}


__all__ = [
    "WORKBOOK_PREFIX", "LATEST_WORKBOOK", "SIDE_FILES",
    "RunAlreadyPublishedError", "ProductionOutputError",
    "ProductionRunPaths", "LatestPaths", "PublishResult", "ProductionOutputPublisher",
    "recommendations_payload", "sheet_data_from_evaluations",
]
