from __future__ import annotations

from dataclasses import replace

from .models import JobLead

_TARGET = ("java", "spring", "react", "frontend", ".net", "c#", "asp.net", "hcm", "payroll", "hris", "workforce", "integration", "backend", "full stack", "software engineer", "developer")
_EXCLUDED = ("support engineer", "technical support", "sales", "marketing", "product manager", "qa engineer", "sdet", "data scientist", "devops engineer")
_INDIA = ("india", "bengaluru", "bangalore", "hyderabad", "pune", "chennai", "noida", "gurugram", "mumbai")


def prefilter(lead: JobLead) -> JobLead:
    text = " ".join((lead.title, lead.location, " ".join(lead.skills))).lower()
    if any(token in lead.title.lower() for token in _EXCLUDED):
        return replace(lead, prefilter_status="REJECTED", prefilter_reason="NON_TARGET_ROLE")
    if not any(token in text for token in _TARGET):
        return replace(lead, prefilter_status="DEFERRED", prefilter_reason="NO_TARGET_SIGNAL")
    if lead.location and not any(token in lead.location.lower() for token in _INDIA):
        return replace(lead, prefilter_status="REJECTED", prefilter_reason="NON_INDIA_LOCATION")
    return replace(lead, prefilter_status="QUEUED", prefilter_reason="TARGET_SIGNAL")
