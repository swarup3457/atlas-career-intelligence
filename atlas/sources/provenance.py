"""Atlas discovery→canonical provenance bridge (Phase 1A).

Integrates normalized :class:`DiscoveryResult` observations into the proven
Phase 0.9 canonical layer (``canonical_jobs`` + ``job_observations``) using
the EXISTING :class:`IdentityResolver` — the identity engine is reused, not
replaced. Guarantees:

    * the same source observation seen twice → one canonical job, one
      observation (idempotent, no duplicate);
    * the same job seen via multiple sources → one canonical job with
      multiple provenance observations (evidence is never collapsed);
    * a probable repost is detected and recorded, never silently merged.

Global dedupe/canonicalization happens HERE (after adapters normalize), not
inside adapters — adapters may only suppress obvious in-batch duplicates.
"""

from __future__ import annotations

import datetime
import hashlib
from dataclasses import dataclass, field
from typing import Iterable, Optional

from atlas.data_integrity.identity import IdentityResolver, Relationship
from atlas.data_integrity.mapping import MappingConfig
from atlas.data_integrity.records import FieldValue, IngestionRecord, Provenance
from atlas.sources.models import ActiveState, DiscoveryResult


def _utcnow_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def discovery_mapping() -> MappingConfig:
    """Identity mapping for discovery results: a job's identity is its
    normalized (company, title, location); fallback (company, title). Only
    identity + observational fields are fed to the resolver, so multi-source
    differences (ids, work mode) never register as content conflicts."""
    return MappingConfig.from_dict(
        {
            "version": "1",
            "entities": [
                {
                    "entity_type": "job",
                    "sheets": ["discovery"],
                    "identity": ["company", "title", "location"],
                    "identity_fallback": ["company", "title"],
                    "fields": [
                        {"canonical": "company", "normalizer": "company"},
                        {"canonical": "title", "normalizer": "text"},
                        {"canonical": "location", "normalizer": "text"},
                        {"canonical": "discovery_source", "normalizer": "text"},
                        {"canonical": "source_url", "normalizer": "url"},
                        {"canonical": "first_seen", "normalizer": "text"},
                    ],
                }
            ],
        }
    )


def _fv(canonical: str, value) -> FieldValue:
    return FieldValue(canonical=canonical, raw=value, normalized=value)


def discovery_to_record(result: DiscoveryResult, index: int) -> IngestionRecord:
    """Convert a DiscoveryResult into an IngestionRecord for the identity
    resolver. record_id is unique per source observation."""
    record_id = "|".join(
        [
            result.source_type.value,
            result.source_instance,
            result.source_job_id or result.canonical_url or result.source_url or f"idx{index}",
        ]
    )
    provenance = Provenance(
        source_file=f"discovery:{result.source_instance}",
        sheet_name="discovery",
        entity_type="job",
        row_index=index + 1,
        ingested_at=result.discovered_at or _utcnow_iso(),
        mapping_version="1",
    )
    fields = {
        "company": _fv("company", result.company),
        "title": _fv("title", result.title),
        "location": _fv("location", result.location),
        "discovery_source": _fv("discovery_source", result.source_instance),
        "source_url": _fv("source_url", result.source_url),
        "first_seen": _fv("first_seen", result.discovered_at),
    }
    return IngestionRecord(
        record_id=record_id,
        entity_type="job",
        provenance=provenance,
        fields=fields,
    )


def _observation_id(result: DiscoveryResult) -> str:
    anchor = result.source_identity() or hashlib.sha256(
        (str(result.company) + str(result.title) + str(result.location) + result.source_instance).encode("utf-8")
    ).hexdigest()
    return hashlib.sha256(anchor.encode("utf-8")).hexdigest()[:32]


def _observed_status(result: DiscoveryResult) -> str:
    return {
        ActiveState.ACTIVE: "ACTIVE",
        ActiveState.INACTIVE: "CLOSED",
        ActiveState.UNKNOWN: "UNKNOWN",
    }[result.is_active]


@dataclass
class IngestionSummary:
    canonical_created: int = 0
    canonical_updated: int = 0
    canonical_unchanged: int = 0
    observations_added: int = 0
    observations_duplicate: int = 0
    clusters: int = 0
    reposts: int = 0
    multi_source: int = 0
    canonical_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "canonical_created": self.canonical_created,
            "canonical_updated": self.canonical_updated,
            "canonical_unchanged": self.canonical_unchanged,
            "observations_added": self.observations_added,
            "observations_duplicate": self.observations_duplicate,
            "clusters": self.clusters,
            "reposts": self.reposts,
            "multi_source": self.multi_source,
            "canonical_ids": list(self.canonical_ids),
        }


