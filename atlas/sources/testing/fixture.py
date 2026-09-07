"""Deterministic FixtureAdapter (Phase 1A test double).

Reads sanitized local JSON fixtures (never the internet) and parses each raw
item independently via :func:`atlas.sources.parsing.parse_isolated`, proving:
parsing/normalization, one-bad-item isolation, Unicode handling, schema
drift detection, zero-result classification, and closed-state representation.

Fixtures are intentionally generic (a small ``{"items": [...]}`` shape), not
a copy of any real site's proprietary markup.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from atlas.sources.adapter import AdapterError, SourceAdapter, new_result_base
from atlas.sources.health import HealthEvidence, SourceHealth, SourceHealthState, classify_health
from atlas.sources.models import (
    ActiveState,
    Capability,
    DetailRequest,
    DiscoveryResult,
    SearchRequest,
    SearchResult,
    SourceInstance,
    SourceType,
    VerificationLevel,
    WorkMode,
    ZeroResultKind,
)
from atlas.models import ErrorCategory
from atlas.sources.parsing import parse_isolated

_EXPECTED_KEYS = {"id", "title", "company", "location"}
_WORK_MODES = {m.value.lower(): m for m in WorkMode}


class FixtureAdapter(SourceAdapter):
    source_type = SourceType.FIXTURE
    CAPABILITIES = frozenset(
        {
            Capability.SEARCH,
            Capability.DETAIL,
            Capability.LOCATION_FILTER,
            Capability.KEYWORD_FILTER,
            Capability.ACTIVE_STATUS,
            Capability.POSTED_DATE,
            Capability.DESCRIPTION,
        }
    )
    adapter_version = "fixture-1.0.0"
    parser_version = "fixture-parser-1.0.0"

    def __init__(
        self,
        instance: SourceInstance,
        raw_items: Optional[list] = None,
        *,
        untrusted_zero: bool = False,
    ):
        super().__init__(instance)
        self.untrusted_zero = untrusted_zero or bool(instance.metadata.get("untrusted_zero"))
        if raw_items is not None:
            self._raw_items = list(raw_items)
        elif instance.metadata.get("items") is not None:
            self._raw_items = list(instance.metadata["items"])
        elif instance.metadata.get("fixture_path"):
            self._raw_items = self._load_fixture(Path(instance.metadata["fixture_path"]))
        else:
            self._raw_items = []

    @staticmethod
    def _load_fixture(path: Path) -> list:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return list(data.get("items", []))
        if isinstance(data, list):
            return data
        raise ValueError(f"fixture {path} must be a list or an object with 'items'")

    # -- parsing ------------------------------------------------------------
    def _parse_item(self, raw: Any) -> Optional[DiscoveryResult]:
        if not isinstance(raw, dict):
            raise ValueError("item is not a JSON object")
        title = raw.get("title")
        if not isinstance(title, str) or not title.strip():
            raise ValueError("missing or invalid 'title'")
        company = raw.get("company")
        if company is not None and not isinstance(company, str):
            raise ValueError("'company' must be a string when present")

        status = str(raw.get("status", "")).lower()
        if status in ("closed", "expired", "filled"):
            is_active = ActiveState.INACTIVE
            verification = VerificationLevel.CLOSED_BANNER
        elif status in ("open", "active", "live"):
            is_active = ActiveState.ACTIVE
            verification = VerificationLevel.PORTAL_LIVE
        else:
            is_active = ActiveState.UNKNOWN
            verification = VerificationLevel.INDEXED_ONLY

        work_mode = _WORK_MODES.get(str(raw.get("work_mode", "")).lower(), WorkMode.UNKNOWN)

        return DiscoveryResult(
            **new_result_base(
                self,
                source_job_id=str(raw["id"]) if raw.get("id") is not None else None,
                source_url=raw.get("url"),
                canonical_url=raw.get("url"),
                company=company,
                title=title,
                location=raw.get("location"),
                work_mode=work_mode,
                posted_at=raw.get("posted_at"),
                deadline=raw.get("deadline"),
                salary_text=raw.get("salary"),
                skills=tuple(raw.get("skills", []) or ()),
                description=raw.get("description"),
                is_active=is_active,
                verification_level=verification,
            )
        )

    def _schema_drift(self, items: list) -> bool:
        """A crude structural-drift signal: none of the items carry any of
        the expected keys (the fixture shape changed under us)."""
        if not items:
            return False
        for raw in items:
            if isinstance(raw, dict) and (_EXPECTED_KEYS & set(raw.keys())):
                return False
        return True

    # -- operations ---------------------------------------------------------
    def health_check(self) -> SourceHealth:
        evidence = HealthEvidence(
            http_status=200,
            result_count=len(self._raw_items),
            expected_structure_present=not self._schema_drift(self._raw_items),
        )
        return classify_health(evidence)

    def search(self, request: SearchRequest) -> SearchResult:
        self._require(Capability.SEARCH)
        parsed = parse_isolated(self._raw_items, self._parse_item)
        results = tuple(parsed.results[: request.limit])

        if results:
            return SearchResult(
                results=results,
                page=request.page,
                total_reported=len(parsed.results),
                zero_result_kind=ZeroResultKind.NOT_APPLICABLE,
                parse_findings=tuple(parsed.finding_reasons()),
            )

        # Zero results: distinguish extraction failure from a genuine zero.
        if parsed.findings and self._schema_drift(self._raw_items):
            kind = ZeroResultKind.EXTRACTION_UNRESOLVED
        elif self.untrusted_zero:
            kind = ZeroResultKind.UNTRUSTED_ZERO
        else:
            kind = ZeroResultKind.TRUSTED_ZERO
        return SearchResult(
            results=(),
            page=request.page,
            zero_result_kind=kind,
            parse_findings=tuple(parsed.finding_reasons()),
        )

    def fetch_detail(self, request: DetailRequest) -> DiscoveryResult:
        self._require(Capability.DETAIL)
        for raw in self._raw_items:
            if not isinstance(raw, dict):
                continue
            if request.source_job_id is not None and str(raw.get("id")) == request.source_job_id:
                parsed = self._parse_item(raw)
                if parsed is not None:
                    return parsed
            if request.url is not None and raw.get("url") == request.url:
                parsed = self._parse_item(raw)
                if parsed is not None:
                    return parsed
        raise AdapterError(ErrorCategory.INVALID_RESPONSE, "No fixture item matched the detail request.")


def make_fixture_instance(instance_id: str, items: list, **metadata: Any) -> SourceInstance:
    md = {"items": items}
    md.update(metadata)
    return SourceInstance(
        instance_id=instance_id,
        source_type=SourceType.FIXTURE,
        display_name=f"Fixture {instance_id}",
        metadata=md,
    )


__all__ = ["FixtureAdapter", "make_fixture_instance"]
