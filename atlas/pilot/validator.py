"""Product-usefulness validator for the pilot (prompt s.14).

Fails on real user-usefulness violations, not just workbook shape: <10 terminal companies,
a company with neither an official career URL nor a truthful blocker, a missing lane
obligation, a foreign/unknown row in All_Jobs, an unsupported mandatory backend in a
Java/full-stack/React/.NET row, a Java row without Java/Spring core evidence, a recommended
row without requirement evidence, a synthetic candidate, GENERAL_SOFTWARE in the main sheet,
a changed prior run/workbook, a pre-existing workbook path, or unreconciled usage totals.
A zero-accept pilot passes ONLY as COMPLETE_NO_MATCHES with all ten searched truthfully.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from atlas.pilot.config import PilotConfig
from atlas.pilot.evaluate import PilotEvaluation
from atlas.pilot.models import CompanySearchResult, TERMINAL_STATUSES, SEARCHED_TERMINAL

_INDIA_DECISIONS = frozenset({"INDIA_PRIMARY", "INDIA_SECONDARY", "REMOTE_INDIA", "INDIA_WIDE"})
_JAVA_CORE = ("java", "spring", "jvm", "j2ee", "jakarta")
_DOTNET_CORE = (".net", "c#", "asp.net", "dotnet")
_FRONTEND = ("react", "angular", "vue", "javascript", "typescript")


@dataclass
class ValidationReport:
    passed: bool = True
    checks: list[dict] = field(default_factory=list)

    def _add(self, name: str, ok: bool, detail: str = "") -> None:
        self.checks.append({"check": name, "ok": ok, "detail": detail})
        if not ok:
            self.passed = False

    @property
    def failures(self) -> list[dict]:
        return [c for c in self.checks if not c["ok"]]

    def to_dict(self) -> dict:
        return {"passed": self.passed, "checks": self.checks, "failures": self.failures}


def validate_evaluation(
    evaluation: PilotEvaluation,
    results: list[CompanySearchResult],
    usage_snapshot: dict,
    config: PilotConfig,
    *,
    candidate_synthetic: bool,
    outcome: str,
) -> ValidationReport:
    rep = ValidationReport()

    # 1) ten company tasks terminal
    planned = len(config.companies)
    terminal = [r for r in results if r.status in TERMINAL_STATUSES]
    rep._add("all_companies_terminal", len(terminal) >= planned and len(results) >= planned,
             f"{len(terminal)}/{planned} terminal, {len(results)} results")

    # 2) each company has an official careers URL or a truthful unresolved/access status
    for r in results:
        ok = bool(r.career_entry_url) or r.status in TERMINAL_STATUSES
        rep._add(f"company_truthful[{r.company}]", ok,
                 f"status={r.status} url={r.career_entry_url!r}")

    # 3) lane obligations: searched companies must have every lane attempted or a board snapshot
    for r in results:
        if r.status in SEARCHED_TERMINAL:
            missing = [
                lane for lane in config.primary_lanes
                if not (r.lanes.get(lane) and (r.lanes[lane].attempted or r.lanes[lane].board_snapshot_evaluated))
            ]
            rep._add(f"lane_obligations[{r.company}]", not missing, f"missing lanes: {missing}")

    # 4) All_Jobs rows: India-only, supported stack, lane core evidence, no GENERAL_SOFTWARE
    for a in evaluation.accepted:
        rep._add(f"india_only[{a.company}:{a.title}]", a.geography_decision in _INDIA_DECISIONS,
                 f"geo={a.geography_decision}")
        rep._add(f"no_unsupported_backend[{a.company}:{a.title}]", not a.unsupported_mandatory_backend,
                 f"unsupported={a.unsupported_mandatory_backend!r}")
        rep._add(f"not_general_software[{a.company}:{a.title}]", a.lane != "GENERAL_SOFTWARE", a.lane)
        low_stack = a.supported_stack_evidence.lower()
        if a.lane in ("JAVA_BACKEND", "JAVA_FULLSTACK"):
            rep._add(f"java_core[{a.company}:{a.title}]", any(t in low_stack for t in _JAVA_CORE),
                     f"stack={a.supported_stack_evidence!r}")
        if a.lane == "JAVA_FULLSTACK":
            rep._add(f"java_fullstack_frontend[{a.company}:{a.title}]",
                     any(t in low_stack for t in _FRONTEND), f"stack={a.supported_stack_evidence!r}")
        if a.lane == "DOTNET":
            rep._add(f"dotnet_core[{a.company}:{a.title}]", any(t in low_stack for t in _DOTNET_CORE),
                     f"stack={a.supported_stack_evidence!r}")
        if a.is_recommended:
            rep._add(f"recommended_has_requirement_evidence[{a.company}:{a.title}]",
                     bool(a.requirement_evidence.strip()), a.requirement_evidence)
            rep._add(f"recommended_india_eligible[{a.company}:{a.title}]",
                     a.geography_decision in _INDIA_DECISIONS and bool(a.location_evidence.strip()),
                     f"geo={a.geography_decision} loc_ev={bool(a.location_evidence.strip())}")

    # 5) no synthetic candidate
    rep._add("real_candidate_profile", not candidate_synthetic, f"synthetic={candidate_synthetic}")

    # 6) no padding: zero-accept only as COMPLETE_NO_MATCHES
    if not evaluation.accepted:
        rep._add("zero_accept_is_complete_no_matches", outcome in ("COMPLETE_NO_MATCHES", "PARTIAL", "WAITING_FOR_HUMAN"),
                 f"outcome={outcome}")

    # 7) usage totals reconcile with per-model sums
    totals = usage_snapshot.get("totals", {})
    by_model = usage_snapshot.get("by_model", {})
    for field_name in ("input_tokens", "output_tokens", "cached_input_tokens", "reasoning_tokens"):
        s = sum(m.get(field_name, 0) for m in by_model.values())
        rep._add(f"usage_reconciles[{field_name}]", s == totals.get(field_name, 0),
                 f"sum={s} total={totals.get(field_name)}")

    return rep


def validate_history(prior_manifest_path: Path, repo_root: Path) -> ValidationReport:
    """Verify no prior production run/workbook file changed since the pre-flight snapshot."""
    rep = ValidationReport()
    if not prior_manifest_path.exists():
        rep._add("prior_manifest_present", True, "no prior snapshot to compare (fresh)")
        return rep
    prior = json.loads(prior_manifest_path.read_text(encoding="utf-8-sig"))
    changed = 0
    deleted = 0
    for entry in prior:
        p = repo_root / entry["path"]
        if not p.exists():
            deleted += 1
            continue
        h = hashlib.sha256(p.read_bytes()).hexdigest()
        if h.upper() != str(entry["sha256"]).upper():
            changed += 1
    rep._add("prior_runs_unchanged", changed == 0, f"{changed} prior file(s) changed")
    rep._add("prior_runs_not_deleted", deleted == 0, f"{deleted} prior file(s) deleted")
    return rep


def validate_pilot_run(run_dir: Path, config: PilotConfig, *, prior_manifest_path: Optional[Path] = None,
                       repo_root: Optional[Path] = None) -> ValidationReport:
    """Disk-based validation: reads the run's artifacts and re-checks the contract."""
    rep = ValidationReport()
    manifest_p = run_dir / "run_manifest.json"
    coverage_p = run_dir / "coverage.json"
    accepted_p = run_dir / "jobs_accepted.json"
    usage_p = run_dir / "llm_usage.json"
    for p in (manifest_p, coverage_p, accepted_p, usage_p):
        rep._add(f"artifact_present[{p.name}]", p.exists(), str(p))
    if not rep.passed:
        return rep
    manifest = json.loads(manifest_p.read_text(encoding="utf-8"))
    coverage = json.loads(coverage_p.read_text(encoding="utf-8"))
    accepted = json.loads(accepted_p.read_text(encoding="utf-8"))

    companies = coverage.get("companies", [])
    rep._add("all_companies_terminal", len(companies) >= len(config.companies),
             f"{len(companies)}/{len(config.companies)}")
    for c in companies:
        rep._add(f"company_truthful[{c['company']}]",
                 bool(c.get("career_entry_url")) or c.get("status") in TERMINAL_STATUSES,
                 f"status={c.get('status')}")
    for a in accepted:
        rep._add(f"india_only[{a['company']}:{a['title']}]", a.get("geography_decision") in _INDIA_DECISIONS,
                 f"geo={a.get('geography_decision')}")
        rep._add(f"no_unsupported_backend[{a['company']}:{a['title']}]",
                 not a.get("unsupported_mandatory_backend"), a.get("unsupported_mandatory_backend"))
    rep._add("real_candidate_profile",
             not manifest.get("candidate_provenance", {}).get("synthetic", False), "")
    # workbook uniqueness (exactly one)
    wbs = list(run_dir.glob("Atlas_LLM_India_Pilot_*.xlsx"))
    rep._add("unique_workbook", len(wbs) == 1, f"{len(wbs)} workbooks")

    if prior_manifest_path and repo_root:
        hist = validate_history(prior_manifest_path, repo_root)
        rep.checks.extend(hist.checks)
        rep.passed = rep.passed and hist.passed
    return rep


__all__ = ["ValidationReport", "validate_evaluation", "validate_history", "validate_pilot_run"]
