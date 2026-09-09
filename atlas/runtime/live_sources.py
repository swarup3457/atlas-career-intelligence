"""Live portal discovery source for the daily pipeline (Phase 1E/F recovery §6/§13).

Runs the REAL read-only portal adapters (LinkedIn guest, Naukri public) live,
follows bounded pagination, and converts each real observation into a
:class:`RankableJob` so the durable daily graph can rank real leads and publish
them through the stable production output contract. Nothing here signs in,
applies, or bypasses a challenge — a login/anti-bot/rate-limit response is
CLASSIFIED into a truthful source-health status, never a fake zero.

A portal observation becomes a RankableJob whose verification_state is
``PORTAL_CURRENT_LEAD`` (so the deterministic eligibility gate marks it DEFERRED
— a lead needs official verification before deep evaluation). Guest cards carry
no job description, so requirement lists are empty; this is truthful, not a
fabricated match.
"""

from __future__ import annotations

import datetime
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from atlas.candidate.eligibility import RankableJob
from atlas.candidate.jobkey import job_key_for
from atlas.sources.adapter import AdapterError
from atlas.sources.http_client import ReadOnlyHttpClient
from atlas.sources.models import SearchRequest, SourceFamily, WorkMode
from atlas.sources.portals import make_portal_instance
from atlas.sources.portals.registry import adapter_class_for_family

_FAMILY = {"linkedin": SourceFamily.LINKEDIN, "naukri": SourceFamily.NAUKRI}

_ISO_DATE_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")


def _parse_posted_date(text: Optional[str]) -> Optional[datetime.date]:
    if not text:
        return None
    m = _ISO_DATE_RE.search(str(text))
    if not m:
        return None
    try:
        return datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


@dataclass
class SourceRunHealth:
    family: str
    attempted: bool = False
    pages: int = 0
    raw_results: int = 0
    unique_leads: int = 0
    status: str = "NOT_REACHED"        # COMPLETED | ATTEMPTED_ZERO | ACCESS_LIMITED | AUTH_REQUIRED | RATE_LIMITED | SOURCE_UNAVAILABLE | EXTRACTION_UNRESOLVED
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "family": self.family, "attempted": self.attempted, "pages": self.pages,
            "raw_results": self.raw_results, "unique_leads": self.unique_leads,
            "status": self.status, "detail": self.detail,
        }


@dataclass
class LiveDiscoveryOutcome:
    jobs: list[RankableJob] = field(default_factory=list)
    health: list[SourceRunHealth] = field(default_factory=list)

    def health_dict(self) -> dict[str, Any]:
        return {h.family: h.to_dict() for h in self.health}