def ingest_discovery_results(
    store,
    results: Iterable[DiscoveryResult],
    *,
    mapping: Optional[MappingConfig] = None,
) -> IngestionSummary:
    """Canonicalize + persist discovery results via the identity engine.

    Idempotent: re-ingesting the same results changes nothing (canonical
    content hash unchanged, observation ids stable)."""
    results = list(results)
    mapping = mapping or discovery_mapping()
    summary = IngestionSummary()
    if not results:
        return summary

    records = [discovery_to_record(r, i) for i, r in enumerate(results)]
    by_record_id = {rec.record_id: res for rec, res in zip(records, results)}
    resolution = IdentityResolver(mapping).resolve(records)
    summary.clusters = len(resolution.clusters)

    # Group the original results by their resolved cluster identity so each
    # cluster maps to exactly one canonical job.
    record_to_cluster: dict[str, str] = {}
    for cluster in resolution.clusters:
        for rid in cluster.record_ids:
            record_to_cluster[rid] = cluster.identity_key
    # Unidentified records each become their own singleton canonical id.
    for rid in resolution.unidentified:
        record_to_cluster[rid] = f"job::unidentified::{rid}"

    clustered: dict[str, list[DiscoveryResult]] = {}
    for rec in records:
        key = record_to_cluster[rec.record_id]
        clustered.setdefault(key, []).append(by_record_id[rec.record_id])

    cluster_rel = {c.identity_key: c.relationships for c in resolution.clusters}

    for canonical_id in sorted(clustered):
        members = clustered[canonical_id]
        representative = _pick_representative(members)
        rels = cluster_rel.get(canonical_id, set())
        is_repost = Relationship.REPOST in rels
        is_multi = Relationship.MULTI_SOURCE in rels
        if is_repost:
            summary.reposts += 1
        if is_multi:
            summary.multi_source += 1

        official_apply_url = next(
            (m.canonical_url for m in members if m.canonical_url), representative.source_url
        )
        outcome = store.upsert_canonical_job(
            canonical_id,
            company=representative.company,
            role=representative.title,
            location=representative.location,
            status=_observed_status(representative),
            source_url=representative.source_url,
            official_apply_url=official_apply_url,
            content_hash=representative.content_hash(),
            payload={
                "multi_source": is_multi,
                "repost_suspected": is_repost,
                "observation_count_hint": len(members),
            },
        )
        if outcome == "created":
            summary.canonical_created += 1
        elif outcome == "updated":
            summary.canonical_updated += 1
        else:
            summary.canonical_unchanged += 1
        summary.canonical_ids.append(canonical_id)

        # Record a repost relationship WITHOUT collapsing the distinct
        # observations (each source observation is retained below).
        if is_repost:
            store.record_status_change(
                history_id=hashlib.sha256(("repost:" + canonical_id).encode("utf-8")).hexdigest()[:32],
                canonical_id=canonical_id,
                to_status="REPOST_SUSPECTED",
                axis="repost",
                reason="Identity engine flagged a probable repost (distinct observation timestamps).",
            )

        for member in members:
            obs_id = _observation_id(member)
            added = store.add_observation(
                obs_id,
                canonical_id,
                record_id=member.source_identity() or obs_id,
                discovery_source=member.source_instance,
                observed_status=_observed_status(member),
                observed_at=member.discovered_at,
                content_hash=member.content_hash(),
                adapter_version=member.adapter_version,
                parser_version=member.parser_version,
                provenance={
                    "source_type": member.source_type.value,
                    "source_url": member.source_url,
                    "verification_level": member.verification_level.value,
                },
            )
            if added:
                summary.observations_added += 1
            else:
                summary.observations_duplicate += 1

    return summary


def _pick_representative(members: list[DiscoveryResult]) -> DiscoveryResult:
    """Choose the most authoritative observation to seed canonical fields:
    prefer official ATS sources, then the richest (has description)."""
    def score(r: DiscoveryResult) -> tuple:
        official = 1 if r.source_type.value.startswith("ATS_") or r.source_type.value == "COMPANY_CAREER" else 0
        has_desc = 1 if r.description else 0
        return (official, has_desc, r.source_instance)

    return sorted(members, key=score, reverse=True)[0]


__all__ = ["discovery_mapping", "discovery_to_record", "ingest_discovery_results", "IngestionSummary"]
