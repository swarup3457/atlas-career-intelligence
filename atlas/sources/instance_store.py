"""SourceInstance ↔ SQLite bridge (Phase 1B, build spec 7.4).

Keeps the low-level :class:`atlas.persistence.sqlite.StateStore` free of any
``atlas.sources`` imports while giving callers a typed way to persist and
reload first-class :class:`SourceInstance` objects idempotently, and to
connect company↔source coverage through stable instance IDs.
"""

from __future__ import annotations

from typing import Optional

from atlas.sources.models import (
    Capability,
    SourceFamily,
    SourceInstance,
    SourceType,
)


def save_instance(store, instance: SourceInstance, *, provenance: Optional[dict] = None) -> None:
    """Persist a source instance idempotently (keyed by instance_id). A
    re-save with identical fields does not change identity — only the
    ``updated_at`` timestamp. ``auth_ref`` is stored as a reference name
    only, never a secret value."""
    store.upsert_source_instance(
        instance.instance_id,
        adapter_key=instance.adapter_key.value,
        source_type=instance.source_type.value,
        category=instance.category.value,
        display_name=instance.display_name,
        base_url=instance.base_url,
        tenant=instance.tenant,
        site=instance.site,
        company_id=instance.company_id,
        enabled=instance.enabled,
        lifecycle_state=instance.lifecycle_state,
        capability_additions=[c.value for c in instance.capability_overrides],
        capability_removals=[c.value for c in instance.capability_removals],
        rate_policy_ref=instance.rate_policy,
        auth_ref=instance.auth_ref,
        provenance=provenance or dict(instance.metadata),
    )


def _row_to_instance(row) -> SourceInstance:
    import json

    additions = frozenset(
        Capability(c) for c in json.loads(row["capability_additions_json"] or "[]")
    )
    removals = frozenset(
        Capability(c) for c in json.loads(row["capability_removals_json"] or "[]")
    )
    return SourceInstance(
        instance_id=row["instance_id"],
        source_type=SourceType(row["source_type"]),
        source_family=SourceFamily(row["adapter_key"]),
        display_name=row["display_name"] or "",
        base_url=row["base_url"],
        tenant=row["tenant"],
        site=row["site"],
        company_id=row["company_id"],
        enabled=bool(row["enabled"]),
        lifecycle_state=row["lifecycle_state"] or "ACTIVE",
        capability_overrides=additions,
        capability_removals=removals,
        rate_policy=row["rate_policy_ref"],
        auth_ref=row["auth_ref"],
        metadata=json.loads(row["provenance_json"] or "{}"),
    )


def load_instance(store, instance_id: str) -> Optional[SourceInstance]:
    row = store.get_source_instance(instance_id)
    return _row_to_instance(row) if row is not None else None


def load_instances(
    store, *, company_id: Optional[str] = None, include_disabled: bool = True
) -> list[SourceInstance]:
    return [
        _row_to_instance(r)
        for r in store.list_source_instances(company_id=company_id, include_disabled=include_disabled)
    ]


__all__ = ["save_instance", "load_instance", "load_instances"]
