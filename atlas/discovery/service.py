from __future__ import annotations

from dataclasses import dataclass
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
