"""Generic adversarial workbook generation (scenarios A..AU + expansion).

Every generator here is:

* **Derived from the immutable fixture** — it reads the real workbook
  read-only and writes brand-new workbooks to a separate output directory.
  The original file's bytes are never touched; :func:`sha256_file` is used
  by the pipeline/tests to *prove* the original is unchanged.
* **Deterministic** — all perturbation uses a seeded :class:`random.Random`,
  so the same seed yields byte-stable structure and repeatable diagnostics.

The catalog covers 47 scenario codes ``A``..``AU`` spanning duplicate/
identity attacks, value-domain violations, structural/schema drift,
encoding hazards, cross-sheet contradictions, injection payloads and
large-volume expansion (500 / 5,000 / 20,000 rows). Where an individual
dedicated workbook is impractical (the three expansion sizes, and a
combined-chaos case), the scenario is represented with explicit result
diagnostics instead — this is intentional and recorded in the report.
"""

from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from openpyxl import load_workbook

from atlas.reporting.excel import ExcelReporter

# Canonical All_Jobs header order (kept as data, not positions elsewhere).
JOB_COLUMNS = [
    "Company", "Role", "Job_ID", "Location", "Work_Mode", "Experience",
    "Search_Lane", "Company_Type", "Discovery_Source", "Source_URL",
    "Official_Apply_URL", "Posted_Date", "Freshness", "Verification_Status",
    "Live_Status", "Match_Score", "Requirements_Matched", "Missing_Requirements",
    "Why_It_Fits", "Application_Recommendation", "First_Seen", "Last_Verified", "Notes",
]

CLOSED_COLUMNS = [
    "Company", "Role", "Job_ID", "Location", "Source", "Reason",
    "Verification_Status", "Live_Status", "Experience", "Posted_Date", "Notes",
]

DEFAULT_SEED = 20260813

# Type: a workbook spec is {sheet_name: {"columns": [...], "rows": [ {col: val} ]}}
WorkbookSpec = dict[str, dict[str, Any]]


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def workbook_content_digest(path: Path) -> str:
    """Digest of a workbook's *logical* content (decompressed members).

    This is the deterministic-mutation guarantee: identical generated
    content yields an identical digest, independent of zip-container byte
    layout (which can vary with the zlib build / OS). Members are hashed in
    sorted name order so the result is stable.
    """
    import zipfile

    h = hashlib.sha256()
    with zipfile.ZipFile(path) as zf:
        for name in sorted(zf.namelist()):
            h.update(name.encode("utf-8"))
            h.update(b"\0")
            h.update(zf.read(name))
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Base extraction (read-only; never mutates the original)
# ---------------------------------------------------------------------------
def extract_sheet_rows(path: Path, sheet: str, limit: Optional[int] = None) -> list[dict[str, Any]]:
    wb = load_workbook(path, read_only=True, data_only=True)
    try:
        ws = wb[sheet]
        rows = list(ws.iter_rows(values_only=True))
    finally:
        wb.close()
    if not rows:
        return []
    header = [("" if c is None else str(c)) for c in rows[0]]
    # trim trailing empties
    while header and header[-1] == "":
        header.pop()
    out: list[dict[str, Any]] = []
    for raw in rows[1:]:
        if all(c is None or (isinstance(c, str) and c.strip() == "") for c in raw):
            continue
        d = {header[i]: raw[i] if i < len(raw) else None for i in range(len(header))}
        out.append(d)
        if limit is not None and len(out) >= limit:
            break
    return out


def _row(overrides: dict[str, Any], columns: list[str]) -> dict[str, Any]:
    base = {c: "" for c in columns}
    base.update({k: v for k, v in overrides.items() if k in columns})
    return base


def _seed_job(base_rows: list[dict[str, Any]], idx: int = 0) -> dict[str, Any]:
    """A clean seed job row from the fixture (falls back to a synthetic one)."""
    if base_rows and idx < len(base_rows):
        return dict(base_rows[idx])
    return _row(
        {
            "Company": "Acme Corp",
            "Role": "Software Engineer",
            "Job_ID": f"AC{idx:04d}",
            "Location": "Bangalore",
            "Work_Mode": "Hybrid",
            "Experience": "1-2 years",
            "Discovery_Source": "Official Careers",
            "Source_URL": "https://acme.example.com/jobs/1",
            "Official_Apply_URL": "https://acme.example.com/apply/1",
            "Verification_Status": "Verified Official",
            "Live_Status": "Active",
            "Match_Score": 80,
            "First_Seen": "2026-08-13",
            "Last_Verified": "2026-08-13",
        },
        JOB_COLUMNS,
    )


