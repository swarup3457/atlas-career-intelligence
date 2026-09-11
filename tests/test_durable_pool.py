from __future__ import annotations

import json
from pathlib import Path

from atlas.persistence.sqlite import StateStore
from atlas.vscode_hunt.models import Backend
from atlas.vscode_hunt.service import VscodeHuntService


def envelope(run_id: str, task_id: str, attempt: str) -> dict:
    lanes = ["JAVA_BACKEND", "JAVA_FULLSTACK", "REACT_FRONTEND", "DOTNET", "ENTERPRISE_HR_PAYROLL_INTEGRATION"]
    return {"schema_version":2,"run_id":run_id,"task_id":task_id,"attempt_id":attempt,"company_id":"co","worker_invocation_id":"w","official_domain":"co.com","career_url":"https://co.com/careers","queries":[],"lanes_attempted":lanes,"result_states":{lane:{"state":"no_results"} for lane in lanes},"detail_urls":[],"jobs":[],"rejections":[],"foreign_leads":[],"browser_errors":[],"source_health":{"status":"healthy"},"external_block_evidence":"","evidence_quotes":[],"completion_claim":True}


def test_commit_is_idempotent_and_finalization_requires_commit(tmp_path: Path):
    with StateStore(tmp_path / "state.sqlite") as store:
        service=VscodeHuntService(store,tmp_path/"out")
        run=service.create_run([{"company_id":"co","name":"Co","official_domain":"co.com","lanes":["JAVA_BACKEND","JAVA_FULLSTACK","REACT_FRONTEND","DOTNET","ENTERPRISE_HR_PAYROLL_INTEGRATION"]}])
        task=service.next_tasks(run)[0]
        attempt=service.record_attempt(run,task["task_id"],"attempt-1",Backend.VSCODE_SUBAGENT)
        source=tmp_path/"result.json"; source.write_text(json.dumps(envelope(run,task["task_id"],attempt.attempt_id)),encoding="utf-8")
        first=service.commit_result(run,task["task_id"],attempt.attempt_id,source)
        second=service.commit_result(run,task["task_id"],attempt.attempt_id,source)
        assert first["commit_id"] == second["commit_id"]
        assert service.finalize_run(run)["can_finish"] is True


def test_commit_conflict_is_rejected(tmp_path: Path):
    with StateStore(tmp_path / "state.sqlite") as store:
        service=VscodeHuntService(store,tmp_path/"out")
        run=service.create_run([{"company_id":"co","name":"Co","official_domain":"co.com"}])
        task=service.next_tasks(run)[0]; attempt=service.record_attempt(run,task["task_id"],"attempt-1",Backend.VSCODE_SUBAGENT)
        source=tmp_path/"result.json"; source.write_text(json.dumps(envelope(run,task["task_id"],attempt.attempt_id)),encoding="utf-8")
        service.commit_result(run,task["task_id"],attempt.attempt_id,source)
        source.write_text(json.dumps({"changed":True}),encoding="utf-8")
        try:
            service.commit_result(run,task["task_id"],attempt.attempt_id,source)
        except ValueError as exc:
            assert "conflict" in str(exc)
        else:
            raise AssertionError("expected commit conflict")
