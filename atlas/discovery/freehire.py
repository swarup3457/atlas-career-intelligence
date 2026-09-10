from __future__ import annotations

import datetime
import json
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from atlas.sources.http_client import HttpRequest, ReadOnlyHttpClient

from .models import DiscoveryBatch, JobLead


class FreehireProvider:
    name = "freehire"

    def __init__(self, *, http_client: Any | None = None, api_base: str | None = None, output_dir: Path | None = None):
        self.api_base = (api_base or os.getenv("FREEHIRE_API_URL", "https://freehire.me")).rstrip("/")
        self.http = http_client or ReadOnlyHttpClient(request_budget=40, accept="application/json")
        self.output_dir = Path(output_dir) if output_dir else None

    def facets(self, query: str = "") -> dict[str, Any]:
        url = self.api_base + "/api/v1/jobs/facets"
        if query:
            url += "?" + urlencode({"q": query})
        response = self.http.fetch(HttpRequest(url, headers={"Accept": "application/json"}))
        return json.loads(response.text())

    def discover(self, queries: list[str]) -> DiscoveryBatch:
        leads: list[JobLead] = []
        errors: list[str] = []
        captured: list[dict[str, Any]] = []
        today = datetime.date.today().isoformat()
        for query in queries:
            params = {"q": query, "limit": "20", "offset": "0", "include_description": "true", "description_format": "text", "countries": "in", "posted_within_days": "45"}
            url = self.api_base + "/api/v1/agent/jobs/search?" + urlencode(params)
            try:
                response = self.http.fetch(HttpRequest(url, headers={"Accept": "application/json"}))
                payload = json.loads(response.text())
                raw = payload.get("data", []) if isinstance(payload, dict) else []
                captured.append({"query": query, "url": url, "count": len(raw), "total": (payload.get("meta") or {}).get("total")})
                for job in raw:
                    enrichment = job.get("enrichment") or {}
                    leads.append(JobLead(provider=self.name, mechanism="public_api", query=query, title=str(job.get("title") or ""), company=str(job.get("company") or ""), location=str(job.get("location") or ""), posted_date=job.get("posted_at") or job.get("date"), source_url=str(job.get("url") or ""), canonical_url=str(job.get("url") or "") or None, company_domain=None, category=enrichment.get("category"), seniority=enrichment.get("seniority"), skills=tuple(str(x) for x in (job.get("skills") or [])), description_available=bool(str(job.get("description") or "").strip()), provenance_at=today, provider_id=str(job.get("public_slug") or job.get("id") or "") or None, raw_reference=str(job.get("public_slug") or job.get("id") or "") or None))
            except Exception as exc:  # provider failure is isolated
                errors.append(f"{query}: {type(exc).__name__}: {exc}")
        status = "OK" if leads or not errors else "DEGRADED"
        return DiscoveryBatch(self.name, status, tuple(leads), tuple(queries), tuple(errors), {"queries": captured, "errors": errors, "as_of": today})