# ---------------------------------------------------------------------------
# Scenario catalog
# ---------------------------------------------------------------------------
@dataclass
class Scenario:
    code: str
    name: str
    category: str
    description: str
    build: Callable[[list[dict[str, Any]], random.Random], WorkbookSpec]
    expected: list[str] = field(default_factory=list)
    represented: bool = False  # True == diagnostics-only / combined representation


def _jobs_spec(rows: list[dict[str, Any]], columns: Optional[list[str]] = None) -> WorkbookSpec:
    return {"All_Jobs": {"columns": columns or JOB_COLUMNS, "rows": rows}}


def _build_A(base, rng):  # exact duplicate row
    r = _seed_job(base)
    return _jobs_spec([dict(r), dict(r)])


def _build_B(base, rng):  # case-variant company (folds to same identity)
    r = _seed_job(base)
    dup = dict(r); dup["Company"] = str(r["Company"]).upper()
    return _jobs_spec([r, dup])


def _build_C(base, rng):  # whitespace padding around identity fields
    r = _seed_job(base)
    dup = dict(r)
    dup["Company"] = f"   {r['Company']}   "
    dup["Job_ID"] = f"  {r['Job_ID']} "
    return _jobs_spec([r, dup])


def _build_D(base, rng):  # diacritic variant company (accent-folds to same)
    r = _seed_job(base)
    dup = dict(r); dup["Company"] = str(r["Company"]).replace("a", "á")
    return _jobs_spec([r, dup])


def _build_E(base, rng):  # trailing whitespace + tab in Job_ID
    r = _seed_job(base)
    dup = dict(r); dup["Job_ID"] = f"{r['Job_ID']}\t "
    return _jobs_spec([r, dup])


def _build_F(base, rng):  # null / empty Job_ID (identity falls back)
    r = _seed_job(base); r["Job_ID"] = None
    return _jobs_spec([r])


def _build_G(base, rng):  # missing required column (drop Company header)
    r = _seed_job(base)
    cols = [c for c in JOB_COLUMNS if c != "Company"]
    return _jobs_spec([_row(r, cols)], columns=cols)


def _build_H(base, rng):  # extra unknown column
    r = _seed_job(base)
    cols = JOB_COLUMNS + ["Mystery_Signal"]
    row = _row(r, cols); row["Mystery_Signal"] = "unmapped-value"
    return _jobs_spec([row], columns=cols)


def _build_I(base, rng):  # reordered columns
    r = _seed_job(base)
    cols = list(reversed(JOB_COLUMNS))
    return _jobs_spec([_row(r, cols)], columns=cols)


def _build_J(base, rng):  # renamed header via alias (Company -> Company_Name)
    r = _seed_job(base)
    cols = ["Company_Name" if c == "Company" else c for c in JOB_COLUMNS]
    row = _row(r, JOB_COLUMNS); row2 = {("Company_Name" if k == "Company" else k): v for k, v in row.items()}
    return _jobs_spec([row2], columns=cols)


def _build_K(base, rng):  # repost: same identity, later observation dates
    r = _seed_job(base)
    dup = dict(r)
    dup["First_Seen"] = "2026-09-01"; dup["Last_Verified"] = "2026-09-01"
    return _jobs_spec([r, dup])


def _build_L(base, rng):  # multi-source: same identity, different Discovery_Source
    r = _seed_job(base); r["Discovery_Source"] = "Official Careers"
    dup = dict(r); dup["Discovery_Source"] = "LinkedIn"
    return _jobs_spec([r, dup])


def _build_M(base, rng):  # closed status
    r = _seed_job(base); r["Live_Status"] = "Closed"
    return _jobs_spec([r])


def _build_N(base, rng):  # conflicting role for same identity
    r = _seed_job(base)
    dup = dict(r); dup["Role"] = str(r["Role"]) + " (SENIOR)"
    return _jobs_spec([r, dup])


