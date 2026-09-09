"""Real detail hydration over current-run observations (Phase 1C-A corrective,
build spec 14).

Replaces the old one-representative-posting telemetry probe. This hydrator:

    * operates on the CURRENT run's staged observations selected by a
      deterministic policy (families that expose a DETAIL capability, in a
      stable order, up to a bounded budget);
    * uses the SourceRegistry to build the adapter, the SHARED
      RateLimitedExecutor to pace the fetch, and the CENTRAL retry policy to
      decide retry vs. terminal — it never bypasses the common executor;
    * uses the adapter-emitted detail anchor (the Workday ``externalPath`` /
      the Greenhouse/Lever id or url), NEVER the requisition id for Workday;
    * merges the hydrated fields into a NEW, immutable observation version
      (``processing_status='HYDRATED'``) that references the original — it never
      destructively overwrites the original observation or its provenance;
    * resumes after interruption without repeating a completed detail call (a
      hydrated child is skipped when its hydrated observation already exists).
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from typing import Mapping, Optional

from atlas.candidate.requirements import extract_requirements
from atlas.models import ErrorCategory
from atlas.orchestration import retry
from atlas.sources.adapter import AdapterError
from atlas.sources.executor import RateLimitedExecutor
from atlas.sources.models import Capability, DetailRequest, SourceInstance
from atlas.sources.registry import SourceRegistry
from atlas.sources.untrusted import redact_secrets

# Families that expose a per-job detail endpoint (Ashby correctly has none).
_DETAIL_FAMILIES = frozenset({"greenhouse", "lever", "workday", "company_career"})

# Bounded, redacted description fragment kept on the hydrated observation so no
# unbounded HTML enters the store (matches the SEARCH-stage bound).
_MAX_DETAIL_TEXT = 2000


def _bounded_text(text: Optional[str]) -> Optional[str]:
    if not text:
        return text
    red = redact_secrets(str(text))
    return red[:_MAX_DETAIL_TEXT] + (" …[truncated]" if len(red) > _MAX_DETAIL_TEXT else "")


def _utcnow() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


@dataclass
class HydrationResult:
    selected: int = 0
    hydrated: int = 0
    skipped_existing: int = 0
    failed: int = 0


class DetailHydrator:
    def __init__(
        self,
        store,
        registry: SourceRegistry,
        instances: Mapping[str, SourceInstance],
        *,
        run_id: str,
        executor: Optional[RateLimitedExecutor] = None,
        retry_budget: int = 2,
        max_details: int = 25,
    ):
        self.store = store
        self.registry = registry
        self.instances = dict(instances)
        self.run_id = run_id
        self.executor = executor or RateLimitedExecutor()
        self.retry_budget = retry_budget
        self.max_details = max_details
        self._adapters: dict[str, object] = {}

    def _adapter(self, instance_id: str):
        if instance_id not in self._adapters:
            self._adapters[instance_id] = self.registry.create(self.instances[instance_id])
        return self._adapters[instance_id]

    def _select(self) -> list:
        """Deterministic selection of current-run observations to hydrate:
        original SEARCH observations whose family exposes DETAIL and that carry a
        usable detail anchor, in stable observation order. A DETAIL revision is
        never itself re-hydrated."""
        out = []
        for row in self.store.list_raw_observations(self.run_id):
            if row["revision_kind"] == "DETAIL":
                continue
            fam = (row["source_family"] or "").lower()
            if fam not in _DETAIL_FAMILIES:
                continue
            inst = self.instances.get(row["source_instance"])
            if inst is None:
                continue
            anchor_url = row["canonical_url"] or row["source_url"]
            anchor_id = row["source_job_id"]
            if not (anchor_url or anchor_id):
                continue
            out.append(row)
            if len(out) >= self.max_details:
                break
        return out

    def hydrate(self) -> HydrationResult:
        result = HydrationResult()
        for row in self._select():
            result.selected += 1
            hydrated_id = f"{row['observation_id']}::detail"
            # Resume: a completed detail call is never repeated.
            if self.store.get_raw_observation(hydrated_id) is not None:
                result.skipped_existing += 1
                continue
            inst = self.instances[row["source_instance"]]
            fam = (row["source_family"] or "").lower()
            adapter = self._adapter(row["source_instance"])
            if not adapter.supports(Capability.DETAIL):
                continue
            # Use the adapter-emitted detail anchor. For Workday the anchor is the
            # externalPath carried on the canonical URL (never the requisition id).
            req = self._detail_request(fam, row)
            if req is None:
                continue
            attempt = 0
            while True:
                attempt += 1
                try:
                    detail = self.executor.run_detail(adapter, req)
                    break
                except AdapterError as exc:
                    decision = retry.evaluate(exc.category, attempt, self.retry_budget)
                    if decision.should_retry:
                        continue
                    result.failed += 1
                    detail = None
                    break
                except Exception:  # noqa: BLE001 - a parse/other fault is terminal for this item
                    result.failed += 1
                    detail = None
                    break
            if detail is None:
                continue
            self._store_hydrated(hydrated_id, row, detail)
            result.hydrated += 1
        return result

    def _detail_request(self, family: str, row) -> Optional[DetailRequest]:
        anchor_url = row["canonical_url"] or row["source_url"]
        anchor_id = row["source_job_id"]
        try:
            if family in ("workday", "company_career"):
                # The detail page IS the public job URL; pass the URL so the
                # adapter fetches/extracts it (never an opaque requisition id).
                if anchor_url:
                    return DetailRequest(url=anchor_url)
                return None
            if anchor_id:
                return DetailRequest(source_job_id=anchor_id)
            if anchor_url:
                return DetailRequest(url=anchor_url)
        except ValueError:
            return None
        return None

    def _store_hydrated(self, hydrated_id: str, row, detail) -> None:
        d = detail.to_dict()
        # Extract requirements from the FULL hydrated description (before it is
        # bounded for storage) so candidate ranking has real mandatory/preferred
        # requirements rather than a title-only guess (§11). Empty when the
        # description yields nothing recognizable — never invented.
        full_description = d.get("description") or ""
        extraction = extract_requirements(
            full_description,
            skills=tuple(d.get("skills") or ()),
            experience_text=d.get("experience_text") or "",
        )
        # A new IMMUTABLE observation REVISION (build spec 6): it references its
        # source SEARCH observation via parent_observation_id and revision_kind
        # 'DETAIL' — canonical_id is NEVER misused to hold an observation id. It
        # is left STAGED so canonicalization SEES and links it to the SAME
        # canonical job as its parent (they share the official requisition), and
        # the report then selects this higher-evidence revision. The original
        # SEARCH observation is untouched, preserving its provenance; a failed
        # detail hydration therefore never destroys the original search evidence.
        self.store.stage_raw_observation(
            hydrated_id, self.run_id, row["source_instance"], row["content_hash"],
            coverage_id=row["coverage_id"], attempt_id=row["attempt_id"],
            query_signature=row["query_signature"], source_family=row["source_family"],
            source_job_id=d.get("source_job_id") or row["source_job_id"],
            source_url=d.get("source_url"), canonical_url=d.get("canonical_url"),
            company=d.get("company"), title=d.get("title"), location=d.get("location"),
            lane=row["lane"], posted_at=d.get("posted_at"), is_active=str(d.get("is_active", "")),
            source_identity=row["source_identity"], adapter_version=d.get("adapter_version", ""),
            parser_version=d.get("parser_version", ""),
            observed_at=_utcnow(),
            parent_observation_id=row["observation_id"], revision_kind="DETAIL",
            detail={"hydrated_from": row["observation_id"], "detail_provenance": dict(d.get("provenance") or {}),
                    "description_fragment": _bounded_text(full_description),
                    "description_len": len(full_description) if full_description else 0,
                    "skills": list(d.get("skills") or []),
                    "work_mode": d.get("work_mode"),
                    "experience_text": _bounded_text(d.get("experience_text")),
                    "mandatory_requirements": list(extraction.mandatory),
                    "preferred_requirements": list(extraction.preferred),
                    "requirement_experience_text": extraction.experience_text,
                    "eligibility_text": extraction.eligibility_text,
                    "requisition_id": (d.get("provenance") or {}).get("requisition_id"),
                    "date_provenance": (d.get("provenance") or {}).get("date_provenance"),
                    "verification_level": d.get("verification_level")},
        )


__all__ = ["DetailHydrator", "HydrationResult"]
