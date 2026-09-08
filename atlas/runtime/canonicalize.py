"""Canonicalization / reconciliation of staged raw discovery observations
(Phase 1B.1, build spec 12 / P0-15; Phase 1C-A corrective build spec 13).

Raw discoveries are staged (never written to ``canonical_jobs`` directly)
during DISCOVER. This module resolves those staged observations into canonical
jobs in the DEDUPE/RECONCILE phase, preserving ALL provenance.

Canonical identity follows the Phase 0.9 priority (via the shared
``identity_token`` normalizer), NOT a URL:

    1. a verified official requisition / source job id WITH company/source
       context (``reqid::{company}::{source_family}::{job_id}``); else
    2. normalized company + title + location.

URL is provenance, NEVER a cross-source identity component — so the same job seen
through two different source URLs collapses to ONE canonical job with TWO source
observations, while a NEW requisition id (even with the same title/location) is a
distinct canonical job (a probable repost / new vacancy), never blindly merged.

Observation-event identity is RUN-scoped (keyed by the staged observation id):
retrying the same page/attempt is idempotent (one event), but observing the same
source job again in a LATER run creates a new observation event against the same
canonical job — so first_seen/last_seen can advance correctly across runs.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from atlas.data_integrity.normalizers import identity_token


@dataclass
class CanonicalizeResult:
    observations_processed: int = 0
    canonical_created: int = 0
    canonical_updated: int = 0
    observations_added: int = 0
    cross_source_duplicates: int = 0


def _identity_key(row) -> str:
    """Phase 0.9 canonical identity — URL-INDEPENDENT (URL is provenance, never a
    cross-source identity component). Two observations of the same posting seen
    through different source URLs share this key and collapse to one canonical
    job; a repost with a DIFFERENT posted date gets a distinct key and is never
    blindly merged. Built from the shared ``identity_token`` normalizer over
    company + title + location, plus the posted date so a repost stays distinct."""
    company = identity_token(row["company"])
    title = identity_token(row["title"])
    location = identity_token(row["location"])
    posted = (row["posted_at"] or "").strip()
    return f"ctl::{company}::{title}::{location}::{posted}"


def canonicalize_run(store, run_id: str) -> CanonicalizeResult:
    """Resolve all STAGED raw observations for ``run_id`` into canonical jobs."""
    result = CanonicalizeResult()
    staged = store.list_raw_observations(run_id, processing_status="STAGED")
    groups: dict[str, list] = {}
    for row in staged:
        groups.setdefault(_identity_key(row), []).append(row)

    for identity_key, rows in groups.items():
        canonical_id = "canon::" + hashlib.sha256(identity_key.encode("utf-8")).hexdigest()[:16]
        rep = rows[0]
        # Content hash of the representative observation (used for change
        # detection on the canonical row, NOT for identity).
        content_hash = rep["content_hash"]
        outcome = store.upsert_canonical_job(
            canonical_id,
            company=rep["company"],
            job_id=rep["source_job_id"],
            role=rep["title"],
            location=rep["location"],
            status="UNKNOWN",
            source_url=rep["source_url"],
            official_apply_url=rep["canonical_url"],
            content_hash=content_hash,
            payload={"lane": rep["lane"], "run_id": run_id, "identity_key": identity_key},
        )
        if outcome == "created":
            result.canonical_created += 1
        elif outcome == "updated":
            result.canonical_updated += 1

        seen_source_identity: set[str] = set()
        distinct_sources: set[str] = set()
        for row in rows:
            source_identity = row["source_identity"] or row["observation_id"]
            distinct_sources.add(row["source_instance"])
            if source_identity not in seen_source_identity:
                seen_source_identity.add(source_identity)
                # RUN-scoped event id (the staged observation id already encodes
                # the run) so a later-run re-observation is a NEW event, while a
                # same-run retry of the same source is idempotent.
                added = store.add_observation(
                    f"jobobs::{canonical_id}::{row['observation_id']}",
                    canonical_id,
                    row["observation_id"],
                    discovery_source=row["source_family"],
                    observed_status=row["is_active"],
                    content_hash=row["content_hash"],
                    adapter_version=row["adapter_version"] or "",
                    parser_version=row["parser_version"] or "",
                    provenance={
                        "source_instance": row["source_instance"],
                        "coverage_id": row["coverage_id"],
                        "query_signature": row["query_signature"],
                        "attempt_id": row["attempt_id"],
                        "run_id": run_id,
                    },
                )
                if added:
                    result.observations_added += 1
            store.mark_raw_observation_processed(row["observation_id"], canonical_id)
            result.observations_processed += 1
        if len(distinct_sources) > 1:
            result.cross_source_duplicates += 1

    return result


__all__ = ["canonicalize_run", "CanonicalizeResult"]