def _build_O(base, rng):  # non-numeric match score
    r = _seed_job(base); r["Match_Score"] = "excellent"
    return _jobs_spec([r])


def _build_P(base, rng):  # out-of-range match score
    r = _seed_job(base); r["Match_Score"] = 250
    return _jobs_spec([r])


def _build_Q(base, rng):  # malformed URL
    r = _seed_job(base); r["Source_URL"] = "htp:/not a url"
    return _jobs_spec([r])


def _build_R(base, rng):  # date format variants
    r1 = _seed_job(base); r1["Job_ID"] = "DATE-1"; r1["Posted_Date"] = "13/08/2026"
    r2 = _seed_job(base); r2["Job_ID"] = "DATE-2"; r2["Posted_Date"] = "Aug 13, 2026"
    return _jobs_spec([r1, r2])


def _build_S(base, rng):  # future-dated posting
    r = _seed_job(base); r["Posted_Date"] = "2099-01-01"
    return _jobs_spec([r])


def _build_T(base, rng):  # numeric-as-text
    r = _seed_job(base); r["Match_Score"] = "88"
    return _jobs_spec([r])


def _build_U(base, rng):  # boolean-ish variants in a bool field (Source_Coverage)
    rows = [
        _row({"Source": "LinkedIn", "Attempted": "Yes", "Completed": "TRUE"}, _SOURCE_COLUMNS),
        _row({"Source": "Naukri", "Attempted": "1", "Completed": "no"}, _SOURCE_COLUMNS),
    ]
    return {"Source_Coverage": {"columns": _SOURCE_COLUMNS, "rows": rows}}


def _build_V(base, rng):  # embedded newlines / CR in text
    r = _seed_job(base); r["Notes"] = "line1\r\nline2\rline3\n\n"
    return _jobs_spec([r])


def _build_W(base, rng):  # very long cell content
    r = _seed_job(base); r["Why_It_Fits"] = "x" * 6000
    return _jobs_spec([r])


def _build_X(base, rng):  # emoji / non-ASCII
    r = _seed_job(base); r["Role"] = "Engineer 🚀 — café"
    return _jobs_spec([r])


def _build_Y(base, rng):  # duplicate headers
    r = _seed_job(base)
    cols = JOB_COLUMNS + ["Notes"]  # Notes twice
    row = _row(r, JOB_COLUMNS)
    ordered = [row.get(c, "") for c in JOB_COLUMNS] + ["second notes col"]
    return {"All_Jobs": {"columns": cols, "rows": [dict(zip(_dedup_positional(cols), ordered))]}}


def _build_Z(base, rng):  # empty sheet (headers only)
    return _jobs_spec([])


def _build_AA(base, rng):  # interspersed blank rows
    r1 = _seed_job(base); r1["Job_ID"] = "BLANK-1"
    r2 = _seed_job(base); r2["Job_ID"] = "BLANK-2"
    blank = _row({}, JOB_COLUMNS)
    return _jobs_spec([r1, blank, r2, blank])


def _build_AB(base, rng):  # blank/merged header gap -> unnamed unknown column
    r = _seed_job(base)
    cols = list(JOB_COLUMNS)
    cols.insert(3, "")  # a blank header between real ones
    row = _row(r, JOB_COLUMNS)
    values = [row.get(c, "") for c in JOB_COLUMNS]
    values.insert(3, "orphan-value")
    return {"All_Jobs": {"columns": cols, "rows": [_positional_row(cols, values)]}}


def _build_AC(base, rng):  # company case alias, same job_id
    r = _seed_job(base); r["Company"] = "JPMorgan Chase"; r["Job_ID"] = "ALIAS-1"
    dup = dict(r); dup["Company"] = "JPMORGAN CHASE"
    return _jobs_spec([r, dup])


def _build_AD(base, rng):  # location variant on same job (content conflict)
    r = _seed_job(base); r["Job_ID"] = "LOC-1"; r["Location"] = "Bengaluru"
    dup = dict(r); dup["Location"] = "Bangalore"
    return _jobs_spec([r, dup])


def _build_AE(base, rng):  # work-mode variant (content conflict)
    r = _seed_job(base); r["Job_ID"] = "WM-1"; r["Work_Mode"] = "Hybrid"
    dup = dict(r); dup["Work_Mode"] = "hybrid remote"
    return _jobs_spec([r, dup])


