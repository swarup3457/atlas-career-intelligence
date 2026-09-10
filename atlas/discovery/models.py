from __future__ import annotations

import dataclasses
import hashlib
import re
from typing import Any


@dataclasses.dataclass(frozen=True)
class JobLead:
    provider: str
    mechanism: str
    query: str
    title: str
    company: str
    location: str
    posted_date: str | None
    source_url: str
    canonical_url: str | None = None
    company_domain: str | None = None
    category: str | None = None
    seniority: str | None = None
    skills: tuple[str, ...] = ()
    description_available: bool = False
    provenance_at: str = ""
    provider_id: str | None = None
    raw_reference: str | None = None
    source_health: str = "OK"
    prefilter_status: str = "PENDING"
    prefilter_reason: str = ""

    @property
    def identity(self) -> str:
        if self.canonical_url:
            return "url:" + self.canonical_url.lower().rstrip("/")
        if self.provider_id:
            return f"provider:{self.provider}:{self.provider_id}"
        key = "|".join(_norm(value) for value in (self.company, self.title, self.location, self.posted_date or ""))
        return "lead:" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]


@dataclasses.dataclass(frozen=True)
class DiscoveryBatch:
    provider: str
    status: str
    leads: tuple[JobLead, ...] = ()
    queries: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    source_health: dict[str, Any] = dataclasses.field(default_factory=dict)


def _norm(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()
