"""Historical importer tests (PRODUCTION R1 §1A)."""

from __future__ import annotations

from pathlib import Path

from openpyxl import Workbook

from atlas.persistence.sqlite import StateStore
from atlas.reporting.trust_boundary import partition_jobs
from atlas.vscode_hunt.historical_import import import_historical_workbooks
from atlas.vscode_hunt.history import canonical_job_id, record_run_jobs

_VALID_ROW = ["Acme", "Java Backend Engineer", "Bengaluru, India", "https://jobs.lever.co/acme/eng-123", "REQ-1", "APPLY_NOW"]
_BLANK_URL_ROW = ["Drivetrain", "Software Engineer", "Bengaluru, India", "", "", ""]  # Drivetrain shape
_MISSING_LOCATION_ROW = ["Foo", "Bar", "", "https://foo.example/1", "", ""]


def _make_workbook(path: Path) -> None:
    wb = Workbook()
    ws = wb.active
    ws.title = "Validated_Jobs"
    ws.append(["company", "title", "location", "official_url", "requisition", "recommendation"])
    for row in (_VALID_ROW, _BLANK_URL_ROW, _MISSING_LOCATION_ROW):
        ws.append(row)
    wb.save(path)


def test_import_rejects_bad_rows_and_is_idempotent(tmp_path: Path) -> None:
    wb_dir = tmp_path / "wb"
    wb_dir.mkdir()
    _make_workbook(wb_dir / "Atlas_Run_20260909-2130.xlsx")

    with StateStore(tmp_path / "state.sqlite") as store:
        manifest = import_historical_workbooks(store, search_roots=[wb_dir], manifest_path=tmp_path / "manifest.json")
        assert manifest["files_accepted"] == 1
        assert manifest["rows_examined"] == 3
        assert manifest["rows_imported"] == 1
        assert manifest["rows_rejected"] == 2
        assert manifest["rejection_reasons"]["BLANK_OR_NON_HTTPS_URL"] == 1
        assert manifest["rejection_reasons"]["MISSING_LOCATION"] == 1
        assert len(manifest["source_file_sha256"]) == 1

        cid = canonical_job_id("https://jobs.lever.co/acme/eng-123", "Acme", "REQ-1")
        job = store.get_canonical_job(cid)
        assert job is not None and job["current_status"] == "REVERIFY_REQUIRED"

        # Re-import the same file: no new observations, all duplicates.
        again = import_historical_workbooks(store, search_roots=[wb_dir])
        assert again["rows_imported"] == 0
        assert again["duplicates"] == 1


def test_import_never_downgrades_an_active_job(tmp_path: Path) -> None:
    wb_dir = tmp_path / "wb"
    wb_dir.mkdir()
    _make_workbook(wb_dir / "Atlas_Run_20260909-2130.xlsx")

    valid_proposal = {
        "proposed_decision": "accept", "title": "Java Backend Engineer",
        "official_url": "https://jobs.lever.co/acme/eng-123", "location": "Bengaluru, India",
        "lane": "JAVA_BACKEND",
        "description": "Java Backend Engineer building Spring Boot microservices in Java. Strong Java, JVM and Spring. 2+ years experience.",
        "experience_text": "2+ years", "requisition_id": "REQ-1", "posted_date": "2026-09-01",
        "stack": ["Java", "Spring Boot"], "evidence_snippets": ["spring boot microservices"],
    }
    with StateStore(tmp_path / "state.sqlite") as store:
        validated, _ = partition_jobs([valid_proposal], official_domain="acme.com", company="Acme")
        record_run_jobs(store, run_id="live1", validated_jobs=validated)  # -> NEW (active family)
        cid = canonical_job_id("https://jobs.lever.co/acme/eng-123", "Acme", "REQ-1")
        assert store.get_canonical_job(cid)["current_status"] == "NEW"

        import_historical_workbooks(store, search_roots=[wb_dir])
        # Still active — a prior workbook appearance must not downgrade it.
        assert store.get_canonical_job(cid)["current_status"] == "NEW"