def _build_AF(base, rng):  # experience-band variant (content conflict)
    r = _seed_job(base); r["Job_ID"] = "EX-1"; r["Experience"] = "1-2 years"
    dup = dict(r); dup["Experience"] = "2+ years"
    return _jobs_spec([r, dup])


def _build_AG(base, rng):  # unknown verification enum
    r = _seed_job(base); r["Verification_Status"] = "Totally Made Up"
    return _jobs_spec([r])


def _build_AH(base, rng):  # unknown live-status enum
    r = _seed_job(base); r["Live_Status"] = "Schrodinger"
    return _jobs_spec([r])


def _build_AI(base, rng):  # cross-sheet contradiction: Active here, Closed there
    r = _seed_job(base); r["Company"] = "Contradiction Inc"; r["Job_ID"] = "XS-1"; r["Live_Status"] = "Active"
    closed = _row(
        {"Company": "Contradiction Inc", "Job_ID": "XS-1", "Role": r["Role"],
         "Location": r["Location"], "Live_Status": "Closed", "Reason": "Position filled"},
        CLOSED_COLUMNS,
    )
    return {
        "All_Jobs": {"columns": JOB_COLUMNS, "rows": [r]},
        "Closed_or_Rejected": {"columns": CLOSED_COLUMNS, "rows": [closed]},
    }


def _build_AJ(base, rng):  # orphan closed job (not in All_Jobs)
    closed = _row(
        {"Company": "Orphan Ltd", "Job_ID": "ORP-1", "Role": "Engineer",
         "Location": "Pune", "Live_Status": "Closed", "Reason": "Expired"},
        CLOSED_COLUMNS,
    )
    return {
        "All_Jobs": {"columns": JOB_COLUMNS, "rows": [_seed_job(base)]},
        "Closed_or_Rejected": {"columns": CLOSED_COLUMNS, "rows": [closed]},
    }


def _build_AK(base, rng):  # company in New_Companies missing from Company_Coverage
    nc = _row(
        {"Company": "Uncovered Co", "Official_Domain": "uncovered.example.com",
         "ATS": "Workday", "Company_Type": "SaaS"},
        _COMPANY_COLUMNS,
    )
    return {"New_Companies": {"columns": _COMPANY_COLUMNS, "rows": [nc]}}


def _build_AL(base, rng):  # duplicate company across two coverage rows
    rows = [
        _row({"Company": "DupCo", "Tier": "1", "Result": "New Jobs"}, _COVERAGE_COLUMNS),
        _row({"Company": "DUPCO", "Tier": "1", "Result": "Known Unchanged"}, _COVERAGE_COLUMNS),
    ]
    return {"Company_Coverage": {"columns": _COVERAGE_COLUMNS, "rows": rows}}


def _build_AM(base, rng):  # negative count in coverage
    row = _row({"Company": "NegCo", "Relevant_Jobs": -5, "New_Jobs": 0}, _COVERAGE_COLUMNS)
    return {"Company_Coverage": {"columns": _COVERAGE_COLUMNS, "rows": [row]}}


def _build_AN(base, rng):  # non-integer where integer expected
    row = _row({"Company": "FloatCo", "Relevant_Jobs": 3.5}, _COVERAGE_COLUMNS)
    return {"Company_Coverage": {"columns": _COVERAGE_COLUMNS, "rows": [row]}}


def _build_AO(base, rng):  # leading-zero identifier preserved as string
    r = _seed_job(base); r["Job_ID"] = "0007734"
    return _jobs_spec([r])


def _build_AP(base, rng):  # SQL-injection-like content (must be inert)
    r = _seed_job(base); r["Notes"] = "Robert'); DROP TABLE jobs;--"
    return _jobs_spec([r])


def _build_AQ(base, rng):  # spreadsheet formula / CSV injection
    # NB: a leading '=' is stored by Excel/openpyxl as a *formula* and reads
    # back as None (no calc engine), which would defang the test. The '@'
    # prefix is an equally real OWASP CSV/DDE-injection trigger that survives
    # as literal text, so the validator genuinely sees and quarantines it.
    r = _seed_job(base); r["Notes"] = "@cmd|'/c calc'!A1"
    return _jobs_spec([r])


