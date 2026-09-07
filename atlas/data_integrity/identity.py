"""Candidate identity relationships.

Groups ingestion records that refer to the *same real-world thing* using
the schema's ``identity_fields`` (with a configurable fallback when the
primary key, e.g. ``job_id``, is missing). Within each identity cluster it
classifies the relationships that matter for reconciliation:

* **EXACT_DUPLICATE** — identical normalized payload.
* **REPOST**          — same identity, later observation timestamps.
* **MULTI_SOURCE**    — same identity discovered via different sources.
* **CONFLICT**        — same identity, contradictory normalized values.

The output is deterministic (stable ordering) so reports and tests are
reproducible.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any, Optional

from atlas.data_integrity.mapping import MappingConfig
from atlas.data_integrity.normalizers import identity_token
from atlas.data_integrity.records import IngestionRecord


class Relationship(str, enum.Enum):
    SINGLETON = "SINGLETON"
    EXACT_DUPLICATE = "EXACT_DUPLICATE"
    REPOST = "REPOST"
    MULTI_SOURCE = "MULTI_SOURCE"
    CONFLICT = "CONFLICT"


# Fields that legitimately differ across observations of the same job and
# therefore must NOT be treated as a data conflict.
_OBSERVATIONAL_FIELDS = frozenset(
    {
        "first_seen",
        "last_verified",
        "posted_date",
        "freshness",
        "live_status",
        "discovery_source",
        "source_url",
        "notes",
        "closure_reason",
        "verification_status",
        "checked_at",
    }
)


@dataclass
class IdentityCluster:
    identity_key: str
    entity_type: str
    record_ids: list[str] = field(default_factory=list)
    relationships: set[Relationship] = field(default_factory=set)
    sources: list[str] = field(default_factory=list)
    conflicts: dict[str, list[Any]] = field(default_factory=dict)  # field -> distinct values

    @property
    def size(self) -> int:
        return len(self.record_ids)

    def to_dict(self) -> dict[str, Any]:
        return {
            "identity_key": self.identity_key,
            "entity_type": self.entity_type,
            "record_ids": list(self.record_ids),
            "relationships": sorted(r.value for r in self.relationships),
            "sources": list(self.sources),
            "conflicts": {k: list(v) for k, v in self.conflicts.items()},
            "size": self.size,
        }


def compute_identity_key(record: IngestionRecord, mapping: MappingConfig) -> Optional[str]:
    """Deterministic identity key for a record, or None if unidentifiable."""
    schema = mapping.schema_for(record.entity_type)
    if schema is None:
        return None

    def build(fields: tuple[str, ...]) -> Optional[str]:
        parts: list[str] = []
        for canonical in fields:
            token = identity_token(record.value(canonical))
            if token == "":
                return None
            parts.append(token)
        if not parts:
            return None
        return record.entity_type + "::" + "|".join(parts)

    key = build(schema.identity_fields)
    if key is not None:
        return key
    if schema.identity_fallback:
        return build(schema.identity_fallback)
    return None


@dataclass
class IdentityResolution:
    clusters: list[IdentityCluster] = field(default_factory=list)
    unidentified: list[str] = field(default_factory=list)  # record_ids

    def by_key(self) -> dict[str, IdentityCluster]:
        return {c.identity_key: c for c in self.clusters}

    def relationship_counts(self) -> dict[str, int]:
        out = {r.value: 0 for r in Relationship}
        for c in self.clusters:
            for r in c.relationships:
                out[r.value] += 1
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "cluster_count": len(self.clusters),
            "unidentified": list(self.unidentified),
            "relationship_counts": self.relationship_counts(),
            "clusters": [c.to_dict() for c in self.clusters],
        }


class IdentityResolver:
    def __init__(self, mapping: MappingConfig):
        self.mapping = mapping

    def resolve(self, records: list[IngestionRecord]) -> IdentityResolution:
        # Assign identity keys (mutates records so downstream stages agree).
        buckets: dict[str, list[IngestionRecord]] = {}
        unidentified: list[str] = []
        for rec in records:
            key = compute_identity_key(rec, self.mapping)
            rec.identity_key = key
            if key is None:
                unidentified.append(rec.record_id)
                continue
            buckets.setdefault(key, []).append(rec)

        clusters: list[IdentityCluster] = []
        for key in sorted(buckets):
            group = buckets[key]
            cluster = IdentityCluster(
                identity_key=key,
                entity_type=group[0].entity_type,
                record_ids=[r.record_id for r in group],
            )
            self._classify(group, cluster)
            clusters.append(cluster)

        return IdentityResolution(clusters=clusters, unidentified=sorted(unidentified))

    # ------------------------------------------------------------------
    def _classify(self, group: list[IngestionRecord], cluster: IdentityCluster) -> None:
        if len(group) == 1:
            cluster.relationships.add(Relationship.SINGLETON)
            src = group[0].value("discovery_source") or group[0].value("source")
            if src:
                cluster.sources.append(str(src))
            return

        # Collect distinct sources.
        sources: list[str] = []
        for rec in group:
            src = rec.value("discovery_source") or rec.value("source")
            if src is not None and str(src) not in sources:
                sources.append(str(src))
        cluster.sources = sources
        if len(sources) > 1:
            cluster.relationships.add(Relationship.MULTI_SOURCE)

        # Exact duplicate: identical stable payloads (excluding observational).
        signatures = {self._stable_signature(r) for r in group}
        if len(signatures) == 1:
            cluster.relationships.add(Relationship.EXACT_DUPLICATE)

        # Repost: distinct observation timestamps.
        observed = [
            r.value("last_verified") or r.value("first_seen") or r.value("checked_at")
            for r in group
        ]
        distinct_obs = {o for o in observed if o is not None}
        if len(distinct_obs) > 1:
            cluster.relationships.add(Relationship.REPOST)

        # Conflict: a *substantive* (non-observational) field disagrees.
        conflicts = self._detect_conflicts(group)
        if conflicts:
            cluster.conflicts = conflicts
            cluster.relationships.add(Relationship.CONFLICT)

    @staticmethod
    def _stable_signature(record: IngestionRecord) -> tuple:
        items = []
        for canonical in sorted(record.fields):
            if canonical in _OBSERVATIONAL_FIELDS:
                continue
            items.append((canonical, record.value(canonical)))
        return tuple(items)

    @staticmethod
    def _detect_conflicts(group: list[IngestionRecord]) -> dict[str, list[Any]]:
        all_fields: set[str] = set()
        for rec in group:
            all_fields.update(rec.fields.keys())
        conflicts: dict[str, list[Any]] = {}
        for canonical in sorted(all_fields):
            if canonical in _OBSERVATIONAL_FIELDS:
                continue
            values: list[Any] = []
            for rec in group:
                v = rec.value(canonical)
                if v in (None, ""):
                    continue
                if v not in values:
                    values.append(v)
            if len(values) > 1:
                conflicts[canonical] = values
        return conflicts
