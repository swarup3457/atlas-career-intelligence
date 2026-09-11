"""Attempt-ceiling and job-lead task-model tests (PRODUCTION R1 §7, §13)."""

from __future__ import annotations

from pathlib import Path

import pytest

from atlas.persistence.sqlite import StateStore
from atlas.vscode_hunt.models import (
    ABSOLUTE_MAX_ATTEMPTS,
    TERMINAL_LEAD_CLASSIFICATIONS,
    VERIFICATION_BATCH_MAX_LEADS,
    Backend,
    LeadClassification,
    TaskKind,
    batch_is_complete,
)
from atlas.vscode_hunt.service import VscodeHuntService


def test_task_kinds_and_lead_classifications_are_the_r1_set() -> None:
    assert {k.value for k in TaskKind} == {
        "DISCOVERY_QUERY_BATCH", "VERIFY_JOB_LEAD_BATCH",
        "EXPAND_VERIFIED_COMPANY", "REVERIFY_HISTORICAL_JOB",
    }
    assert TERMINAL_LEAD_CLASSIFICATIONS == {
        "VERIFIED_ACCEPTED", "VERIFIED_STRETCH", "VERIFIED_REJECTED", "PORTAL_ONLY_UNVERIFIED",
        "CLOSED", "FOREIGN", "DUPLICATE", "SOURCE_UNAVAILABLE", "INTERNAL_ERROR",
    }
    assert VERIFICATION_BATCH_MAX_LEADS == 8


def test_batch_complete_only_when_every_lead_is_terminal() -> None:
    assert not batch_is_complete([])
    assert batch_is_complete([LeadClassification.VERIFIED_ACCEPTED.value, LeadClassification.CLOSED.value])
    assert not batch_is_complete(["VERIFIED_ACCEPTED", "IN_PROGRESS"])


def test_fourth_attempt_is_rejected(tmp_path: Path) -> None:
    with StateStore(tmp_path / "state.sqlite") as store:
        service = VscodeHuntService(store, tmp_path / "out")
        run_id = service.create_run([{"company_id": "co", "name": "Co", "official_domain": "co.example"}])
        task = service.next_tasks(run_id, materialize=True)[0]
        for i in range(ABSOLUTE_MAX_ATTEMPTS):
            service.record_attempt(run_id, task["task_id"], f"attempt-{i + 1}", Backend.VSCODE_SUBAGENT)
        with pytest.raises(ValueError, match="ceiling"):
            service.record_attempt(run_id, task["task_id"], "attempt-4", Backend.VSCODE_SUBAGENT)
