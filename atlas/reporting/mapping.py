"""Atlas report mapping (Phase 1B, build spec section 20).

Excel is REPORT-ONLY and is generated FROM durable state; it is never
operational truth. This module loads the public ``report_mapping.yaml``,
maps CANONICAL internal fields to human-facing workbook columns (resolving
the Official_Apply_URL/Verification_Status naming conflicts), builds the
eight-sheet workbook, and re-opens it to validate sheet names, columns, and
row counts before it is considered published.

Phase 1B builds/tests the mapping with SYNTHETIC data only; it performs no
live search.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

import yaml

from atlas.reporting.excel import ExcelReporter

DEFAULT_MAPPING = Path(__file__).resolve().parents[2] / "config" / "policy" / "report_mapping.yaml"

REQUIRED_SHEETS = (
    "All_Jobs",
    "New_Companies",
    "Company_Coverage",
    "Source_Coverage",
    "Closed_or_Rejected",
    "Resume_Tailoring",
    "Recruiter_Contacts",
    "Run_Summary",
)


class ReportMappingError(ValueError):
    pass


@dataclass(frozen=True)
class SheetSpec:
    name: str
    columns: tuple[str, ...]
    field_map: Mapping[str, str] = field(default_factory=dict)  # canonical -> column
    status_values: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReportMapping:
    sheets: Mapping[str, SheetSpec]
    sheet_order: tuple[str, ...]
    schema_version: int
    policy_version: str

    def map_record(self, sheet: str, canonical: Mapping[str, Any]) -> dict[str, Any]:
        """Map ONE canonical internal record to a workbook row for ``sheet``.
        Fields with a field_map use canonical keys; otherwise column names are
        used directly. Unmapped columns become empty strings (never invented)."""
        spec = self.sheets[sheet]
        row: dict[str, Any] = {c: "" for c in spec.columns}
        if spec.field_map:
            for canonical_key, column in spec.field_map.items():
                if canonical_key in canonical and column in row:
                    row[column] = canonical[canonical_key]
        # direct column matches (for sheets without a field_map)
        for col in spec.columns:
            if col in canonical:
                row[col] = canonical[col]
        return row

    def build_rows(self, sheet: str, records: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        return [self.map_record(sheet, r) for r in records]


def load_report_mapping(path: Optional[Path] = None) -> ReportMapping:
    path = Path(path) if path else DEFAULT_MAPPING
    if not path.exists():
        raise ReportMappingError(f"missing report mapping: {path}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, Mapping):
        raise ReportMappingError("report mapping top level must be a mapping")
    sheets_raw = raw.get("sheets", {}) or {}
    problems: list[str] = []
    sheets: dict[str, SheetSpec] = {}
    for name, body in sheets_raw.items():
        cols = tuple(str(c) for c in (body.get("columns") or []))
        if not cols:
            problems.append(f"sheet {name}: no columns")
        sheets[name] = SheetSpec(
            name=name,
            columns=cols,
            field_map=dict(body.get("field_map", {}) or {}),
            status_values=tuple(body.get("status_values", []) or []),
        )
    for required in REQUIRED_SHEETS:
        if required not in sheets:
            problems.append(f"missing required sheet: {required}")
    if problems:
        raise ReportMappingError("Invalid report mapping:\n  - " + "\n  - ".join(problems))
    order = tuple(raw.get("sheet_order", list(sheets)))
    return ReportMapping(
        sheets=sheets,
        sheet_order=order,
        schema_version=int(raw.get("schema_version", 1)),
        policy_version=str(raw.get("policy_version", "unversioned")),
    )


@dataclass(frozen=True)
class ReportValidation:
    ok: bool
    sheet_names: tuple[str, ...]
    row_counts: Mapping[str, int]
    problems: tuple[str, ...] = ()


def write_report(
    mapping: ReportMapping,
    output_path: Path,
    data: Mapping[str, Sequence[Mapping[str, Any]]],
) -> "WriteResult":
    """Build and ATOMICALLY save the eight-sheet report workbook from canonical
    data, returning the :class:`WriteResult` (actual written path, locked-file
    fallback, recovered stale temps). ``data`` maps sheet name -> canonical
    records. Report-only (build spec 20/21, P0-18): production reports use the
    proven temp-save + reopen-validate + atomic-replace writer, never a bare
    ``wb.save()``."""
    from atlas.data_integrity.report_writer import write_workbook_atomic

    reporter = ExcelReporter()
    sheets: list[tuple[str, Sequence[str], Sequence[Mapping[str, Any]]]] = []
    for sheet_name in mapping.sheet_order:
        spec = mapping.sheets[sheet_name]
        rows = mapping.build_rows(sheet_name, data.get(sheet_name, []))
        sheets.append((sheet_name, list(spec.columns), rows))
    wb = reporter.build_workbook(sheets)
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    return write_workbook_atomic(output_path, wb, expected_sheets=REQUIRED_SHEETS)


def validate_report(mapping: ReportMapping, path: Path) -> ReportValidation:
    """Re-open a generated workbook and verify sheet names, required columns,
    and row counts (build spec 20 'report reopen validation')."""
    from openpyxl import load_workbook

    problems: list[str] = []
    wb = load_workbook(str(path), read_only=True)
    names = tuple(wb.sheetnames)
    for required in REQUIRED_SHEETS:
        if required not in names:
            problems.append(f"workbook missing sheet: {required}")
    row_counts: dict[str, int] = {}
    for sheet_name in names:
        ws = wb[sheet_name]
        spec = mapping.sheets.get(sheet_name)
        header = []
        data_rows = 0
        for i, row in enumerate(ws.iter_rows(values_only=True)):
            if i == 0:
                header = [c for c in row if c is not None]
            else:
                data_rows += 1
        row_counts[sheet_name] = data_rows
        if spec is not None and tuple(header) != spec.columns:
            problems.append(f"sheet {sheet_name}: header {tuple(header)} != expected {spec.columns}")
    wb.close()
    return ReportValidation(ok=not problems, sheet_names=names, row_counts=row_counts, problems=tuple(problems))


__all__ = [
    "REQUIRED_SHEETS",
    "ReportMappingError",
    "SheetSpec",
    "ReportMapping",
    "load_report_mapping",
    "ReportValidation",
    "write_report",
    "validate_report",
]
