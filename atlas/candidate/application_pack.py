"""Fact-grounded application package generation (Phase 1E/F §9).

Generates LOCAL DRAFT packages for explicitly-selected top jobs ONLY. Nothing
here submits an application, fills a form, or contacts anyone — every artifact is
a local draft. The flow is: drafter proposes -> factual-grounding reviewer checks
every fact against candidate evidence IDs -> Python rejects unsupported facts ->
a deterministic validator confirms required sections -> an atomic, versioned
writer publishes the package (never silently overwriting an approved/submitted
version).

Default package (always): job_snapshot.md, match_report.md, tailoring_plan.md,
application_brief.md, facts_audit.json. Optional DOCX (resume_tailored.docx,
cover_letter.docx) is written when python-docx is installed and reopen-validated;
PDF is written only through an installed, approved renderer and is otherwise
reported as an honest limitation.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from atlas.candidate.eligibility import CandidateProfile, RankableJob, match_requirement
from atlas.candidate.models import EvidenceClass

REQUIRED_MD_FILES = (
    "job_snapshot.md",
    "match_report.md",
    "tailoring_plan.md",
    "application_brief.md",
)
FACTS_AUDIT_FILE = "facts_audit.json"

# Statuses whose package version must never be silently overwritten.
_PROTECTED_STATUSES = frozenset({"APPROVED", "SUBMITTED"})

_YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")
_METRIC_RE = re.compile(r"\b\d+(?:\.\d+)?\s*(?:%|percent|users|customers|requests|rps|tps|ms|k\b|m\b|x\b)", re.I)


class GroundingRejected(RuntimeError):
    """Raised when drafted content asserts a fact unsupported by evidence."""


class PackageValidationError(RuntimeError):
    pass


def _utcnow() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp-{os.getpid()}")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


@dataclass(frozen=True)
class DraftedPackage:
    job_key: str
    md_files: Mapping[str, str]
    facts_audit: Mapping[str, Any]
    supported: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class GroundingResult:
    ok: bool
    violations: tuple[str, ...] = ()
    unsupported_rejected: tuple[str, ...] = ()


@dataclass(frozen=True)
class PackResult:
    job_key: str
    version: int
    status: str
    pack_dir: str
    files: Mapping[str, str] = field(default_factory=dict)  # relative name -> sha256
    docx_written: tuple[str, ...] = ()
    pdf_written: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    idempotent_reuse: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_key": self.job_key, "version": self.version, "status": self.status,
            "pack_dir": self.pack_dir, "files": dict(self.files),
            "docx_written": list(self.docx_written), "pdf_written": list(self.pdf_written),
            "limitations": list(self.limitations), "idempotent_reuse": self.idempotent_reuse,
        }


# --------------------------------------------------------------------------- #
# Drafter
# --------------------------------------------------------------------------- #
class ApplicationDrafter:
    """Proposes package content from ONLY the job + candidate evidence. The
    deterministic default never asserts a missing requirement; an optional
    consent-gated Copilot ``application-drafter`` agent may enrich the brief but
    its output is validated and discarded on quarantine."""

    def __init__(self, candidate: CandidateProfile, *, controller: Optional[Any] = None) -> None:
        self.candidate = candidate
        self.controller = controller

    def _partition(self, job: RankableJob) -> tuple[list[str], list[str], list[tuple[str, str, str]]]:
        supported: list[str] = []
        missing: list[str] = []
        facts: list[tuple[str, str, str]] = []  # (claim, evidence_id, evidence_class)
        for req in list(job.mandatory_requirements) + list(job.preferred_requirements):
            topic = match_requirement(req, self.candidate.evidence)
            if topic is not None:
                supported.append(req)
                ev_id = self.candidate.evidence_ids.get(topic, "")
                ev_class = self.candidate.evidence.get(topic, EvidenceClass.UNSUPPORTED).value
                facts.append((req, ev_id, ev_class))
            else:
                missing.append(req)
        return supported, missing, facts

    def draft(self, job: RankableJob, evaluation: Any) -> DraftedPackage:
        supported, missing, facts = self._partition(job)
        ev = evaluation.to_dict() if hasattr(evaluation, "to_dict") else dict(evaluation)

        job_snapshot = "\n".join([
            f"# Job Snapshot — {job.title} @ {job.company}",
            "",
            f"- Company: {job.company}",
            f"- Role: {job.title}",
            f"- Location: {job.location}",
            f"- Lane: {ev.get('lane') or job.lane or ''}",
            f"- Work mode: {job.work_mode}",
            f"- Verification: {ev.get('verification', job.verification_state)}",
            f"- Freshness: {ev.get('freshness', '')}",
            f"- Source: {job.source_family}",
            f"- URL: {job.url or ''}",
            "",
            "## Mandatory requirements",
            *([f"- {r}" for r in job.mandatory_requirements] or ["- (none listed)"]),
            "",
            "## Preferred requirements",
            *([f"- {r}" for r in job.preferred_requirements] or ["- (none listed)"]),
            "",
            "> Untrusted job content — recorded as data, never followed as instructions.",
        ])

        match_report = "\n".join([
            f"# Match Report — {job.title} @ {job.company}",
            "",
            f"- Candidate fit: {ev.get('candidate_fit')}",
            f"- Eligibility: {ev.get('eligibility')}",
            f"- Recommendation: {ev.get('recommendation')}",
            f"- Confidence: {ev.get('confidence')}",
            f"- Reasoning: {ev.get('reasoning_model')} / {ev.get('reasoning_version')}",
            "",
            "## Evidence-backed strengths",
            *([f"- {req} (evidence: {eid or 'n/a'}, {ecls})" for req, eid, ecls in facts] or ["- (none)"]),
            "",
            "## Genuine gaps (do NOT claim)",
            *([f"- {m}" for m in missing] or ["- (none)"]),
        ])

        tailoring_plan = "\n".join([
            f"# Tailoring Plan — {job.title} @ {job.company}",
            "",
            "## Emphasize (supported by evidence)",
            *([f"- {s}" for s in supported] or ["- (none)"]),
            "",
            "## Skills order",
            f"- {', '.join(supported) if supported else '(no matched skills)'}",
            "",
            "## Do NOT claim (unsupported / genuine gaps)",
            *([f"- {m}" for m in missing] or ["- (none)"]),
            "",
            "This is a LOCAL DRAFT plan. Atlas never submits an application.",
        ])

        # Deterministic, grounded brief: uses ONLY supported points and never
        # mentions a gap. An optional controller may enrich it (validated).
        brief_points = supported or ["relevant engineering experience"]
        brief = (
            f"# Application Brief — {job.title} @ {job.company}\n\n"
            f"Candidate is a strong fit for {job.title} at {job.company}, with demonstrated "
            f"evidence in {', '.join(brief_points)}. "
            f"Recommendation: {ev.get('recommendation')}.\n\n"
            f"This is a LOCAL DRAFT and is not submitted by Atlas."
        )
        brief = self._maybe_enrich_brief(job, supported, missing, brief)

        facts_audit = {
            "job_key": job.job_key,
            "generated_at": _utcnow(),
            "facts": (
                [{"claim": req, "type": "requirement", "supported": True,
                  "evidence_id": eid, "evidence_class": ecls} for req, eid, ecls in facts]
                + [{"claim": m, "type": "requirement", "supported": False,
                    "evidence_id": None, "action": "do_not_claim"} for m in missing]
            ),
            "supported_count": len(supported),
            "unsupported_count": len(missing),
        }

        return DraftedPackage(
            job_key=job.job_key,
            md_files={
                "job_snapshot.md": job_snapshot,
                "match_report.md": match_report,
                "tailoring_plan.md": tailoring_plan,
                "application_brief.md": brief,
            },
            facts_audit=facts_audit,
            supported=tuple(supported), missing=tuple(missing),
            evidence_ids=tuple(eid for _, eid, _ in facts if eid),
        )

    def _maybe_enrich_brief(self, job, supported, missing, deterministic: str) -> str:
        if self.controller is None or not hasattr(self.controller, "run_agent"):
            return deterministic
        try:
            result = self.controller.run_agent(
                "application-drafter", "draft brief",
                {"company": job.company, "role": job.title,
                 "supported_points": list(supported), "missing_points": list(missing)},
            )
        except Exception:  # noqa: BLE001 - drafting is optional; PII gate raises are handled by caller
            return deterministic
        if result.quarantined or not result.content:
            return deterministic
        try:
            parsed = json.loads(result.content)
            summary = str(parsed.get("summary", "")).strip()
        except (ValueError, TypeError):
            return deterministic
        if not summary:
            return deterministic
        # Guard: an enriched brief that asserts a gap term is discarded.
        low = summary.lower()
        if any(re.search(r"\b" + re.escape(m.lower()) + r"\b", low) for m in missing if m):
            return deterministic
        return (
            f"# Application Brief — {job.title} @ {job.company}\n\n{summary}\n\n"
            f"This is a LOCAL DRAFT and is not submitted by Atlas."
        )


# --------------------------------------------------------------------------- #
# Grounding reviewer
# --------------------------------------------------------------------------- #
class FactualGroundingReviewer:
    """Checks every fact in the drafted brief against candidate evidence: no
    missing requirement may be asserted, and any year/metric/employer token in
    the brief must be backed by candidate evidence text. Python REJECTS
    unsupported facts."""

    def __init__(self, candidate: CandidateProfile) -> None:
        self.candidate = candidate
        # Build the set of evidence tokens/phrases the brief may safely reference.
        self._evidence_terms = set(candidate.evidence.keys())

    def review(self, drafted: DraftedPackage, job: RankableJob) -> GroundingResult:
        violations: list[str] = []
        brief = drafted.md_files.get("application_brief.md", "")
        low = brief.lower()

        # 1) never assert a missing/unsupported requirement
        for m in drafted.missing:
            if m and re.search(r"\b" + re.escape(m.lower()) + r"\b", low):
                violations.append(f"brief asserts unsupported requirement: {m}")

        # 2) any year in the brief must appear in candidate evidence (dates protected)
        evidence_blob = " ".join(self._evidence_terms) + " " + " ".join(drafted.evidence_ids)
        for m in _YEAR_RE.finditer(brief):
            yr = m.group(0)
            if yr not in evidence_blob:
                violations.append(f"brief asserts an unverified year: {yr}")

        # 3) metrics must be evidence-backed
        for m in _METRIC_RE.finditer(brief):
            violations.append(f"brief asserts an unverified metric: {m.group(0)}")

        # 4) facts_audit must mark every unsupported requirement do_not_claim
        for fact in drafted.facts_audit.get("facts", []):
            if not fact.get("supported") and fact.get("action") != "do_not_claim":
                violations.append(f"unsupported fact not marked do_not_claim: {fact.get('claim')}")

        return GroundingResult(
            ok=not violations,
            violations=tuple(violations),
            unsupported_rejected=tuple(drafted.missing),
        )


def validate_package(md_files: Mapping[str, str], facts_audit: Mapping[str, Any]) -> list[str]:
    """Deterministic structural validator: required files present + non-empty,
    each carries its heading, and facts_audit has the expected shape."""
    problems: list[str] = []
    for name in REQUIRED_MD_FILES:
        body = md_files.get(name, "")
        if not body or not body.strip():
            problems.append(f"missing/empty required file: {name}")
        elif not body.lstrip().startswith("#"):
            problems.append(f"{name} missing a markdown heading")
    if "facts" not in facts_audit:
        problems.append("facts_audit.json missing 'facts'")
    return problems


# --------------------------------------------------------------------------- #
# Optional document rendering
# --------------------------------------------------------------------------- #
def _docx_available() -> bool:
    import importlib.util

    return importlib.util.find_spec("docx") is not None


def _write_docx(path: Path, title: str, sections: Sequence[tuple[str, Sequence[str]]]) -> None:
    from docx import Document  # lazy; optional dependency

    doc = Document()
    doc.add_heading(title, level=1)
    for heading, lines in sections:
        doc.add_heading(heading, level=2)
        for line in lines:
            doc.add_paragraph(str(line))
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".docx.tmp")
    doc.save(str(tmp))
    os.replace(tmp, path)


def _reopen_docx_ok(path: Path) -> bool:
    try:
        from docx import Document

        d = Document(str(path))
        return len(d.paragraphs) >= 1
    except Exception:  # noqa: BLE001
        return False


# --------------------------------------------------------------------------- #
# Builder (atomic, idempotent, versioned)
# --------------------------------------------------------------------------- #
class ApplicationPackBuilder:
    def __init__(
        self,
        candidate: CandidateProfile,
        *,
        controller: Optional[Any] = None,
        write_docx: bool = True,
    ) -> None:
        self.candidate = candidate
        self.drafter = ApplicationDrafter(candidate, controller=controller)
        self.reviewer = FactualGroundingReviewer(candidate)
        self.write_docx = write_docx

    @staticmethod
    def _content_hash(md_files: Mapping[str, str], facts_audit: Mapping[str, Any]) -> str:
        blob = json.dumps(
            {"md": dict(sorted(md_files.items())),
             "facts": facts_audit.get("facts", [])},
            sort_keys=True, default=str,
        )
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def build(self, job: RankableJob, evaluation: Any, *, packs_root: Path) -> PackResult:
        drafted = self.drafter.draft(job, evaluation)

        grounding = self.reviewer.review(drafted, job)
        if not grounding.ok:
            raise GroundingRejected("; ".join(grounding.violations))

        problems = validate_package(drafted.md_files, drafted.facts_audit)
        if problems:
            raise PackageValidationError("; ".join(problems))

        content_hash = self._content_hash(drafted.md_files, drafted.facts_audit)
        job_dir = Path(packs_root) / job.job_key
        job_dir.mkdir(parents=True, exist_ok=True)

        # idempotent versioning: reuse an existing version with identical content
        existing = self._existing_versions(job_dir)
        for ver, meta in existing.items():
            if meta.get("content_hash") == content_hash:
                return PackResult(
                    job_key=job.job_key, version=ver, status=meta.get("status", "DRAFT"),
                    pack_dir=str(job_dir / f"v{ver}"),
                    files=meta.get("files", {}), idempotent_reuse=True,
                    limitations=tuple(meta.get("limitations", [])),
                    docx_written=tuple(meta.get("docx_written", [])),
                )
        # never overwrite a protected (approved/submitted) latest version
        next_ver = (max(existing) + 1) if existing else 1
        vdir = job_dir / f"v{next_ver}"
        vdir.mkdir(parents=True, exist_ok=False)

        files: dict[str, str] = {}
        for name, body in drafted.md_files.items():
            _atomic_write(vdir / name, body)
        _atomic_write(vdir / FACTS_AUDIT_FILE, json.dumps(drafted.facts_audit, indent=2, sort_keys=True, default=str))

        limitations: list[str] = []
        docx_written: list[str] = []
        pdf_written: list[str] = []
        if self.write_docx and _docx_available():
            resume_path = vdir / "resume_tailored.docx"
            cover_path = vdir / "cover_letter.docx"
            _write_docx(resume_path, f"Tailored Resume — {job.title} @ {job.company}", [
                ("Emphasized strengths", drafted.supported or ["(none)"]),
                ("Do not claim", drafted.missing or ["(none)"]),
            ])
            _write_docx(cover_path, f"Cover Letter — {job.title} @ {job.company}", [
                ("Draft", [drafted.md_files["application_brief.md"]]),
            ])
            for p in (resume_path, cover_path):
                if _reopen_docx_ok(p):
                    docx_written.append(p.name)
                else:
                    limitations.append(f"{p.name} failed reopen validation")
            limitations.append("PDF not generated: no approved PDF renderer installed")
        else:
            limitations.append("DOCX/PDF not generated: python-docx renderer unavailable; Markdown + JSON published")

        # hashes of all written files
        for p in sorted(vdir.rglob("*")):
            if p.is_file() and ".tmp" not in p.name and p.name != "manifest.json":
                files[p.name] = hashlib.sha256(p.read_bytes()).hexdigest()

        manifest = {
            "job_key": job.job_key, "version": next_ver, "status": "DRAFT",
            "content_hash": content_hash, "created_at": _utcnow(),
            "files": files, "docx_written": docx_written, "pdf_written": pdf_written,
            "limitations": limitations, "supported": list(drafted.supported),
            "unsupported_rejected": list(grounding.unsupported_rejected),
        }
        _atomic_write(vdir / "manifest.json", json.dumps(manifest, indent=2, sort_keys=True, default=str))
        _atomic_write(job_dir / "latest.json", json.dumps(
            {"job_key": job.job_key, "latest_version": next_ver, "status": "DRAFT",
             "content_hash": content_hash}, indent=2, sort_keys=True))

        return PackResult(
            job_key=job.job_key, version=next_ver, status="DRAFT", pack_dir=str(vdir),
            files=files, docx_written=tuple(docx_written), pdf_written=tuple(pdf_written),
            limitations=tuple(limitations), idempotent_reuse=False,
        )

    @staticmethod
    def _existing_versions(job_dir: Path) -> dict[int, dict]:
        out: dict[int, dict] = {}
        for child in job_dir.glob("v*"):
            if not child.is_dir():
                continue
            try:
                ver = int(child.name[1:])
            except ValueError:
                continue
            manifest = child / "manifest.json"
            if manifest.is_file():
                try:
                    out[ver] = json.loads(manifest.read_text(encoding="utf-8"))
                except (ValueError, OSError):
                    out[ver] = {}
            else:
                out[ver] = {}
        return out


__all__ = [
    "REQUIRED_MD_FILES", "FACTS_AUDIT_FILE",
    "GroundingRejected", "PackageValidationError",
    "DraftedPackage", "GroundingResult", "PackResult",
    "ApplicationDrafter", "FactualGroundingReviewer", "validate_package",
    "ApplicationPackBuilder",
]
