"""Reusable adapter contract harness (Phase 1A).

Every future real adapter must satisfy this same suite. The harness is a
function driven by a ``make_adapter(scenario)`` factory so it is adapter- and
framework-neutral; a factory returns ``None`` for a scenario its adapter
cannot represent (that check is SKIPPED, not failed).

Covered categories: registration/identity, capabilities, typed result
schema, query validation, pagination, recency, normalization, missing
fields, malformed-item isolation, 429/5xx/timeout mapping, 404 behavior,
trusted/untrusted zero, sentinel behavior, selector drift, closed-state,
Unicode, provenance, adapter/parser version, and no-secret evidence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

from atlas.models import ErrorCategory
from atlas.sources.adapter import AdapterError, CapabilityNotSupported, SourceAdapter
from atlas.sources.evidence import EvidenceStore
from atlas.sources.health import SourceHealth, SourceHealthState
from atlas.sources.models import (
    ActiveState,
    Capability,
    DetailRequest,
    DiscoveryResult,
    SearchRequest,
    SearchResult,
    WorkMode,
    ZeroResultKind,
)
from atlas.sources.zero_result import assess_search, run_sentinel_probe

AdapterFactory = Callable[[str], Optional[SourceAdapter]]

PASS, SKIP, FAIL = "PASS", "SKIP", "FAIL"

_REQUIRED_RESULT_KEYS = {
    "source_type", "source_instance", "source_job_id", "source_url", "canonical_url",
    "company", "title", "location", "work_mode", "posted_at", "updated_at", "deadline",
    "employment_type", "experience_text", "salary_text", "skills", "description",
    "is_active", "verification_level", "discovered_at", "adapter_version",
    "parser_version", "confidence", "provenance", "raw_observation_ref",
}


@dataclass
class ContractReport:
    checks: list[tuple[str, str, str]] = field(default_factory=list)

    def add(self, name: str, status: str, detail: str = "") -> None:
        self.checks.append((name, status, detail))

    @property
    def ok(self) -> bool:
        return all(status != FAIL for _, status, _ in self.checks)

    @property
    def failures(self) -> list[tuple[str, str, str]]:
        return [c for c in self.checks if c[1] == FAIL]

    def passed(self, name: str) -> bool:
        return any(n == name and s == PASS for n, s, _ in self.checks)

    def render(self) -> str:
        return "\n".join(f"[{s}] {n}" + (f" - {d}" if d else "") for n, s, d in self.checks)


def _sentinel_request() -> SearchRequest:
    return SearchRequest(query="*", limit=10, extra_filters={"sentinel": True})


class _Skip(Exception):
    """Internal: a capability-gated check that does not apply to this adapter."""


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


def run_contract_checks(make_adapter: AdapterFactory, *, run_evidence_check: bool = True) -> ContractReport:
    report = ContractReport()

    def check(name: str, scenario: str, fn) -> None:
        adapter = make_adapter(scenario)
        if adapter is None:
            report.add(name, SKIP, f"scenario {scenario!r} not supported")
            return
        try:
            fn(adapter)
            report.add(name, PASS)
        except _Skip as exc:
            report.add(name, SKIP, str(exc) or "capability not supported")
        except AssertionError as exc:
            report.add(name, FAIL, str(exc))
        except Exception as exc:  # noqa: BLE001
            report.add(name, FAIL, f"{type(exc).__name__}: {exc}")

    # --- registration / identity / capabilities ---------------------------
    def _registration(a: SourceAdapter) -> None:
        d = a.describe()
        _assert(bool(d["instance_id"]), "instance_id must be set")
        _assert(bool(d["adapter_version"]) and bool(d["parser_version"]), "versions must be set")
        _assert(len(a.capabilities()) >= 1, "at least one capability required")
        _assert(all(isinstance(c, Capability) for c in a.capabilities()), "capabilities must be Capability enum")
    check("registration_identity", "results", _registration)

    # --- typed result schema / normalization / provenance / versions ------
    def _schema(a: SourceAdapter) -> None:
        result = a.search(SearchRequest(query="engineer", limit=25))
        _assert(isinstance(result, SearchResult), "search must return SearchResult")
        _assert(result.count > 0, "results scenario must return >=1 result")
        for r in result.results:
            _assert(isinstance(r, DiscoveryResult), "each result must be a DiscoveryResult")
            _assert(set(r.to_dict().keys()) == _REQUIRED_RESULT_KEYS, "result schema keys mismatch")
            _assert(isinstance(r.work_mode, WorkMode), "work_mode must be WorkMode")
            _assert(isinstance(r.is_active, ActiveState), "is_active must be ActiveState")
            _assert(isinstance(r.skills, tuple), "skills must be a tuple")
            _assert(r.source_instance == a.instance_id, "provenance source_instance mismatch")
            _assert(r.adapter_version == a.adapter_version, "adapter_version not stamped")
            _assert(r.parser_version == a.parser_version, "parser_version not stamped")
    check("typed_result_schema", "results", _schema)

    # --- query validation --------------------------------------------------
    def _query_validation(a: SourceAdapter) -> None:
        for bad in (dict(page=0), dict(limit=0), dict(recency_days=-1)):
            try:
                SearchRequest(**bad)
                raise AssertionError(f"SearchRequest({bad}) should have raised")
            except ValueError:
                pass
    check("query_validation", "results", _query_validation)

    # --- pagination --------------------------------------------------------
    def _pagination(a: SourceAdapter) -> None:
        if not a.supports(Capability.PAGINATION):
            raise _Skip("no PAGINATION capability")
        result = a.search(SearchRequest(query="engineer", page=1, limit=2))
        _assert(result.count <= 2, "limit must bound page size")
        _assert(isinstance(result.has_more, bool), "has_more must be bool")
    check("pagination_semantics", "results", _pagination)

    # --- recency -----------------------------------------------------------
    def _recency(a: SourceAdapter) -> None:
        if not a.supports(Capability.RECENCY_FILTER):
            raise _Skip("no RECENCY_FILTER capability")
        result = a.search(SearchRequest(query="engineer", recency_days=7, limit=5))
        _assert(isinstance(result, SearchResult), "recency search must return SearchResult")
    check("recency_semantics", "results", _recency)

    # --- malformed item / one-bad-item isolation --------------------------
    def _isolation(a: SourceAdapter) -> None:
        result = a.search(SearchRequest(query="engineer", limit=50))
        _assert(result.count >= 1, "valid items must survive alongside a malformed one")
        _assert(len(result.parse_findings) >= 1, "a malformed item must produce a finding")
    check("one_bad_item_isolation", "malformed", _isolation)

    # --- error mappings ----------------------------------------------------
    def _error_check(category: ErrorCategory):
        def _fn(a: SourceAdapter) -> None:
            try:
                a.search(SearchRequest(query="x"))
                raise AssertionError("expected AdapterError")
            except AdapterError as exc:
                _assert(exc.category == category, f"expected {category.value}, got {exc.category.value}")
        return _fn
    check("map_429", "error:HTTP_429", _error_check(ErrorCategory.HTTP_429))
    check("map_5xx", "error:HTTP_5XX", _error_check(ErrorCategory.HTTP_5XX))
    check("map_timeout", "error:TIMEOUT", _error_check(ErrorCategory.TIMEOUT))

    # --- 404 / missing detail ---------------------------------------------
    def _not_found(a: SourceAdapter) -> None:
        try:
            a.fetch_detail(DetailRequest(source_job_id="__does_not_exist__"))
            raise AssertionError("expected AdapterError for missing detail")
        except AdapterError as exc:
            _assert(exc.category in (ErrorCategory.INVALID_RESPONSE, ErrorCategory.SOURCE_UNAVAILABLE),
                    f"unexpected 404 category {exc.category.value}")
    check("not_found_detail", "notfound", _not_found)

    # --- zero-result trusted / untrusted ----------------------------------
    def _trusted_zero(a: SourceAdapter) -> None:
        result = a.search(SearchRequest(query="nomatch"))
        _assert(result.count == 0, "zero scenario must return no results")
        _assert(assess_search(result) == ZeroResultKind.TRUSTED_ZERO, "expected TRUSTED_ZERO")
    check("zero_result_trusted", "zero", _trusted_zero)

    def _untrusted_zero(a: SourceAdapter) -> None:
        result = a.search(SearchRequest(query="nomatch"))
        _assert(result.count == 0, "untrusted zero scenario must return no results")
        kind = assess_search(result, historical_yields=(40, 38, 45))
        _assert(kind == ZeroResultKind.UNTRUSTED_ZERO, f"expected UNTRUSTED_ZERO, got {kind.value}")
    check("zero_result_untrusted", "untrusted_zero", _untrusted_zero)

    # --- sentinel + selector drift ----------------------------------------
    def _sentinel(a: SourceAdapter) -> None:
        outcome = run_sentinel_probe(a, _sentinel_request(), historical_yields=(40, 38))
        _assert(outcome.ran, "sentinel probe must run once")
        _assert(isinstance(outcome.health, SourceHealth), "sentinel must classify health")
    check("sentinel_behavior", "untrusted_zero", _sentinel)

    def _drift(a: SourceAdapter) -> None:
        outcome = run_sentinel_probe(a, _sentinel_request(), historical_yields=(40, 38))
        _assert(outcome.health.state == SourceHealthState.SELECTOR_DRIFT_SUSPECTED,
                f"expected SELECTOR_DRIFT_SUSPECTED, got {outcome.health.state.value}")
    check("selector_drift", "selector_drift", _drift)

    # --- closed-state representation --------------------------------------
    def _closed(a: SourceAdapter) -> None:
        result = a.search(SearchRequest(query="engineer", limit=50))
        _assert(any(r.is_active == ActiveState.INACTIVE for r in result.results),
                "a closed posting must be represented as INACTIVE")
    check("closed_state", "closed", _closed)

    # --- Unicode -----------------------------------------------------------
    def _unicode(a: SourceAdapter) -> None:
        result = a.search(SearchRequest(query="engineer", limit=50))
        _assert(any(r.title and any(ord(ch) > 127 for ch in r.title) for r in result.results),
                "unicode scenario must preserve non-ASCII characters")
    check("unicode", "unicode", _unicode)

    # --- health_check ------------------------------------------------------
    def _health(a: SourceAdapter) -> None:
        _assert(isinstance(a.health_check(), SourceHealth), "health_check must return SourceHealth")
    check("health_check", "results", _health)

    # --- no-secret evidence (adapter-independent) -------------------------
    if run_evidence_check:
        try:
            store = EvidenceStore()
            ref = store.put(
                "job body ghp_" + "a" * 36 + " and Authorization: Bearer sk-secret-value",
                source_instance="contract",
            )
            fragment = store.get(ref).fragment() or ""
            _assert("ghp_" + "a" * 36 not in fragment, "github token must be redacted from evidence")
            _assert("REDACTED" in fragment, "redaction marker expected")
            report.add("no_secret_evidence", PASS)
        except AssertionError as exc:
            report.add("no_secret_evidence", FAIL, str(exc))

    return report


__all__ = ["ContractReport", "run_contract_checks", "AdapterFactory"]