def _build_AR(base, rng):  # expansion 500 (represented)
    return _expansion_spec(base, 500, rng)


def _build_AS(base, rng):  # expansion 5000 (represented)
    return _expansion_spec(base, 5000, rng)


def _build_AT(base, rng):  # expansion 20000 (represented)
    return _expansion_spec(base, 20000, rng)


def _build_AU(base, rng):  # combined chaos (represents multiple cases at once)
    r = _seed_job(base)
    dup = dict(r)  # exact dup
    conflict = dict(r); conflict["Role"] = str(r["Role"]) + " *"  # conflict
    bad_score = dict(r); bad_score["Job_ID"] = "CHAOS-2"; bad_score["Match_Score"] = 999
    bad_url = dict(r); bad_url["Job_ID"] = "CHAOS-3"; bad_url["Source_URL"] = "nonsense"
    injection = dict(r); injection["Job_ID"] = "CHAOS-4"; injection["Notes"] = "@HYPERLINK('http://x')"
    unknown_enum = dict(r); unknown_enum["Job_ID"] = "CHAOS-5"; unknown_enum["Live_Status"] = "???"
    blank = _row({}, JOB_COLUMNS)
    rows = [r, dup, conflict, bad_score, bad_url, injection, unknown_enum, blank]
    return _jobs_spec(rows)


def _expansion_spec(base, n: int, rng: random.Random) -> WorkbookSpec:
    seed_row = _seed_job(base)
    rows = []
    for i in range(n):
        row = dict(seed_row)
        row["Job_ID"] = f"EXP-{i:06d}"
        row["Company"] = f"Company {i % 250}"
        row["Match_Score"] = rng.randint(40, 99)
        row["Location"] = rng.choice(["Bangalore", "Pune", "Hyderabad", "Chennai", "Remote"])
        rows.append(row)
    return _jobs_spec(rows)


def _dedup_positional(cols: list[str]) -> list[str]:
    seen: dict[str, int] = {}
    out = []
    for c in cols:
        if c in seen:
            seen[c] += 1
            out.append(f"{c}__{seen[c]}")
        else:
            seen[c] = 0
            out.append(c)
    return out


def _positional_row(cols: list[str], values: list[Any]) -> dict[str, Any]:
    keys = _dedup_positional(cols)
    return {keys[i]: (values[i] if i < len(values) else "") for i in range(len(keys))}


# Column sets for non-job sheets used above.
_COMPANY_COLUMNS = [
    "Company", "Official_Domain", "Careers_Domain", "ATS", "Company_Type",
    "India_Locations", "Source_Discovered_From", "Reason_Added", "Suggested_Tier",
    "Next_Delta_Check", "Next_Deep_Check",
]
_COVERAGE_COLUMNS = [
    "Company", "Tier", "Company_Type", "Official_Careers_Domain", "ATS", "Check_Type",
    "Checked_At", "Result", "Relevant_Jobs", "New_Jobs", "Known_Unchanged", "Closed",
    "Manual_Verification", "Access_Status", "Next_Delta_Check", "Next_Deep_Check",
]
_SOURCE_COLUMNS = [
    "Source", "Attempted", "Completed", "Searches_Performed", "Raw_Results",
    "Relevant_Results", "Verified_Official", "Portal_Only", "Manual_Verification",
    "Closed", "Access_Limited", "Notes",
]


