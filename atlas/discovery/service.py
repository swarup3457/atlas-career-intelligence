from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .canonicalize import deduplicate
from .models import DiscoveryBatch, JobLead
from .prefilter import prefilter
from .provider import DiscoveryProvider


@dataclass(frozen=True)
class DiscoveryResult:
    batches: tuple[DiscoveryBatch, ...]
    raw_leads: tuple[JobLead, ...]
    deduplicated_leads: tuple[JobLead, ...]
    queued_leads: tuple[JobLead, ...]
    errors: tuple[str, ...]


class DiscoveryService:
    def __init__(self, providers: Iterable[DiscoveryProvider]):
        self.providers = tuple(providers)

    def run(self, queries: list[str]) -> DiscoveryResult:
        batches: list[DiscoveryBatch] = []
        errors: list[str] = []
        raw: list[JobLead] = []
        for provider in self.providers:
            try:
                batch = provider.discover(queries)
            except Exception as exc:
                errors.append(f"{getattr(provider, 'name', type(provider).__name__)}: {exc}")
                continue
            batches.append(batch)
            raw.extend(batch.leads)
            errors.extend(batch.errors)
        unique = deduplicate(raw)
        filtered = [prefilter(lead) for lead in unique]
        return DiscoveryResult(tuple(batches), tuple(raw), tuple(filtered), tuple(lead for lead in filtered if lead.prefilter_status == "QUEUED"), tuple(errors))


def write_discovery_evidence(evidence_root: Path, result: DiscoveryResult, queries: list[str]) -> Path:
    """Persist a discovery run as the evidence JSONs the run workbook reads (§9)."""
    evidence_root = Path(evidence_root)
    evidence_root.mkdir(parents=True, exist_ok=True)
    (evidence_root / "freehire_raw.json").write_text(json.dumps([lead.to_record() for lead in result.raw_leads]), encoding="utf-8")
    (evidence_root / "freehire_queued.json").write_text(json.dumps([lead.to_record() for lead in result.queued_leads]), encoding="utf-8")
    (evidence_root / "freehire_health.json").write_text(json.dumps([{"provider": batch.provider, "status": batch.status, "source_health": batch.source_health} for batch in result.batches]), encoding="utf-8")
    (evidence_root / "discovery_queries.json").write_text(json.dumps({"queries": list(queries)}), encoding="utf-8")
    if result.errors:
        (evidence_root / "discovery_errors.json").write_text(json.dumps(list(result.errors)), encoding="utf-8")
    return evidence_root


def plan_verification_batches(leads: Iterable[JobLead], *, max_leads: int = 8) -> list[tuple[JobLead, ...]]:
    """Chunk queued leads into verification batches of at most ``max_leads`` (§7, §11).

    Batches may span companies; a lead is never dropped.
    """
    if max_leads < 1:
        raise ValueError("max_leads must be >= 1")
    ordered = list(leads)
    return [tuple(ordered[index:index + max_leads]) for index in range(0, len(ordered), max_leads)]
