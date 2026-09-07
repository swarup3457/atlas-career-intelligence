"""Data-quality report builder.

Assembles the outputs of every pipeline stage (audit, ingestion,
validation, identity, reconciliation, adversarial scenarios, performance)
into a single deterministic report structure, and renders it to:

* a multi-sheet XLSX workbook (via the generic :class:`ExcelReporter`), and
* an equivalent JSON document.

Persistence is delegated to :mod:`atlas.data_integrity.report_writer`,
which writes both atomically and crash-safely. The report structure itself
is pure data (no I/O), so it is trivially testable and reproducible.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from atlas.data_integrity.identity import IdentityResolution
from atlas.data_integrity.records import IngestionResult
from atlas.data_integrity.validation import ValidationReport
from atlas.reporting.excel import ExcelReporter

REPORT_SHEETS = (
    "Summary",
    "Sheet_Diagnostics",
    "Findings",
    "Quarantine",
    "Identity_Relationships",
    "Reconciliation",
    "Adversarial_Scenarios",
    "Performance",
)


@dataclass
class QualityReport:
    generated_at: str
    audit: dict[str, Any]
    ingestion: dict[str, Any]
    validation: dict[str, Any]
    identity: dict[str, Any]
    reconciliation: Optional[dict[str, Any]] = None
    adversarial: list[dict[str, Any]] = field(default_factory=list)
    performance: list[dict[str, Any]] = field(default_factory=list)

    # ------------------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "audit": self.audit,
            "ingestion": self.ingestion,
            "validation": self.validation,
            "identity": self.identity,
            "reconciliation": self.reconciliation,
            "adversarial": self.adversarial,
            "performance": self.performance,
        }

    # ------------------------------------------------------------------
    def _summary_rows(self) -> list[dict[str, Any]]:
        val = self.validation
        rec = self.reconciliation or {}
        by_sev = val.get("by_severity", {})
        rows = [
            {"metric": "generated_at", "value": self.generated_at},
            {"metric": "source_file", "value": self.audit.get("source_file")},
            {"metric": "source_sha256", "value": self.audit.get("sha256")},
            {"metric": "source_bytes", "value": self.audit.get("bytes")},
            {"metric": "mapping_version", "value": self.audit.get("mapping_version")},
            {"metric": "sheets", "value": self.ingestion.get("sheet_count")},
            {"metric": "records_ingested", "value": self.ingestion.get("record_count")},
            {"metric": "unknown_columns", "value": self.ingestion.get("unknown_column_count")},
            {"metric": "findings_total", "value": val.get("finding_count")},
            {"metric": "findings_info", "value": by_sev.get("INFO", 0)},
            {"metric": "findings_warning", "value": by_sev.get("WARNING", 0)},
            {"metric": "findings_error", "value": by_sev.get("ERROR", 0)},
            {"metric": "findings_critical", "value": by_sev.get("CRITICAL", 0)},
            {"metric": "quarantined", "value": len(val.get("quarantined", []))},
            {"metric": "identity_clusters", "value": self.identity.get("cluster_count")},
            {"metric": "canonical_created", "value": rec.get("created")},
            {"metric": "canonical_updated", "value": rec.get("updated")},
            {"metric": "canonical_unchanged", "value": rec.get("unchanged")},
            {"metric": "canonical_closed", "value": rec.get("closed")},
            {"metric": "canonical_reposted", "value": rec.get("reposted")},
            {"metric": "observations_added", "value": rec.get("observations_added")},
        ]
        return rows

    def _sheet_rows(self) -> list[dict[str, Any]]:
        rows = []
        for s in self.ingestion.get("sheets", []):
            rows.append(
                {
                    "sheet": s.get("sheet_name"),
                    "entity_type": s.get("entity_type"),
                    "resolved": s.get("resolved"),
                    "mapped_columns": len(s.get("mapped_columns", [])),
                    "unknown_columns": len(s.get("unknown_columns", [])),
                    "duplicate_headers": len(s.get("duplicate_headers", [])),
                    "data_rows": s.get("data_row_count"),
                    "blank_rows": s.get("blank_row_count"),
                    "records": s.get("record_count"),
                    "notes": "; ".join(s.get("notes", [])),
                }
            )
        return rows

    def _finding_rows(self) -> list[dict[str, Any]]:
        return [
            {
                "record_id": f.get("record_id"),
                "entity_type": f.get("entity_type"),
                "code": f.get("code"),
                "severity": f.get("severity"),
                "action": f.get("action"),
                "field": f.get("field"),
                "message": f.get("message"),
            }
            for f in self.validation.get("findings", [])
        ]

    def _quarantine_rows(self) -> list[dict[str, Any]]:
        return [{"record_id": rid} for rid in self.validation.get("quarantined", [])]

    def _identity_rows(self) -> list[dict[str, Any]]:
        rows = []
        for c in self.identity.get("clusters", []):
            if c.get("size", 1) <= 1 and "SINGLETON" in c.get("relationships", []):
                continue  # only surface non-trivial relationships
            rows.append(
                {
                    "identity_key": c.get("identity_key"),
                    "entity_type": c.get("entity_type"),
                    "size": c.get("size"),
                    "relationships": ", ".join(c.get("relationships", [])),
                    "sources": ", ".join(c.get("sources", [])),
                    "conflict_fields": ", ".join(c.get("conflicts", {}).keys()),
                }
            )
        return rows

    def _reconciliation_rows(self) -> list[dict[str, Any]]:
        if not self.reconciliation:
            return []
        return [{"metric": k, "value": v} for k, v in self.reconciliation.items()]

    def _adversarial_rows(self) -> list[dict[str, Any]]:
        return list(self.adversarial)

    def _performance_rows(self) -> list[dict[str, Any]]:
        return list(self.performance)

    # ------------------------------------------------------------------
    def to_workbook(self):
        reporter = ExcelReporter()
        sheets = [
            ("Summary", ["metric", "value"], self._summary_rows()),
            ("Sheet_Diagnostics", None, self._sheet_rows()),
            (
                "Findings",
                ["record_id", "entity_type", "code", "severity", "action", "field", "message"],
                self._finding_rows(),
            ),
            ("Quarantine", ["record_id"], self._quarantine_rows()),
            (
                "Identity_Relationships",
                ["identity_key", "entity_type", "size", "relationships", "sources", "conflict_fields"],
                self._identity_rows(),
            ),
            ("Reconciliation", ["metric", "value"], self._reconciliation_rows()),
            ("Adversarial_Scenarios", None, self._adversarial_rows()),
            ("Performance", None, self._performance_rows()),
        ]
        # Ensure every declared sheet exists even when empty (stable schema).
        normalized = []
        for name, cols, rows in sheets:
            if not rows and cols is None:
                cols = ["info"]
                rows = [{"info": "no rows"}]
            elif not rows:
                rows = [{c: "" for c in cols}]
            normalized.append((name, cols, rows))
        return reporter.build_workbook(normalized)


def build_quality_report(
    generated_at: str,
    audit: dict[str, Any],
    ingestion: IngestionResult,
    validation: ValidationReport,
    identity: IdentityResolution,
    reconciliation: Optional[dict[str, Any]] = None,
    adversarial: Optional[list[dict[str, Any]]] = None,
    performance: Optional[list[dict[str, Any]]] = None,
) -> QualityReport:
    ing_dict = ingestion.to_dict()
    ing_dict["sheet_count"] = len(ingestion.sheets)
    ing_dict["record_count"] = len(ingestion.records)
    ing_dict["unknown_column_count"] = sum(len(s.unknown_columns) for s in ingestion.sheets)
    return QualityReport(
        generated_at=generated_at,
        audit=audit,
        ingestion=ing_dict,
        validation=validation.to_dict(),
        identity=identity.to_dict(),
        reconciliation=reconciliation,
        adversarial=adversarial or [],
        performance=performance or [],
    )
