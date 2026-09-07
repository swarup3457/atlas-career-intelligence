"""Typed ingestion records: raw + normalized + unknown + provenance.

Every value read from a workbook is preserved in THREE forms so nothing is
ever silently lost:

* ``raw``        - exactly what the cell contained (untouched).
* ``normalized`` - the deterministic canonical form (see normalizers).
* ``unknown``    - columns that did not map to any known field, kept with
  their header + value so an operator can see what was dropped.

Each record also carries :class:`Provenance` (source file, sheet, row) so
every downstream finding, status transition, or canonical write can be
traced back to the exact origin cell.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass(frozen=True)
class Provenance:
    """Where a record came from. Immutable — provenance never changes."""

    source_file: str
    sheet_name: str
    entity_type: str
    row_index: int  # 1-based row number within the sheet (header = row 1)
    ingested_at: str
    mapping_version: str = "1"

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclass
class FieldValue:
    """A single field's raw + normalized pair, with the normalizer used."""

    canonical: str
    raw: Any
    normalized: Any
    normalizer: str = "identity"
    changed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "canonical": self.canonical,
            "raw": self.raw,
            "normalized": self.normalized,
            "normalizer": self.normalizer,
            "changed": self.changed,
        }


@dataclass
class IngestionRecord:
    """One logical row: typed, normalized, and fully provenanced.

    ``fields`` is keyed by canonical field name. ``unknown`` holds header
    -> value pairs for columns that did not resolve to any known field.
    ``identity_key`` is filled in by the identity resolver. ``findings``
    accumulates validation findings referencing this record.
    """

    record_id: str
    entity_type: str
    provenance: Provenance
    fields: dict[str, FieldValue] = field(default_factory=dict)
    unknown: dict[str, Any] = field(default_factory=dict)
    identity_key: Optional[str] = None
    status: str = "INGESTED"

    # ------------------------------------------------------------------
    def raw_value(self, canonical: str) -> Any:
        fv = self.fields.get(canonical)
        return fv.raw if fv is not None else None

    def value(self, canonical: str) -> Any:
        """Normalized value for a canonical field (or None)."""
        fv = self.fields.get(canonical)
        return fv.normalized if fv is not None else None

    def has(self, canonical: str) -> bool:
        fv = self.fields.get(canonical)
        return fv is not None and fv.normalized not in (None, "")

    def to_dict(self) -> dict[str, Any]:
        return {
            "record_id": self.record_id,
            "entity_type": self.entity_type,
            "provenance": self.provenance.to_dict(),
            "fields": {k: v.to_dict() for k, v in self.fields.items()},
            "unknown": dict(self.unknown),
            "identity_key": self.identity_key,
            "status": self.status,
        }

    def normalized_dict(self) -> dict[str, Any]:
        return {k: v.normalized for k, v in self.fields.items()}


@dataclass
class SheetDiagnostic:
    """Per-sheet outcome from ingestion (schema-agnostic)."""

    sheet_name: str
    entity_type: Optional[str]
    resolved: bool
    header_columns: list[str] = field(default_factory=list)
    mapped_columns: list[str] = field(default_factory=list)
    unknown_columns: list[str] = field(default_factory=list)
    duplicate_headers: list[str] = field(default_factory=list)
    data_row_count: int = 0
    blank_row_count: int = 0
    record_count: int = 0
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclass
class IngestionResult:
    """Everything produced by ingesting one workbook."""

    source_file: str
    records: list[IngestionRecord] = field(default_factory=list)
    sheets: list[SheetDiagnostic] = field(default_factory=list)
    load_error: Optional[str] = None

    def records_for(self, entity_type: str) -> list[IngestionRecord]:
        return [r for r in self.records if r.entity_type == entity_type]

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_file": self.source_file,
            "record_count": len(self.records),
            "sheets": [s.to_dict() for s in self.sheets],
            "load_error": self.load_error,
        }
