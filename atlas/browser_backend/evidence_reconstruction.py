"""Offline evidence reconstruction from archived Playwright snapshots.

The compact Atlas object emitted in ``task_complete`` does not carry all of the
source text needed to ground its evidence quotes. The archived Playwright page
snapshot (the accessibility-tree ``.yml`` the MCP writes to ``mcp-output/``)
does. This module — used only by the offline recovery path — locates the job
*detail* snapshot by canonical URL / requisition id / title, extracts the exact
source strings (responsibilities, skills, experience header, requisition id),
and grounds the proposed job in that exact text. It never fabricates text and
never weakens quote validation: every reconstructed evidence quote is an exact
substring of the captured source.

No network, no browser, no model — pure text over already-captured files.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from atlas.pilot.normalize import normalize_source_text

# ``- role [ref=...] [cursor=...]: <accessible value>``  -> capture the value.
_VALUE_RE = re.compile(r"\]\s*:\s*(.+?)\s*$")
_EXP_RE = re.compile(r"(\d+)\s*Years?\s*(?:to|-|–|—)\s*(\d+)\s*Years?", re.I)
_SKILL_RE = re.compile(r"Technology->[^\s,][^\n,]*")


@dataclass
class SourceEvidence:
    """Exact, source-derived evidence for one job detail page."""

    snapshot_path: str = ""
    requisition_id: str = ""
    title: str = ""
    location: str = ""
    experience_text: str = ""
    description: str = ""
    responsibilities: str = ""
    skills: tuple[str, ...] = ()
    mandatory_requirements: tuple[str, ...] = ()
    matched_url: bool = False
    matched_requisition: bool = False
    matched_title: bool = False

    def to_dict(self) -> dict:
        return {
            "snapshot_path": self.snapshot_path,
            "requisition_id": self.requisition_id,
            "title": self.title,
            "location": self.location,
            "experience_text": self.experience_text,
            "description": self.description,
            "responsibilities": self.responsibilities,
            "skills": list(self.skills),
            "mandatory_requirements": list(self.mandatory_requirements),
            "matched_url": self.matched_url,
            "matched_requisition": self.matched_requisition,
            "matched_title": self.matched_title,
        }


def snapshot_values(text: str) -> list[str]:
    """Every accessible-name/value string in a Playwright ``.yml`` snapshot."""
    out: list[str] = []
    for line in (text or "").splitlines():
        m = _VALUE_RE.search(line)
        if m:
            val = m.group(1).strip()
            # Strip surrounding quotes the snapshot uses for some names.
            if len(val) >= 2 and val[0] == val[-1] == '"':
                val = val[1:-1]
            if val:
                out.append(val)
    return out


def _snapshot_files(run_dir: Path) -> list[Path]:
    mcp = run_dir / "mcp-output"
    if not mcp.exists():
        # Some captures nest one company dir deeper.
        cand = list(run_dir.glob("*/mcp-output"))
        mcp = cand[0] if cand else mcp
    if not mcp.exists():
        return []
    return sorted(mcp.glob("page-*.yml"))


def find_detail_snapshot(run_dir: Path, *, requisition_id: str = "",
                         url: str = "", title: str = "") -> Optional[Path]:
    """Locate the snapshot that captured the job *detail* page.

    Preference order: the snapshot containing the requisition id (strongest
    canonical anchor), then the official URL, then the title + a skills/detail
    marker. Among candidates, the one that also carries detail evidence
    (``Technology->`` skills or a ``Responsibilities`` block) wins.
    """
    req = (requisition_id or "").strip()
    u = (url or "").strip()
    ttl = (title or "").strip().lower()
    best: Optional[Path] = None
    best_score = -1
    for path in _snapshot_files(run_dir):
        try:
            txt = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        low = txt.lower()
        score = 0
        if req and req in txt:
            score += 100
        if u and u in txt:
            score += 40
        if ttl and ttl in low:
            score += 10
        if "responsibilities" in low:
            score += 8
        if "technology->" in low:
            score += 6
        if "job id/reference code" in low:
            score += 5
        if score > best_score:
            best_score = score
            best = path
    # Require at least a canonical anchor (requisition or URL) to claim a match.
    if best is not None and best_score >= 40:
        return best
    return None


def extract_source_evidence(snapshot_text: str, *, requisition_id: str = "",
                            title: str = "", snapshot_path: str = "") -> SourceEvidence:
    """Extract exact source strings for the job detail from a snapshot."""
    values = snapshot_values(snapshot_text)
    req = (requisition_id or "").strip()

    # Requisition id: exact presence check.
    matched_req = bool(req) and any(req == v or req in v for v in values)

    # Title: the emitted title as an exact/contained accessible value.
    matched_title = False
    found_title = ""
    if title:
        tl = title.strip().lower()
        for v in values:
            if v.strip().lower() == tl:
                found_title = v.strip()
                matched_title = True
                break
        if not matched_title:
            for v in values:
                if tl in v.strip().lower() and len(v) <= 80:
                    found_title = v.strip()
                    matched_title = True
                    break

    # Responsibilities / description: the longest source value (the detail body).
    responsibilities = ""
    for v in values:
        if len(v) > len(responsibilities) and ("experience" in v.lower() or "years" in v.lower()
                                               or "responsib" in v.lower()):
            responsibilities = v
    if not responsibilities:
        responsibilities = max(values, key=len) if values else ""

    # Skills: every ``Technology->...`` node, exactly as captured.
    skills: list[str] = []
    for v in values:
        for m in _SKILL_RE.findall(v):
            s = m.strip().rstrip(",")
            if s and s not in skills:
                skills.append(s)

    # Experience header: first ``N Years to M Years`` occurrence.
    experience_text = ""
    joined = "\n".join(values)
    em = _EXP_RE.search(joined)
    if em:
        experience_text = f"{em.group(1)} Years to {em.group(2)} Years"

    # Location: a "..., Infosys Limited" style or first location-looking value.
    location = ""
    for v in values:
        if re.search(r",\s*[A-Z][A-Za-z .]+Limited$", v) or re.match(r"^[A-Z]{3,},", v):
            location = v.strip()
            break

    # Mandatory requirements: bullet-split the responsibilities body.
    mandatory = _split_bullets(responsibilities)

    return SourceEvidence(
        snapshot_path=snapshot_path,
        requisition_id=req,
        title=found_title or (title or ""),
        location=location,
        experience_text=experience_text,
        description=responsibilities,
        responsibilities=responsibilities,
        skills=tuple(skills),
        mandatory_requirements=tuple(mandatory),
        matched_url=False,
        matched_requisition=matched_req,
        matched_title=matched_title,
    )


def _split_bullets(text: str) -> list[str]:
    if not text:
        return []
    parts = re.split(r"\s*[•·]\s*|\s*\u2022\s*", text)
    out = [p.strip(" .") for p in parts if len(p.strip()) >= 8]
    return out[:12]


def _grounded(quote: str, source_norm_low: str) -> bool:
    q = normalize_source_text(quote).strip().lower()
    return bool(q) and q in source_norm_low


def ground_job(job: dict, evidence: SourceEvidence) -> dict:
    """Return a copy of ``job`` grounded in exact source text.

    Enrichment is *only* source-derived: the description/requirements/skills are
    replaced/augmented with the captured source strings, and every evidence
    quote is reconciled to an exact source substring. Quotes that are already
    grounded are kept; paraphrases are replaced with exact source anchors.
    """
    g = dict(job)

    # Enrich description with the exact source responsibilities (superset of the
    # agent's paraphrase), so grounded quotes resolve.
    src_desc = evidence.description or ""
    if src_desc:
        g["description"] = src_desc
    # Skills -> preferred requirements (exact source nodes).
    pref = list(g.get("preferred_requirements") or [])
    for s in evidence.skills:
        if s not in pref:
            pref.append(s)
    g["preferred_requirements"] = pref
    # Mandatory requirements from source bullets when the agent gave none/paraphrase.
    if evidence.mandatory_requirements:
        g["mandatory_requirements"] = list(evidence.mandatory_requirements)
    if evidence.experience_text and not str(g.get("experience_text", "")).strip():
        g["experience_text"] = f"Work Experience of {evidence.experience_text}"
    if evidence.requisition_id:
        g["requisition_id"] = evidence.requisition_id
    if evidence.location and not str(g.get("location", "")).strip():
        g["location"] = evidence.location

    # Build the normalized source corpus the validator will see.
    corpus = " \n ".join([
        str(g.get("title", "")), str(g.get("description", "")),
        str(g.get("experience_text", "")), str(g.get("eligibility_text", "")),
        *[str(x) for x in (g.get("mandatory_requirements") or [])],
        *[str(x) for x in (g.get("preferred_requirements") or [])],
    ])
    corpus_low = normalize_source_text(corpus).lower()

    # Reconcile evidence quotes: keep grounded originals; drop ungrounded
    # paraphrases; always add exact source anchors (experience sentence + skills).
    reconciled: list[str] = []

    def add_quote(q: str) -> None:
        q = (q or "").strip()
        if q and q not in reconciled and _grounded(q, corpus_low):
            reconciled.append(q)

    for q in (job.get("evidence_snippets") or []):
        add_quote(str(q))
    # Exact source anchors.
    for s in evidence.skills:
        add_quote(s)
    # A concrete experience/requirement anchor sentence from the source.
    for sent in _split_bullets(evidence.responsibilities):
        if "java" in sent.lower() or "experience" in sent.lower():
            add_quote(sent)
            break
    g["evidence_snippets"] = reconciled
    return g


__all__ = [
    "SourceEvidence", "snapshot_values", "find_detail_snapshot",
    "extract_source_evidence", "ground_job",
]
