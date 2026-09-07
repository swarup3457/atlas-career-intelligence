"""Phase 1A.5: company discovery through the existing LangGraph governor,
including crash/resume. No LLM, no network."""

from __future__ import annotations

import pytest

from atlas.company.fixtures import build_discovery_workload
from atlas.company.registry import CompanyRegistry
from atlas.models import TaskStatus
from atlas.orchestration.checkpoints import open_checkpointer, thread_config
from atlas.orchestration.graph import build_graph, run_to_completion
from atlas.orchestration.state import RUN_STATUS_COMPLETE, initial_queue_state
from atlas.persistence.sqlite import StateStore
from atlas.workers.company import CompanyDiscoveryWorker

pytestmark = pytest.mark.integration


def test_company_discovery_workload(tmp_path):
    plan, transient = build_discovery_workload()
    with StateStore(tmp_path / "state.sqlite") as store:
        reg = CompanyRegistry(store)
        worker = CompanyDiscoveryWorker(reg, plan, simulated_first_attempt_failures=transient)
        with open_checkpointer(tmp_path / "cp.sqlite") as cp:
            graph = build_graph(worker, retry_budget=2).compile(checkpointer=cp)
            final = run_to_completion(graph, thread_config("disc"), initial_queue_state(list(plan)))

        assert final["run_status"] == RUN_STATUS_COMPLETE
        # every planned company reached a terminal state, none repeated
        assert sorted(final["completed_items"]) == sorted(plan)
        assert len(final["completed_items"]) == len(set(final["completed_items"]))
        # a retryable transient failure was retried (>= one extra attempt)
        assert len(final["attempt_log"]) > len(plan)
        # malformed observation is EXTRACTION_UNRESOLVED, everything else SUCCESS
        assert final["item_results"]["malformed"]["status"] == TaskStatus.EXTRACTION_UNRESOLVED.value

        # registry is correct: no duplicate companies / source instances
        assert store.count_companies() == 15
        assert store.count_source_relationships() == 13
        # lookalikes are distinct companies (not merged)
        assert (worker.results["look-1"].company.company_id
                != worker.results["look-2"].company.company_id)
        # untrusted posting registered no source
        assert worker.results["untrusted"].source_registered is False


def test_crash_and_resume(tmp_path):
    plan, transient = build_discovery_workload()
    state_db = tmp_path / "state.sqlite"
    cp_db = tmp_path / "cp.sqlite"
    cfg = thread_config("disc-crash")
    items = list(plan)

    # --- Session 1: process a few items then "crash". ---
    with StateStore(state_db) as store:
        worker = CompanyDiscoveryWorker(CompanyRegistry(store), plan, simulated_first_attempt_failures=transient)
        with open_checkpointer(cp_db) as cp:
            graph = build_graph(worker, retry_budget=2).compile(checkpointer=cp)
            state = graph.invoke(initial_queue_state(items), cfg)
            while len(state.get("completed_items", [])) < 5 and state.get("run_status") != RUN_STATUS_COMPLETE:
                state = graph.invoke({}, cfg)
        partial = list(state["completed_items"])
    assert 0 < len(partial) < len(items)

    # --- Session 2: fresh process resumes from the checkpoint + same DB. ---
    with StateStore(state_db) as store:
        reg = CompanyRegistry(store)
        worker = CompanyDiscoveryWorker(reg, plan, simulated_first_attempt_failures=transient)
        with open_checkpointer(cp_db) as cp:
            graph = build_graph(worker, retry_budget=2).compile(checkpointer=cp)
            existing = graph.get_state(cfg).values
            final = run_to_completion(graph, cfg, existing if existing else initial_queue_state(items))

        assert final["run_status"] == RUN_STATUS_COMPLETE
        assert sorted(final["completed_items"]) == sorted(items)
        assert len(final["completed_items"]) == len(set(final["completed_items"]))
        # No duplicate companies/sources despite the crash + resume.
        assert store.count_companies() == 15
        assert store.count_source_relationships() == 13
        # aliases preserved across the crash (Acme variant alias survived)
        acme = reg.find_company("Acme", "acme.com")
        assert acme is not None and any("Global" in a for a in acme.aliases)
