"""Validation: severity/action finding model + quarantine.

A :class:`Finding` never mutates data — it *describes* a problem and
prescribes an :class:`Action`. The :class:`Validator` runs a set of rules
over records, attaches findings, and derives each record's disposition
(the most severe action wins). Records whose disposition is QUARANTINE or
REJECT are separated out so they can never silently reach the canonical
store.

Rules are deliberately generic: they read a record's :class:`FieldSpec`s
from the mapping (dtype, domain, required, min/max), so adding a field or
tightening a domain is a mapping change, not a rule change.
"""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass, field as dc_field
from typing import Any, Iterable, Optional

from atlas.data_integrity.mapping import EntitySchema, MappingConfig
from atlas.data_integrity.records import IngestionRecord


class Severity(enum.IntEnum):
    INFO = 10
    WARNING = 20
    ERROR = 30
    CRITICAL = 40


class Action(enum.IntEnum):
    ACCEPT = 0
    FLAG = 1
    QUARANTINE = 2
    REJECT = 3


@dataclass(frozen=True)
class Finding:
    code: str
    severity: Severity
    action: Action
    message: str
    entity_type: str
    record_id: str
    field: Optional[str] = None
    context: dict[str, Any] = dc_field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity.name,
            "action": self.action.name,
            "message": self.message,
            "entity_type": self.entity_type,
            "record_id": self.record_id,
            "field": self.field,
            "context": dict(self.context),
        }


# --- Formula/script injection heuristics (spreadsheet safety) -------------
_FORMULA_RE = re.compile(r"^\s*[=+\-@]")
_INJECTION_HINT_RE = re.compile(r"(?i)(=cmd|=hyperlink|=webservice|=importxml|\bDDE\b)")
_URL_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://[^\s]+$")
_DOMAIN_RE = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9\-]{0,61}[a-z0-9])?\.)+[a-z]{2,}$")


class _RuleContext:
    """Cross-record context shared by all rules for one validation pass."""

    def __init__(self, records: list[IngestionRecord]):
        self.records = records


class ValidationRule:
    """Base rule. Override :meth:`check` (per-record) and/or
    :meth:`check_global` (whole-batch)."""

    code = "RULE"

    def check(
        self, record: IngestionRecord, schema: Optional[EntitySchema]
    ) -> Iterable[Finding]:
        return ()

    def check_global(self, ctx: _RuleContext) -> Iterable[Finding]:
        return ()


class RequiredFieldRule(ValidationRule):
    code = "REQUIRED_MISSING"

    def check(self, record, schema):
        if schema is None:
            return
        for spec in schema.fields:
            if spec.required and not record.has(spec.canonical):
                yield Finding(
                    self.code,
                    Severity.ERROR,
                    Action.QUARANTINE,
                    f"Required field '{spec.canonical}' is missing/empty.",
                    record.entity_type,
                    record.record_id,
                    field=spec.canonical,
                )


class NumericRule(ValidationRule):
    code = "NUMERIC"

    def check(self, record, schema):
        if schema is None:
            return
        for spec in schema.fields:
            if spec.dtype not in ("int", "score"):
                continue
            fv = record.fields.get(spec.canonical)
            if fv is None or fv.normalized in (None, ""):
                continue
            val = fv.normalized
            if not isinstance(val, (int, float)) or isinstance(val, bool):
                yield Finding(
                    "NON_NUMERIC",
                    Severity.WARNING,
                    Action.FLAG,
                    f"Field '{spec.canonical}' expected numeric, got {val!r}.",
                    record.entity_type,
                    record.record_id,
                    field=spec.canonical,
                    context={"value": val},
                )
                continue
            if spec.dtype == "int" and isinstance(val, float) and not val.is_integer():
                yield Finding(
                    "NON_INTEGER",
                    Severity.WARNING,
                    Action.FLAG,
                    f"Field '{spec.canonical}' expected integer, got {val!r}.",
                    record.entity_type,
                    record.record_id,
                    field=spec.canonical,
                    context={"value": val},
                )
            if spec.min_value is not None and val < spec.min_value:
                yield Finding(
                    "OUT_OF_RANGE_LOW",
                    Severity.ERROR,
                    Action.QUARANTINE,
                    f"Field '{spec.canonical}'={val} below minimum {spec.min_value}.",
                    record.entity_type,
                    record.record_id,
                    field=spec.canonical,
                    context={"value": val, "min": spec.min_value},
                )
            if spec.max_value is not None and val > spec.max_value:
                yield Finding(
                    "OUT_OF_RANGE_HIGH",
                    Severity.ERROR,
                    Action.QUARANTINE,
                    f"Field '{spec.canonical}'={val} above maximum {spec.max_value}.",
                    record.entity_type,
                    record.record_id,
                    field=spec.canonical,
                    context={"value": val, "max": spec.max_value},
                )


class DomainRule(ValidationRule):
    code = "DOMAIN"

    def check(self, record, schema):
        if schema is None:
            return
        for spec in schema.fields:
            if not spec.domain:
                continue
            fv = record.fields.get(spec.canonical)
            if fv is None or fv.normalized in (None, ""):
                continue
            value = str(fv.normalized)
            allowed = {a.casefold() for a in spec.domain}
            if value.casefold() not in allowed:
                yield Finding(
                    "UNKNOWN_ENUM_VALUE",
                    Severity.WARNING,
                    Action.FLAG,
                    f"Field '{spec.canonical}'={value!r} not in known domain {list(spec.domain)}.",
                    record.entity_type,
                    record.record_id,
                    field=spec.canonical,
                    context={"value": value, "domain": list(spec.domain)},
                )