class LivePortalDiscovery:
    """Bounded, read-only live portal discovery producing RankableJobs."""

    def __init__(
        self,
        *,
        lane: str = "JAVA_BACKEND",
        location: str = "India",
        recency_days: int = 7,
        max_pages: int = 2,
        max_cards: int = 10,
        client_factory=None,
        adapter_factory=None,
    ) -> None:
        self.lane = lane
        self.location = location
        self.recency_days = recency_days
        self.max_pages = max(1, max_pages)
        self.max_cards = max(1, max_cards)
        self._client_factory = client_factory or (
            lambda: ReadOnlyHttpClient(accept="text/html,application/xhtml+xml,*/*;q=0.8",
                                       request_budget=self.max_pages + 2)
        )
        # Injectable for tests: (family_str) -> SourceAdapter. When None, the
        # real read-only portal adapter is built with a bounded HTTP client.
        self._adapter_factory = adapter_factory

    def _to_job(self, result, family: str) -> Optional[RankableJob]:
        title = (result.title or "").strip()
        anchor = (result.source_job_id or "").strip() or (result.canonical_url or "").strip()
        if not title or not anchor:
            return None
        company = (result.company or "").strip() or "(unknown)"
        location = (result.location or "").strip()
        work_mode = result.work_mode.value if isinstance(result.work_mode, WorkMode) else str(result.work_mode)
        key = job_key_for(company=company, title=title, location=location,
                          source_family=family, source_job_id=result.source_job_id, official=False)
        return RankableJob(
            job_key=key, company=company, title=title, location=location, lane=self.lane,
            work_mode=work_mode, description="",
            mandatory_requirements=(), preferred_requirements=(),
            experience_text="", eligibility_text=location,
            posted_date=_parse_posted_date(result.posted_at),
            verification_state="PORTAL_CURRENT_LEAD", has_live_official_page=False,
            is_fetchable=True, source_family=family,
            canonical_id=None, url=result.canonical_url or result.source_url,
        )

    def _query(self) -> str:
        # A concrete, policy-aligned keyword query for the lane. Kept simple and
        # bounded; the market campaign owns full policy query compilation.
        lane_terms = {
            "JAVA_BACKEND": "java backend engineer",
            "GENERAL_SOFTWARE": "software engineer",
            "JAVA_FULLSTACK": "java full stack developer",
            "REACT_FRONTEND": "react frontend developer",
            "DOTNET": "c# .net developer",
            "ENTERPRISE_HR_PAYROLL_INTEGRATION": "hr payroll integration engineer",
        }
        return lane_terms.get(self.lane, "software engineer")

    def discover_family(self, family: str) -> tuple[list[RankableJob], SourceRunHealth]:
        fam_enum = _FAMILY.get(family)
        health = SourceRunHealth(family=family)
        if fam_enum is None:
            health.status = "NOT_REACHED"
            health.detail = f"unknown portal family {family!r}"
            return [], health

        md = {"max_pages": self.max_pages, "max_results": self.max_cards}
        if self._adapter_factory is not None:
            adapter = self._adapter_factory(family)
        else:
            instance = make_portal_instance(fam_enum, f"{family}-live", metadata=md)
            client = self._client_factory()
            adapter = adapter_class_for_family(fam_enum)(instance, http_client=client)

        jobs: list[RankableJob] = []
        seen: set[str] = set()
        health.attempted = True
        page = 1
        while page <= self.max_pages:
            request = SearchRequest(query=self._query(), location=self.location,
                                    recency_days=self.recency_days, limit=self.max_cards, page=page)
            try:
                res = adapter.search(request)
            except AdapterError as exc:
                health.status = {
                    "ANTI_BOT": "ACCESS_LIMITED", "LOGIN_WALL": "AUTH_REQUIRED",
                    "HTTP_429": "RATE_LIMITED",
                }.get(exc.category.value, "SOURCE_UNAVAILABLE")
                health.detail = exc.message
                break
            health.pages = page
            health.raw_results += len(res.results)
            for r in res.results:
                job = self._to_job(r, family)
                if job is None or job.job_key in seen:
                    continue
                seen.add(job.job_key)
                jobs.append(job)
            if not res.has_more:
                # zero-truth: a reachable page with no cards is truthful about WHY
                if not jobs and res.zero_result_kind.value == "EXTRACTION_UNRESOLVED":
                    health.status = "EXTRACTION_UNRESOLVED"
                    health.detail = "; ".join(res.parse_findings) or "no recognizable cards"
                break
            page += 1

        health.unique_leads = len(jobs)
        if health.status == "NOT_REACHED":
            health.status = "COMPLETED" if jobs else (
                "ATTEMPTED_ZERO" if health.raw_results == 0 else "EXTRACTION_UNRESOLVED")
        return jobs, health

    def discover(self, families: tuple[str, ...] = ("linkedin",)) -> LiveDiscoveryOutcome:
        outcome = LiveDiscoveryOutcome()
        for family in families:
            jobs, health = self.discover_family(family)
            outcome.jobs.extend(jobs)
            outcome.health.append(health)
        return outcome

    def as_market_exec(self, families: tuple[str, ...] = ("linkedin",)):
        """Return a zero-arg producer for DailyRunner.market_exec. It records the
        run health on the returned callable so the caller can surface it."""
        holder: dict[str, Any] = {}

        def _producer():
            outcome = self.discover(families)
            holder["outcome"] = outcome
            return outcome.jobs

        _producer.holder = holder  # type: ignore[attr-defined]
        return _producer


__all__ = [
    "LivePortalDiscovery",
    "LiveDiscoveryOutcome",
    "SourceRunHealth",
]
