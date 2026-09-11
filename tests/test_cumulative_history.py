"""Cumulative job history + master workbook tests (PRODUCTION R1 §3, §15)."""

from __future__ import annotations

from pathlib import Path

import pytest
from openpyxl import load_workbook

from atlas.persistence.sqlite import StateStore
from atlas.reporting.trust_boundary import partition_jobs
from atlas.vscode_hunt.history import (
    MASTER_SHEETS,
    build_master_workbook,
    canonical_job_id,
    mark_closed,
    mark_source_unavailable,
    record_run_jobs,
)


def _valid(url: str = "https://jobs.lever.co/acme/eng-123", title: str = "Java Backend Engineer") -> dict:
    return {
        "proposed_decision": "accept", "title": title, "official_url": url,
        "location": "Bengaluru, India", "lane": "JAVA_BACKEND",
        "description": (
            "We are hiring a Java Backend Engineer to build Spring Boot microservices in "
            "Java. Requires strong Java, JVM and Spring experience. 2+ years experience."
        ),
        "experience_text": "2+ years", "requisition_id": "REQ-1", "posted_date": "2026-09-01",
        "stack": ["Java", "Spring Boot"], "evidence_snippets": ["spring boot microservices"],
    }


def _vjob(url: str = "https://jobs.lever.co/acme/eng-123", title: str = "Java Backend Engineer"):
    validated, rejected = partition_jobs([_valid(url, title)], official_domain="acme.com", company="Acme")
    assert validated, rejected
    return validated[0]


def _rows(path: Path, sheet: str) -> list:
    wb = load_workbook(path, read_only=True)
    rows = list(wb[sheet].iter_rows(min_row=2, values_only=True))
    wb.close()
    return rows


def test_record_lifecycle_new_unchanged_updated(tmp_path: Path) -> None:
    with StateStore(tmp_path / "state.sqlite") as store:
        first = record_run_jobs(store, run_id="run1", validated_jobs=[_vjob()])
        assert first["new"] == 1
        again = record_run_jobs(store, run_id="run2", validated_jobs=[_vjob()])
        assert again["unchanged"] == 1
        changed = record_run_jobs(store, run_id="run3", validated_jobs=[_vjob(title="Java Backend Engineer II")])
        assert changed["updated"] == 1

        cid = canonical_job_id("https://jobs.lever.co/acme/eng-123", "Acme", "REQ-1")
        job = store.get_canonical_job(cid)
        assert job["current_status"] == "UPDATED"
        assert job["first_seen"]  # preserved across runs


def test_prior_job_survives_an_unrelated_later_run(tmp_path: Path) -> None:
    with StateStore(tmp_path / "state.sqlite") as store:
        record_run_jobs(store, run_id="run1", validated_jobs=[_vjob(url="https://jobs.lever.co/acme/eng-1", title="Java Backend Engineer")])
        record_run_jobs(store, run_id="run2", validated_jobs=[_vjob(url="https://jobs.lever.co/acme/eng-2", title="Java Backend Engineer")])
        output = build_master_workbook(store, tmp_path / "out")

    active = _rows(output, "Active_Verified_Jobs")
    urls = {row[4] for row in active}
    assert "https://jobs.lever.co/acme/eng-1" in urls
    assert "https://jobs.lever.co/acme/eng-2" in urls  # earlier job did not disappear


def test_mark_closed_requires_evidence(tmp_path: Path) -> None:
    with StateStore(tmp_path / "state.sqlite") as store:
        record_run_jobs(store, run_id="run1", validated_jobs=[_vjob()])
        cid = canonical_job_id("https://jobs.lever.co/acme/eng-123", "Acme", "REQ-1")
        with pytest.raises(ValueError):
            mark_closed(store, cid, evidence="")
        mark_closed(store, cid, evidence="detail page returns 'no longer accepting applications'")
        output = build_master_workbook(store, tmp_path / "out")

    assert not _rows(output, "Active_Verified_Jobs")
    closed = _rows(output, "Closed_Jobs")
    assert len(closed) == 1 and closed[0][0] == cid


def test_source_unavailable_moves_not_deletes(tmp_path: Path) -> None:
    with StateStore(tmp_path / "state.sqlite") as store:
        record_run_jobs(store, run_id="run1", validated_jobs=[_vjob()])
        cid = canonical_job_id("https://jobs.lever.co/acme/eng-123", "Acme", "REQ-1")
        mark_source_unavailable(store, cid, reason="ATS 503")
        assert store.get_canonical_job(cid) is not None  # never deleted
        output = build_master_workbook(store, tmp_path / "out")

    assert not _rows(output, "Active_Verified_Jobs")
    reverify = _rows(output, "Reverify_Required")
    assert len(reverify) == 1 and reverify[0][0] == cid


def test_master_workbook_sheets_and_immutability(tmp_path: Path) -> None:
    with StateStore(tmp_path / "state.sqlite") as store:
        record_run_jobs(store, run_id="run1", validated_jobs=[_vjob()])
        first = build_master_workbook(store, tmp_path / "out")
        second = build_master_workbook(store, tmp_path / "out")

    assert first != second  # never overwrites
    wb = load_workbook(first, read_only=True)
    assert wb.sheetnames == list(MASTER_SHEETS)
    wb.close()
