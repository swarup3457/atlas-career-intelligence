"""Live + demo board providers for the hunt pipeline (build spec 8, 15).

The live provider is OFFICIAL-SOURCE-ONLY and read-only: it fetches public
employer-controlled ATS JSON boards (Greenhouse / Lever public APIs), bounded by
page/company/timeout, and NEVER logs in, bypasses a challenge, or touches a
portal. Unreachable/unknown companies are recorded truthfully as ACCESS_LIMITED
rather than fabricated. The demo provider serves deterministic offline data.
"""

from __future__ import annotations

import datetime
import json
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Mapping, Optional

from atlas.hunt.campaign import CompanyRef
from atlas.hunt.models import BoardSnapshot, JobDetailRevision, RawJob
from atlas.sources.ats.base import sanitize_description

__all__ = ["LiveOfficialProvider", "build_live_provider", "build_demo_provider", "KNOWN_BOARDS"]

_UA = "AtlasHunt/2.0 (+official-only read-only research)"

# Curated map of seed / seed-adjacent companies to their PUBLIC employer ATS
# board. Only public, employer-controlled endpoints. Companies not present are
# recorded ACCESS_LIMITED (no public official ATS board resolvable) — never
# fabricated. Tokens may change over time; failures degrade truthfully.
KNOWN_BOARDS: dict[str, tuple[str, str]] = {
    "Razorpay": ("lever", "razorpay"),
    "Postman": ("greenhouse", "postman"),
    "BrowserStack": ("greenhouse", "browserstack"),
    "Freshworks": ("greenhouse", "freshworks"),
    "Groww": ("lever", "groww"),
    "Cashfree Payments": ("lever", "cashfree"),
    "Zeta": ("greenhouse", "zeta"),
    "Whatfix": ("lever", "whatfix"),
    "MongoDB": ("greenhouse", "mongodb"),
    "Databricks": ("greenhouse", "databricks"),
    "HashiCorp": ("greenhouse", "hashicorp"),
    "GitLab": ("greenhouse", "gitlab"),
    "Stripe": ("greenhouse", "stripe"),
}


