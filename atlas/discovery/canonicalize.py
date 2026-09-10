from __future__ import annotations

from .models import JobLead


def deduplicate(leads: list[JobLead]) -> list[JobLead]:
    seen: dict[str, JobLead] = {}
    for lead in leads:
        existing = seen.get(lead.identity)
        if existing is None or (lead.description_available and not existing.description_available):
            seen[lead.identity] = lead
    return list(seen.values())
