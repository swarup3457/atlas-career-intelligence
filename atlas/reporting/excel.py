"""Atlas Excel reporting — SCAFFOLD ONLY, no final business schema.

Excel is report-only: it is generated FROM durable SQLite state and is
never itself a source of orchestration truth. The final workbook schema
(sheet names, columns, formatting rules) will be defined once the
Workspace Atlas Agent's Excel schema specification is imported.

Today this module only proves the mechanism: given a list of plain dict
rows, write a single generic worksheet. This is intentionally generic and
will very likely be replaced/extended once the real schema lands.
"""

from __future__ import annotations

import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from openpyxl import Workbook

# Fixed timestamp used to pin workbook core properties so generated files
# are byte-deterministic (openpyxl otherwise stamps datetime.now()).
_FIXED_TS = datetime.datetime(2026, 1, 1, 0, 0, 0)


def _pin_properties(wb: Workbook) -> None:
    props = wb.properties
    props.created = _FIXED_TS
    props.modified = _FIXED_TS
    props.creator = "atlas.data_integrity"
    props.lastModifiedBy = "atlas.data_integrity"


class ExcelReporter:
    """Minimal, generic Excel report generator (scaffold).

    NOT the final Atlas report schema. Use `write_generic_sheet` only for
    foundation-level testing/demonstration purposes. `build_workbook` is
    the generic multi-sheet primitive used by the Phase 0.9 data-quality
    report writer (which owns its own atomic, crash-safe persistence).
    """

    @staticmethod
    def _columns_for(rows: Sequence[Mapping[str, Any]], columns: Iterable[str] | None) -> list[str]:
        if columns is not None:
            return list(columns)
        seen: list[str] = []
        for row in rows:
            for key in row.keys():
                if key not in seen:
                    seen.append(key)
        return seen

    def build_workbook(
        self,
        sheets: Sequence[tuple[str, Sequence[str] | None, Sequence[Mapping[str, Any]]]],
    ) -> Workbook:
        """Build (but do not save) a multi-sheet workbook.

        ``sheets`` is an ordered sequence of ``(sheet_name, columns, rows)``.
        ``columns`` may be ``None`` to infer from the row dict keys. Returns
        the in-memory :class:`~openpyxl.Workbook` so callers can persist it
        atomically. Sheet names are truncated to Excel's 31-char limit and
        de-duplicated deterministically. Core document properties are pinned
        to a fixed timestamp so identical input yields byte-identical output.
        """
        wb = Workbook()
        wb.remove(wb.active)
        _pin_properties(wb)
        used: set[str] = set()
        for raw_name, columns, rows in sheets:
            name = (raw_name or "Sheet")[:31]
            base = name
            n = 2
            while name in used:
                suffix = f"_{n}"
                name = base[: 31 - len(suffix)] + suffix
                n += 1
            used.add(name)
            ws = wb.create_sheet(title=name)
            cols = self._columns_for(rows, columns)
            ws.append(cols)
            for row in rows:
                ws.append([_cell(row.get(col, "")) for col in cols])
        if not wb.sheetnames:
            wb.create_sheet(title="Sheet1")
        return wb

    def write_generic_sheet(
        self,
        output_path: Path,
        sheet_name: str,
        rows: Sequence[dict[str, Any]],
        columns: Iterable[str] | None = None,
    ) -> Path:
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        wb = Workbook()
        ws = wb.active
        ws.title = sheet_name[:31] if sheet_name else "Sheet1"

        columns = self._columns_for(rows, columns)
        ws.append(columns)
        for row in rows:
            ws.append([_cell(row.get(col, "")) for col in columns])

        wb.save(str(output_path))
        return output_path


def _cell(value: Any) -> Any:
    """Coerce a value to something openpyxl can store safely.

    Lists/dicts/tuples are JSON-ish stringified; everything else that is not
    a native Excel scalar is stringified. None becomes an empty string.
    """
    if value is None:
        return ""
    if isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (list, tuple, dict, set)):
        import json

        try:
            return json.dumps(value if not isinstance(value, set) else sorted(value), default=str)
        except (TypeError, ValueError):
            return str(value)
    return str(value)
