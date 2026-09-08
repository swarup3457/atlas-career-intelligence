"""Canonicalization / reconciliation of staged raw discovery observations
(Phase 1B.1, build spec 12 / P0-15).

Raw discoveries are staged (never written to ``canonical_jobs`` directly)
during DISCOVER. This module resolves those staged observations into canonical
jobs in the DEDUPE/RECONCILE phase, preserving ALL provenance:

    * observations that share a source-independent dedupe key collapse to ONE
      canonical job (cross-source duplicate) with MULTIPLE observations;
    * a duplicate SAME-source observation is idempotent (deduped by source
      identity — it never inflates the observation count);
    * a probable repost (same role, different posted date) has a distinct
      dedupe key and is NEVER silently merged.

Canonicalization is idempotent: re-running only processes still-STAGED rows,
and canonical/observation ids are deterministic, so a resume never duplicates
canonical jobs, observations, or report content.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class CanonicalizeResult:
    observations_processed: int = 0
    canonical_created: int = 0
    canonical_updated: int = 0
    observations_added: int = 0
    cross_source_duplicates: int = 0


def canonicalize_run(store, run_id: str) -> CanonicalizeResult:
    """Resolve all STAGED raw observations for ``run_id`` into canonical jobs."""
    result = CanonicalizeResult()
    staged = store.list_raw_observations(run_id, processing_status="STAGED")
    groups: dict[str, list] = {}
    for row in staged:
        groups.setdefault(row["content_hash"], []).append(row)

    for content_hash, rows in groups.items():
        canonical_id = f"canon::{content_hash[:16]}"
        rep = rows[0]
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
            payload={"lane": rep["lane"], "run_id": run_id},
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
                added = store.add_observation(
                    f"jobobs::{canonical_id}::{source_identity}",
                    canonical_id,
                    row["observation_id"],
                    discovery_source=row["source_family"],
                    observed_status=row["is_active"],
                    content_hash=content_hash,
                    adapter_version=row["adapter_version"] or "",
                    parser_version=row["parser_version"] or "",
                    provenance={
                        "source_instance": row["source_instance"],
                        "coverage_id": row["coverage_id"],
                        "query_signature": row["query_signature"],
                        "attempt_id": row["attempt_id"],
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
