"""Canonicalization / reconciliation of staged raw discovery observations
(Phase 1B.1 build spec 12 / P0-15; Phase 1C-A FINAL build spec 6 + 7).

Raw discoveries are staged (never written to ``canonical_jobs`` directly) during
DISCOVER and hydrated DETAIL revisions are staged during DETAIL_HYDRATION. This
module resolves ALL staged revisions (SEARCH and DETAIL) into canonical jobs in
the DEDUPE/RECONCILE phase, preserving every provenance.

Canonical identity follows the established contract (build spec 7), reusing the
Phase 0.9 ``identity_token`` normalizer — NOT a URL:

    1. a VERIFIED OFFICIAL requisition id in a CONFIRMED employer/source context
       (a source_job_id whose canonical/source URL host is trusted for its
       source family) -> ``reqid::{company}::{source_family}::{job_id}``; else
    2. normalized company + title + location (the fallback identity), plus the
       posted date so a probable repost (same role, DIFFERENT posted date) stays
       a DISTINCT canonical job rather than churning one identity.

URL is provenance, NEVER a cross-source identity component. Posting date is
evidence for repost classification, never the official identity itself. A portal
observation (no confirmed official requisition) that ALIGNS to exactly one
official canonical by company/title/location collapses into it as a lead; an
ambiguous alignment (matching several official canonicals) is preserved as its
own canonical and marked UNABLE_TO_DETERMINE rather than being silently merged.

A hydrated DETAIL revision shares its source SEARCH observation's official
requisition, so both revisions reconcile to the SAME canonical job (the report
then selects the highest-evidence revision) — one source job, one report row.

Observation-event identity is RUN-scoped (keyed by the staged observation id):
retrying the same page/attempt is idempotent (one event), but observing the same
source job again in a LATER run creates a new observation event against the same
canonical job — so first_seen/last_seen can advance correctly across runs.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from atlas.data_integrity.normalizers import identity_token
from atlas.sources.ats.base import host_trusted_for


# Official ATS families and the registrable apex their public hosts live under.
_FAMILY_APEX = {
    "greenhouse": "greenhouse.io",
    "lever": "lever.co",
    "ashby": "ashbyhq.com",
    "workday": "myworkdayjobs.com",
}

# Evidence revision ranking (highest wins as the canonical representative).
_EVIDENCE_RANK = {"OFFICIAL_DETAIL_LIVE": 3, "OFFICIAL_SEARCH_LIVE": 2, "PORTAL_LIVE": 1}


@dataclass
class CanonicalizeResult:
    observations_processed: int = 0
    canonical_created: int = 0
    canonical_updated: int = 0
    observations_added: int = 0
    cross_source_duplicates: int = 0
    reposts: int = 0
    portal_leads_aligned: int = 0
    ambiguous: int = 0
    relationships: list = field(default_factory=list)


def _host(url) -> str:
    try:
        return (urlsplit(url or "").hostname or "").lower()
    except (ValueError, TypeError):
        return ""


def _is_official(row) -> bool:
    """True when the observation is a CONFIRMED official-source requisition: it
    carries a source_job_id AND its canonical/source URL host is trusted for its
    declared source family (build spec 7). The URL only CONFIRMS the official
    context; it is never used as identity."""
    job_id = (row["source_job_id"] or "").strip()
    if not job_id:
        return False
    apex = _FAMILY_APEX.get((row["source_family"] or "").strip().lower())
    if apex is None:
        return False
    return any(host_trusted_for(h, apex) for h in (_host(row["canonical_url"]), _host(row["source_url"])) if h)


def _official_key(row) -> str:
    company = identity_token(row["company"])
    family = (row["source_family"] or "").strip().lower()
    job_id = (row["source_job_id"] or "").strip().lower()
    return f"reqid::{company}::{family}::{job_id}"


def _align_key(row) -> str:
    """company + title + location (URL- and date-INDEPENDENT). Links a portal
    lead to an official canonical and is the base of the fallback identity."""
    return f"ctl::{identity_token(row['company'])}::{identity_token(row['title'])}::{identity_token(row['location'])}"


def _fallback_key(row) -> str:
    """Fallback identity: company+title+location PLUS the posted date so a
    probable repost (same role, different posted date) stays a DISTINCT canonical
    job with a repost relationship, never silently merged (build spec 7)."""
    posted = (row["posted_at"] or "").strip()
    return f"{_align_key(row)}::{posted}"


def _canonical_id(key: str) -> str:
    return "canon::" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def _evidence_rank(row) -> tuple:
    try:
        detail = json.loads(row["detail_json"] or "{}")
    except (ValueError, TypeError):
        detail = {}
    return (
        _EVIDENCE_RANK.get(detail.get("verification_level") or "", 0),
        1 if row["revision_kind"] == "DETAIL" else 0,
    )


def _representative(rows):
    """The highest-evidence revision (build spec 6): a hydrated DETAIL revision
    beats a SEARCH observation; ties break deterministically."""
    return max(rows, key=lambda r: (_evidence_rank(r), r["observed_at"] or "", r["observation_id"]))


def canonicalize_run(store, run_id: str) -> CanonicalizeResult:
    """Resolve all STAGED raw observations (SEARCH + hydrated DETAIL revisions)
    for ``run_id`` into canonical jobs under the requisition-first identity
    contract (build spec 6 + 7)."""
    result = CanonicalizeResult()
    staged = store.list_raw_observations(run_id, processing_status="STAGED")

    # -- partition official (requisition-first) vs. fallback -----------------
    official_groups: dict[str, list] = {}
    fallback_rows: list = []
    for row in staged:
        if _is_official(row):
            official_groups.setdefault(_official_key(row), []).append(row)
        else:
            fallback_rows.append(row)

    # Map an alignment key (company/title/location) to the official canonical(s)
    # sharing it, so a portal lead can attach to a UNIQUE official canonical.
    align_to_officials: dict[str, set] = defaultdict(set)
    official_canonical: dict[str, str] = {}
    for okey, rows in official_groups.items():
        cid = _canonical_id(okey)
        official_canonical[okey] = cid
        align_to_officials[_align_key(rows[0])].add(cid)

    # -- assign every group a canonical id -----------------------------------
    groups: dict[str, list] = {}
    canonical_meta: dict[str, dict] = {}
    for okey, rows in official_groups.items():
        cid = official_canonical[okey]
        groups.setdefault(cid, []).extend(rows)
        canonical_meta.setdefault(cid, {"identity": "OFFICIAL_REQUISITION", "identity_key": okey})

    for row in fallback_rows:
        ak = _align_key(row)
        officials = align_to_officials.get(ak)
        if officials and len(officials) == 1:
            # Portal lead aligns to exactly one official canonical -> collapse.
            cid = next(iter(officials))
            groups.setdefault(cid, []).append(row)
            result.portal_leads_aligned += 1
        elif officials and len(officials) > 1:
            # Ambiguous: preserve the portal lead as its own canonical rather
            # than silently merging on title similarity (build spec 7).
            cid = _canonical_id("ambiguous::" + row["observation_id"])
            groups.setdefault(cid, []).append(row)
            canonical_meta[cid] = {"identity": "UNABLE_TO_DETERMINE", "identity_key": ak,
                                   "aligned_candidates": sorted(officials)}
            result.ambiguous += 1
        else:
            fkey = _fallback_key(row)
            cid = _canonical_id("fb::" + fkey)
            groups.setdefault(cid, []).append(row)
            canonical_meta.setdefault(cid, {"identity": "FALLBACK_CTL", "identity_key": fkey})

    # -- upsert canonicals + append observation events -----------------------
    for canonical_id, rows in groups.items():
        rep = _representative(rows)
        meta = canonical_meta.get(canonical_id, {"identity": "FALLBACK_CTL"})
        outcome = store.upsert_canonical_job(
            canonical_id,
            company=rep["company"], job_id=rep["source_job_id"], role=rep["title"],
            location=rep["location"], status="UNKNOWN",
            source_url=rep["source_url"], official_apply_url=rep["canonical_url"],
            content_hash=rep["content_hash"],
            payload={"lane": rep["lane"], "run_id": run_id, "identity": meta.get("identity"),
                     "identity_key": meta.get("identity_key"),
                     "evidence": (json.loads(rep["detail_json"] or "{}").get("verification_level")),
                     "revisions": len(rows)},
        )
        if outcome == "created":
            result.canonical_created += 1
        elif outcome == "updated":
            result.canonical_updated += 1

        seen_source_identity: set = set()
        distinct_sources: set = set()
        for row in rows:
            source_identity = row["source_identity"] or row["observation_id"]
            distinct_sources.add(row["source_instance"])
            if source_identity not in seen_source_identity:
                seen_source_identity.add(source_identity)
                added = store.add_observation(
                    f"jobobs::{canonical_id}::{row['observation_id']}",
                    canonical_id, row["observation_id"],
                    discovery_source=row["source_family"], observed_status=row["is_active"],
                    content_hash=row["content_hash"], adapter_version=row["adapter_version"] or "",
                    parser_version=row["parser_version"] or "",
                    provenance={"source_instance": row["source_instance"], "coverage_id": row["coverage_id"],
                                "query_signature": row["query_signature"], "attempt_id": row["attempt_id"],
                                "revision_kind": row["revision_kind"],
                                "parent_observation_id": row["parent_observation_id"], "run_id": run_id},
                )
                if added:
                    result.observations_added += 1
            store.mark_raw_observation_processed(row["observation_id"], canonical_id)
            result.observations_processed += 1
        if len(distinct_sources) > 1:
            result.cross_source_duplicates += 1

    # -- repost / new-vacancy relationship classification --------------------
    # Two canonical jobs sharing an alignment key (same company/title/location)
    # but resolved to DIFFERENT canonical ids are a probable repost / new vacancy
    # — recorded as a relationship, never merged into one identity.
    align_to_canonicals: dict[str, set] = defaultdict(set)
    for canonical_id, rows in groups.items():
        align_to_canonicals[_align_key(rows[0])].add(canonical_id)
    for ak, cids in align_to_canonicals.items():
        if len(cids) > 1:
            result.reposts += 1
            result.relationships.append({"align_key": ak, "canonical_ids": sorted(cids),
                                         "classification": "PROBABLE_REPOST_OR_NEW_VACANCY"})

    return result


__all__ = ["canonicalize_run", "CanonicalizeResult"]