SCENARIOS: list[Scenario] = [
    Scenario("A", "Exact duplicate row", "identity", "Identical row repeated", _build_A, ["rel:EXACT_DUPLICATE", "code:DUPLICATE_IDENTITY"]),
    Scenario("B", "Case-variant company", "identity", "Company name case differs", _build_B, ["code:DUPLICATE_IDENTITY"]),
    Scenario("C", "Whitespace-padded identity", "identity", "Leading/trailing spaces", _build_C, ["code:DUPLICATE_IDENTITY"]),
    Scenario("D", "Diacritic-variant company", "identity", "Accented characters fold", _build_D, ["code:DUPLICATE_IDENTITY"]),
    Scenario("E", "Whitespace/tab in Job_ID", "identity", "Trailing tab/space in id", _build_E, ["code:DUPLICATE_IDENTITY"]),
    Scenario("F", "Empty Job_ID", "identity", "Identity falls back to role/location", _build_F, ["records==1", "load_ok"]),
    Scenario("G", "Missing required column", "schema", "Company header dropped", _build_G, ["code:REQUIRED_MISSING", "quar>=1"]),
    Scenario("H", "Extra unknown column", "schema", "Unmapped column preserved", _build_H, ["unknown_col", "code:UNKNOWN_COLUMN"]),
    Scenario("I", "Reordered columns", "schema", "Columns in reverse order still map", _build_I, ["records==1"]),
    Scenario("J", "Renamed header alias", "schema", "Company_Name alias resolves", _build_J, ["records==1", "load_ok"]),
    Scenario("K", "Repost (new dates)", "identity", "Same job, later observation", _build_K, ["rel:REPOST"]),
    Scenario("L", "Multi-source", "identity", "Same job, two sources", _build_L, ["rel:MULTI_SOURCE"]),
    Scenario("M", "Closed status", "status", "Live_Status Closed", _build_M, ["rec_closed>=1"]),
    Scenario("N", "Conflicting role", "identity", "Same id, different role", _build_N, ["rel:CONFLICT"]),
    Scenario("O", "Non-numeric score", "value", "Match_Score text", _build_O, ["code:NON_NUMERIC"]),
    Scenario("P", "Out-of-range score", "value", "Match_Score > 100", _build_P, ["code:OUT_OF_RANGE_HIGH", "quar>=1"]),
    Scenario("Q", "Malformed URL", "value", "Non-URL in url field", _build_Q, ["code:MALFORMED_URL"]),
    Scenario("R", "Date format variants", "value", "Mixed date formats", _build_R, ["records==2", "load_ok"]),
    Scenario("S", "Future-dated posting", "value", "Posted_Date in the future", _build_S, ["records==1", "load_ok"]),
    Scenario("T", "Numeric stored as text", "value", "Score '88'", _build_T, ["records==1", "load_ok"]),
    Scenario("U", "Boolean-ish variants", "value", "Yes/TRUE/1/no", _build_U, ["records==2", "load_ok"]),
    Scenario("V", "Embedded newlines", "encoding", "CRLF/CR collapsed", _build_V, ["records==1", "load_ok"]),
    Scenario("W", "Very long cell", "encoding", "6k-char cell", _build_W, ["records==1", "load_ok"]),
    Scenario("X", "Emoji / non-ASCII", "encoding", "Emoji + accents", _build_X, ["records==1", "load_ok"]),
    Scenario("Y", "Duplicate headers", "schema", "Two Notes columns", _build_Y, ["dup_header"]),
    Scenario("Z", "Empty sheet", "schema", "Header-only sheet", _build_Z, ["records==0", "load_ok"]),
    Scenario("AA", "Blank rows interspersed", "schema", "Fully-blank rows skipped", _build_AA, ["records==2", "blank>=1"]),
    Scenario("AB", "Blank header gap", "schema", "Unnamed column carries value", _build_AB, ["code:UNKNOWN_COLUMN"]),
    Scenario("AC", "Company case alias", "identity", "Case-only company alias", _build_AC, ["code:DUPLICATE_IDENTITY"]),
    Scenario("AD", "Location variant", "identity", "Bengaluru vs Bangalore", _build_AD, ["rel:CONFLICT"]),
    Scenario("AE", "Work-mode variant", "identity", "Hybrid vs hybrid remote", _build_AE, ["rel:CONFLICT"]),
    Scenario("AF", "Experience variant", "identity", "Different experience band", _build_AF, ["rel:CONFLICT"]),
    Scenario("AG", "Unknown verification enum", "value", "Out-of-domain verification", _build_AG, ["code:UNKNOWN_ENUM_VALUE"]),
    Scenario("AH", "Unknown live-status enum", "value", "Out-of-domain live status", _build_AH, ["code:UNKNOWN_ENUM_VALUE"]),
    Scenario("AI", "Cross-sheet contradiction", "status", "Active vs Closed across sheets", _build_AI, ["rec_closed>=1"]),
    Scenario("AJ", "Orphan closed job", "status", "Closed job absent from All_Jobs", _build_AJ, ["rec_closed>=1"]),
    Scenario("AK", "Company missing coverage", "cross", "New company without coverage", _build_AK, ["load_ok"], represented=True),
    Scenario("AL", "Duplicate company coverage", "cross", "Same company twice in coverage", _build_AL, ["code:DUPLICATE_IDENTITY"]),
    Scenario("AM", "Negative count", "value", "Relevant_Jobs = -5", _build_AM, ["code:OUT_OF_RANGE_LOW", "quar>=1"]),
    Scenario("AN", "Non-integer count", "value", "Relevant_Jobs = 3.5", _build_AN, ["code:NON_INTEGER"]),
    Scenario("AO", "Leading-zero id", "value", "Job_ID 0007734 stays string", _build_AO, ["records==1", "load_ok"]),
    Scenario("AP", "SQL-injection text", "injection", "Inert SQL-looking text", _build_AP, ["records==1", "load_ok"]),
    Scenario("AQ", "Formula injection", "injection", "=cmd payload quarantined", _build_AQ, ["code:FORMULA_INJECTION", "quar>=1"]),
    Scenario("AR", "Expansion 500", "performance", "500 job rows", _build_AR, ["records==500"], represented=True),
    Scenario("AS", "Expansion 5000", "performance", "5,000 job rows", _build_AS, ["records==5000"], represented=True),
    Scenario("AT", "Expansion 20000", "performance", "20,000 job rows", _build_AT, ["records==20000"], represented=True),
    Scenario("AU", "Combined chaos", "combined", "Many hazards in one sheet", _build_AU, ["quar>=1", "code:DUPLICATE_IDENTITY"], represented=True),
]