class UrlRule(ValidationRule):
    code = "URL"

    def check(self, record, schema):
        if schema is None:
            return
        for spec in schema.fields:
            if spec.dtype not in ("url", "domain"):
                continue
            fv = record.fields.get(spec.canonical)
            if fv is None or fv.normalized in (None, ""):
                continue
            value = str(fv.normalized)
            if spec.dtype == "url":
                ok = bool(_URL_RE.match(value))
                code, label = "MALFORMED_URL", "URL"
            else:
                ok = bool(_DOMAIN_RE.match(value))
                code, label = "MALFORMED_DOMAIN", "domain"
            if not ok:
                yield Finding(
                    code,
                    Severity.WARNING,
                    Action.FLAG,
                    f"Field '{spec.canonical}'={value!r} is not a well-formed {label}.",
                    record.entity_type,
                    record.record_id,
                    field=spec.canonical,
                    context={"value": value},
                )


class UnknownColumnRule(ValidationRule):
    code = "UNKNOWN_COLUMN"

    def check(self, record, schema):
        for header in record.unknown:
            yield Finding(
                self.code,
                Severity.INFO,
                Action.FLAG,
                f"Unmapped column {header!r} preserved as provenance-only.",
                record.entity_type,
                record.record_id,
                field=header,
            )


class InjectionRule(ValidationRule):
    """Detect spreadsheet formula / CSV injection payloads in text cells."""

    code = "INJECTION"

    def check(self, record, schema):
        for canonical, fv in record.fields.items():
            raw = fv.raw
            if not isinstance(raw, str):
                continue
            if _FORMULA_RE.match(raw) or _INJECTION_HINT_RE.search(raw):
                yield Finding(
                    "FORMULA_INJECTION",
                    Severity.ERROR,
                    Action.QUARANTINE,
                    f"Field '{canonical}' contains a formula/injection payload.",
                    record.entity_type,
                    record.record_id,
                    field=canonical,
                    context={"raw": raw[:120]},
                )


class DuplicateIdentityRule(ValidationRule):
    """Flag records that share an identity key within the batch."""

    code = "DUPLICATE_IDENTITY"

    def check_global(self, ctx):
        seen: dict[str, list[IngestionRecord]] = {}
        for rec in ctx.records:
            if rec.identity_key is None:
                continue
            seen.setdefault(rec.identity_key, []).append(rec)
        for key, group in seen.items():
            if len(group) <= 1:
                continue
            # first occurrence is the survivor; the rest are exact/near dupes
            for rec in group[1:]:
                yield Finding(
                    self.code,
                    Severity.INFO,
                    Action.FLAG,
                    f"Duplicate identity {key!r} ({len(group)} rows share it).",
                    rec.entity_type,
                    rec.record_id,
                    context={"identity_key": key, "count": len(group)},
                )


DEFAULT_RULES: tuple[ValidationRule, ...] = (
    RequiredFieldRule(),
    NumericRule(),
    DomainRule(),
    UrlRule(),
    UnknownColumnRule(),
    InjectionRule(),
    DuplicateIdentityRule(),
)


@dataclass
class ValidationReport:
    findings: list[Finding] = dc_field(default_factory=list)
    dispositions: dict[str, Action] = dc_field(default_factory=dict)  # record_id -> Action
    quarantined: list[str] = dc_field(default_factory=list)  # record_ids
    rejected: list[str] = dc_field(default_factory=list)

    def worst_action(self, record_id: str) -> Action:
        return self.dispositions.get(record_id, Action.ACCEPT)

    def counts_by_severity(self) -> dict[str, int]:
        out: dict[str, int] = {s.name: 0 for s in Severity}
        for f in self.findings:
            out[f.severity.name] += 1
        return out

    def counts_by_code(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for f in self.findings:
            out[f.code] = out.get(f.code, 0) + 1
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "finding_count": len(self.findings),
            "by_severity": self.counts_by_severity(),
            "by_code": self.counts_by_code(),
            "quarantined": list(self.quarantined),
            "rejected": list(self.rejected),
            "findings": [f.to_dict() for f in self.findings],
        }


class Validator:
    def __init__(
        self,
        mapping: MappingConfig,
        rules: Iterable[ValidationRule] = DEFAULT_RULES,
    ):
        self.mapping = mapping
        self.rules = tuple(rules)

    def validate(self, records: list[IngestionRecord]) -> ValidationReport:
        report = ValidationReport()
        ctx = _RuleContext(records)
        dispositions: dict[str, Action] = {r.record_id: Action.ACCEPT for r in records}

        for rec in records:
            schema = self.mapping.schema_for(rec.entity_type)
            for rule in self.rules:
                for finding in rule.check(rec, schema):
                    report.findings.append(finding)
                    dispositions[finding.record_id] = max(
                        dispositions.get(finding.record_id, Action.ACCEPT), finding.action
                    )

        for rule in self.rules:
            for finding in rule.check_global(ctx):
                report.findings.append(finding)
                dispositions[finding.record_id] = max(
                    dispositions.get(finding.record_id, Action.ACCEPT), finding.action
                )

        report.dispositions = dispositions
        for rid, action in dispositions.items():
            if action == Action.QUARANTINE:
                report.quarantined.append(rid)
            elif action == Action.REJECT:
                report.rejected.append(rid)
        report.quarantined.sort()
        report.rejected.sort()
        # Stable ordering of findings for deterministic reports.
        report.findings.sort(key=lambda f: (f.record_id, f.code, f.field or ""))
        return report
