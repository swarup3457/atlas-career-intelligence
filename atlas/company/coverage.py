"""Company↔source coverage linkage (Phase 1A.5).

Generic bridge between the company/source registry and the Phase 1A
coverage framework: builds one :class:`CoverageTask` per company×source
relationship so a future run can truthfully report per-company source
coverage (Workday India COMPLETED, LinkedIn NOT_ATTEMPTED, …). No report
formatting and no candidate policy — linkage only.
"""

from __future__ import annotations

from typing import Optional

from atlas.company.registry import CompanyRegistry
from atlas.sources.coverage import CoverageStatus, CoverageTask


def company_source_coverage_id(company_id: str, instance_id: str) -> str:
    return f"{company_id}::{instance_id}"


def company_coverage_tasks(
    registry: CompanyRegistry,
    company_id: str,
    *,
    lane: Optional[str] = None,
    include_noncurrent: bool = False,
) -> list[CoverageTask]:
    """One coverage task per (current) company↔source relationship."""
    company = registry.get_company(company_id)
    if company is None:
        return []
    tasks: list[CoverageTask] = []
    for rel in registry.list_relationships(company_id):
        if not include_noncurrent and not rel.is_current:
            continue
        tasks.append(
            CoverageTask(
                coverage_id=company_source_coverage_id(company_id, rel.instance_id),
                source_instance=rel.instance_id,
                company=company.canonical_name,
                source_type=rel.source_type,
                lane=lane,
                status=CoverageStatus.NOT_ATTEMPTED,
            )
        )
    return tasks


__all__ = ["company_source_coverage_id", "company_coverage_tasks"]