assert [s.code for s in SCENARIOS] == (
    [chr(c) for c in range(ord("A"), ord("Z") + 1)]
    + ["A" + chr(c) for c in range(ord("A"), ord("U") + 1)]
), "Scenario catalog must be exactly A..AU (47 codes)."


def scenario_by_code(code: str) -> Optional[Scenario]:
    for s in SCENARIOS:
        if s.code == code:
            return s
    return None


# ---------------------------------------------------------------------------
# Workbook writing (always to a NEW file; original never touched)
# ---------------------------------------------------------------------------
def spec_to_workbook(spec: WorkbookSpec):
    reporter = ExcelReporter()
    sheets = []
    for sheet_name, payload in spec.items():
        columns = payload.get("columns")
        rows = payload.get("rows", [])
        sheets.append((sheet_name, columns, rows))
    if not sheets:
        sheets = [("All_Jobs", JOB_COLUMNS, [])]
    return reporter.build_workbook(sheets)


def save_workbook_deterministic(wb, out_path: Path) -> Path:
    """Save an openpyxl workbook so identical content yields identical bytes.

    openpyxl stamps each zip member with the wall-clock time, which makes raw
    file bytes non-deterministic across runs. We re-pack the archive with a
    fixed member timestamp and sorted entry order so byte-for-byte
    reproducibility holds (used by the deterministic-mutation tests).
    """
    import io
    import zipfile

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    buffer = io.BytesIO()
    wb.save(buffer)
    buffer.seek(0)
    with zipfile.ZipFile(buffer) as src, zipfile.ZipFile(
        out_path, "w", zipfile.ZIP_DEFLATED
    ) as dst:
        for name in sorted(src.namelist()):
            data = src.read(name)
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o600 << 16
            dst.writestr(info, data)
    return out_path


def write_spec(spec: WorkbookSpec, out_path: Path) -> Path:
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    wb = spec_to_workbook(spec)
    return save_workbook_deterministic(wb, out_path)


def generate_scenario_copy(
    base_rows: list[dict[str, Any]],
    scenario: Scenario,
    out_dir: Path,
    seed: int = DEFAULT_SEED,
) -> Path:
    rng = random.Random(f"{seed}:{scenario.code}")
    spec = scenario.build(base_rows, rng)
    out = Path(out_dir) / f"adversarial_{scenario.code}.xlsx"
    return write_spec(spec, out)


def generate_expansion(
    base_rows: list[dict[str, Any]],
    n: int,
    out_dir: Path,
    seed: int = DEFAULT_SEED,
) -> Path:
    rng = random.Random(f"{seed}:EXP:{n}")
    spec = _expansion_spec(base_rows, n, rng)
    out = Path(out_dir) / f"expansion_{n}.xlsx"
    return write_spec(spec, out)