def _http_json(url: str, timeout: float):
    req = urllib.request.Request(url, headers={"User-Agent": _UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, json.loads(resp.read(4_000_000))


def _parse_date(value) -> Optional[datetime.date]:
    if not value:
        return None
    try:
        return datetime.date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


@dataclass
class LiveOfficialProvider:
    """Official-only, read-only ATS provider with bounded fetches."""

    max_pages: int = 2
    timeout: float = 15.0
    max_hydrate_per_board: int = 40
    network_calls: int = 0
    boards: Mapping[str, tuple[str, str]] = field(default_factory=lambda: dict(KNOWN_BOARDS))
    _cache: dict = field(default_factory=dict, init=False)

    def fetch_board(self, campaign_id: str, company: CompanyRef) -> BoardSnapshot:
        started = datetime.datetime.now(datetime.timezone.utc).isoformat()
        mapping = self.boards.get(company.name)
        if not mapping:
            return self._empty(campaign_id, company, "OFFICIAL_BOARD_NOT_RESOLVED", started)
        provider, token = mapping
        try:
            if provider == "greenhouse":
                raw, url = self._greenhouse_board(token)
            elif provider == "lever":
                raw, url = self._lever_board(token)
            else:
                return self._empty(campaign_id, company, "UNSUPPORTED_PROVIDER", started)
        except (urllib.error.URLError, socket.timeout, TimeoutError, json.JSONDecodeError, ValueError) as exc:
            snap = self._empty(campaign_id, company, f"ACCESS_ERROR:{type(exc).__name__}", started)
            return snap
        completed = datetime.datetime.now(datetime.timezone.utc).isoformat()
        snap = BoardSnapshot(
            snapshot_id=f"{campaign_id}:{company.name}:{provider}",
            campaign_id=campaign_id, company_id=company.name, company_name=company.name,
            source_instance_id=f"{company.name}:{provider}:{token}", source_family=f"OFFICIAL_ATS_{provider.upper()}",
            route_family="OFFICIAL_ATS", source_url=url, fetch_started_at=started, fetch_completed_at=completed,
            pages=1, adapter_version="live-1", parser_version="live-1", source_health="OK",
            raw_jobs=tuple(raw), snapshot_status="COMPLETE", access_status="OK",
        )
        self._cache[snap.snapshot_id] = (provider, token)
        return snap

    def _empty(self, campaign_id, company: CompanyRef, reason: str, started: str) -> BoardSnapshot:
        return BoardSnapshot(
            snapshot_id=f"{campaign_id}:{company.name}:none", campaign_id=campaign_id,
            company_id=company.name, company_name=company.name,
            source_instance_id=f"{company.name}:careers", source_family="OFFICIAL_CAREERS",
            route_family="OFFICIAL_CAREERS", fetch_started_at=started, pages=0,
            raw_jobs=(), snapshot_status="ACCESS_BLOCKED", access_status=reason, source_health="LIMITED",
        )

    def _greenhouse_board(self, token: str):
        self.network_calls += 1
        url = f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs"
        status, data = _http_json(url, self.timeout)
        jobs = []
        for j in data.get("jobs", []):
            loc = (j.get("location") or {}).get("name", "")
            jobs.append(RawJob(
                source_job_id=str(j.get("id")), title=j.get("title", ""), location=loc,
                url=j.get("absolute_url", ""), summary=j.get("title", ""),
                posted_date=_parse_date(j.get("updated_at") or j.get("first_published")),
                requisition_id=str(j.get("requisition_id") or ""),
            ))
        return jobs, url

    def _lever_board(self, token: str):
        self.network_calls += 1
        url = f"https://api.lever.co/v0/postings/{token}?mode=json"
        status, data = _http_json(url, self.timeout)
        jobs = []
        for j in data:
            cats = j.get("categories") or {}
            jobs.append(RawJob(
                source_job_id=str(j.get("id")), title=j.get("text", ""), location=cats.get("location", ""),
                url=j.get("hostedUrl", ""), summary=(j.get("descriptionPlain") or "")[:400],
                posted_date=_parse_date(datetime.datetime.utcfromtimestamp((j.get("createdAt") or 0) / 1000).date().isoformat()) if j.get("createdAt") else None,
                requisition_id="",
            ))
        return jobs, url

    def hydrate(self, snapshot: BoardSnapshot, source_job_id: str) -> Optional[JobDetailRevision]:
        mapping = self._cache.get(snapshot.snapshot_id)
        if not mapping:
            return None
        provider, token = mapping
        raw = next((r for r in snapshot.raw_jobs if r.source_job_id == source_job_id), None)
        if raw is None:
            return None
        try:
            if provider == "greenhouse":
                self.network_calls += 1
                _, d = _http_json(f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs/{source_job_id}", self.timeout)
                content = sanitize_description(d.get("content", "")) or ""
                official_url = d.get("absolute_url", raw.url)
                posted = _parse_date(d.get("updated_at") or d.get("first_published")) or raw.posted_date
                deadline = _parse_date(d.get("application_deadline"))
                reqid = str(d.get("requisition_id") or raw.requisition_id or "")
            else:  # lever detail is already in the list payload
                content = raw.summary
                official_url = raw.url
                posted = raw.posted_date
                deadline = None
                reqid = raw.requisition_id
        except (urllib.error.URLError, socket.timeout, TimeoutError, json.JSONDecodeError, ValueError):
            return None
        loc = raw.location or ""
        work_mode = "REMOTE" if "remote" in loc.lower() else "ONSITE"
        return JobDetailRevision(
            revision_id=f"{snapshot.snapshot_id}:{source_job_id}", snapshot_id=snapshot.snapshot_id,
            source_job_id=source_job_id, company=snapshot.company_name, title=raw.title,
            description=content, experience_text=content, location=loc, work_mode=work_mode,
            posted_date=posted, deadline=deadline, requisition_id=reqid, official_url=official_url,
            verification_state="VERIFIED_OFFICIAL", has_live_official_page=True,
            evidence_texts=(content[:2000],), eligibility_text=f"{loc}. {content[:400]}",
            source_family=snapshot.source_family, parser_version="live-1",
            retrieved_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
            record_class="OFFICIAL_DIRECT", discovery_channels=(snapshot.source_family,),
        )


def build_live_provider(policy, intent, *, max_companies=30, max_pages=2, timeout=15.0):
    provider = LiveOfficialProvider(max_pages=max_pages, timeout=timeout)
    resolvable = sum(1 for c in policy.company_seed.companies if c.name in provider.boards)
    return provider, f"live official ATS ({resolvable} seed companies have a public board; others recorded ACCESS_LIMITED)"


# ---------------------------------------------------------------------------
# Deterministic offline demo provider (rich, multi-lane, with wrong-stack
# negative controls) used by ``atlas hunt run`` without --live and by tests.
# ---------------------------------------------------------------------------
def _demo_dataset():
    today = datetime.date.today()

    def snap(company, jobs):
        return BoardSnapshot(
            snapshot_id=f"DEMO:{company}", campaign_id="DEMO", company_id=company, company_name=company,
            source_instance_id=f"{company}:careers", source_family="OFFICIAL_CAREERS",
            route_family="OFFICIAL_CAREERS", source_url=f"{company.lower()}.example/careers",
            raw_jobs=tuple(jobs), snapshot_status="COMPLETE",
        )

    def det(company, sid, title, desc, exp, reqs=(), loc="Bengaluru"):
        return JobDetailRevision(
            revision_id=f"D:{sid}", snapshot_id=f"DEMO:{company}", source_job_id=sid, company=company,
            title=title, description=desc, mandatory_requirements=reqs, experience_text=exp, location=loc,
            posted_date=today, official_url=f"{company.lower()}.example/careers/{sid}", requisition_id=sid,
            verification_state="VERIFIED_OFFICIAL", has_live_official_page=True, eligibility_text=f"{loc}, India",
            source_family="OFFICIAL_CAREERS", discovery_channels=("OFFICIAL_CAREERS",),
        )

    data = {
        "Accenture": (
            [RawJob("A1", "Java Backend Engineer", "Bengaluru", summary="Java Spring Boot"),
             RawJob("A2", "Senior Backend Engineer (Ruby on Rails)", "Bengaluru", summary="Ruby on Rails")],
            {"A1": det("Accenture", "A1", "Java Backend Engineer",
                       "Build Java Spring Boot microservices, REST APIs, Hibernate, MySQL.", "2-3 years",
                       ("Java", "Spring Boot", "REST APIs")),
             "A2": det("Accenture", "A2", "Senior Backend Engineer (Ruby on Rails)",
                       "Ruby on Rails, PostgreSQL, REST APIs, microservices.", "6+ years")},
        ),
        "Microsoft": (
            [RawJob("M1", "Full Stack Java Developer", "Hyderabad", summary="Java React"),
             RawJob("M2", "Technical Product Manager for API Management", "Hyderabad", summary="product roadmap")],
            {"M1": det("Microsoft", "M1", "Full Stack Java Developer",
                       "Java, Spring Boot backend with React and TypeScript frontend.", "2-3 years",
                       ("Java", "Spring Boot", "React"), loc="Hyderabad"),
             "M2": det("Microsoft", "M2", "Technical Product Manager for API Management",
                       "Own the API product roadmap and REST API strategy.", "5 years", loc="Hyderabad")},
        ),
        "JPMorgan Chase": (
            [RawJob("J1", "React Developer", "Bengaluru", summary="React TypeScript"),
             RawJob("J2", "Software Development Engineer in Test", "Bengaluru", summary="Selenium automation")],
            {"J1": det("JPMorgan Chase", "J1", "React Developer",
                       "React, ReactJS, TypeScript, responsive UI, Redux.", "2 years", ("React", "TypeScript")),
             "J2": det("JPMorgan Chase", "J2", "Software Development Engineer in Test",
                       "Selenium test automation, API testing, Java.", "3 years")},
        ),
        "Flipkart": (
            [RawJob("F1", ".NET Developer", "Bengaluru", summary="C# ASP.NET"),
             RawJob("F2", "Staff Backend Engineer (Go)", "Bengaluru", summary="Golang microservices")],
            {"F1": det("Flipkart", "F1", ".NET Developer",
                       "C#, ASP.NET Core, Entity Framework, SQL Server, REST APIs.", "2-3 years",
                       ("C#", "ASP.NET Core")),
             "F2": det("Flipkart", "F2", "Staff Backend Engineer (Go)",
                       "Design Go microservices, REST APIs, SQL.", "8+ years")},
        ),
        "Dayforce": (
            [RawJob("D1", "Payroll Software Engineer", "Pune", summary="payroll HCM Java"),
             RawJob("D2", "Associate Software Engineer", "Pune", summary="software development")],
            {"D1": det("Dayforce", "D1", "Payroll Software Engineer",
                       "Build payroll and HCM integration software using Java Spring Boot.", "2-3 years",
                       ("Java", "payroll"), loc="Pune"),
             "D2": det("Dayforce", "D2", "Associate Software Engineer",
                       "Software development and application development in Java and SQL.", "1-2 years",
                       ("Java", "SQL"), loc="Pune")},
        ),
    }
    boards = {c: snap(c, jobs) for c, (jobs, _) in data.items()}
    details = {}
    for _c, (_jobs, dets) in data.items():
        details.update(dets)
    return boards, details


def build_demo_provider():
    from atlas.hunt.pipeline import FixtureProvider

    boards, details = _demo_dataset()
    return FixtureProvider(boards=boards, details=details)
