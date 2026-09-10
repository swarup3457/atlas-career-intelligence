"""Offline tests for the Copilot-CLI + Playwright-MCP browser backend.

No real web, no real Copilot process (except a deterministic local subprocess for
the process-tree cleanup test). Covers install/config, process, policy/result,
recipe, and governor contracts.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

from atlas.browser_backend import MCP_LICENSE, MCP_PACKAGE, MCP_VERSION
from atlas.browser_backend.models import (
    BackendResult, CliProcessConfig, CompanyTask, MCPConfig, ROUTE_CLI_PLAYWRIGHT,
    ROUTE_STRUCTURED,
)
from atlas.browser_backend import cli_process, mcp_config, recipes, validation
from atlas.browser_backend.hybrid import (
    CompletionLedger, HybridRouter, MAX_COMPANY_CONCURRENCY, run_company_with_policy,
)
from atlas.browser_backend.jsonl_parser import extract_result_objects, final_assistant_text
from atlas.browser_backend.cli_playwright import parse_usage_credits
from atlas.pilot.status_v4 import CompanySearchStatus

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]


# --------------------------------------------------------------------------- #
# INSTALL / CONFIG
# --------------------------------------------------------------------------- #
def test_mcp_version_and_license_pinned():
    assert MCP_PACKAGE == "@playwright/mcp"
    assert MCP_VERSION == "0.0.80"
    assert MCP_LICENSE == "Apache-2.0"


def test_tool_manifest_pins_exact_version_no_latest():
    pj = json.loads((REPO_ROOT / "tools" / "playwright-mcp" / "package.json").read_text(encoding="utf-8"))
    assert pj["dependencies"]["@playwright/mcp"] == "0.0.80"
    assert "latest" not in json.dumps(pj).lower()
    assert pj["engines"]["node"] == ">=18"


def test_mcp_config_absolute_cli_and_bounded_timeouts():
    cfg = MCPConfig(cli_path=str(Path("/abs/cli.js").resolve()), output_dir="/tmp/o", user_data_dir="/tmp/ctx")
    d = cfg.to_config_dict()
    args = d["mcpServers"]["playwright"]["args"]
    assert d["mcpServers"]["playwright"]["command"] == "node"
    assert Path(args[0]).is_absolute()
    assert "--snapshot-mode" in args and "full" in args
    assert "--image-responses" in args and "omit" in args
    assert "--timeout-action" in args and "10000" in args
    assert "--timeout-navigation" in args and "90000" in args
    assert "--isolated" in args


def test_mcp_config_written_utf8_without_bom(tmp_path):
    task = CompanyTask(company="Acme")
    cfg = mcp_config.build_mcp_config(task, tmp_path, cli_path="/abs/cli.js")
    path = mcp_config.write_mcp_config(cfg, tmp_path / "mcp.json")
    raw = path.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf")  # no BOM
    assert json.loads(raw.decode("utf-8"))["mcpServers"]["playwright"]["command"] == "node"


def test_unique_context_and_output_per_task(tmp_path):
    t1, t2 = CompanyTask(company="Acme"), CompanyTask(company="Acme")
    c1 = mcp_config.build_mcp_config(t1, tmp_path / "a", cli_path="/abs/cli.js")
    c2 = mcp_config.build_mcp_config(t2, tmp_path / "b", cli_path="/abs/cli.js")
    assert c1.output_dir != c2.output_dir
    assert c1.user_data_dir != c2.user_data_dir  # never a shared persistent profile


def test_default_run_is_headless_canary_is_headed(tmp_path):
    normal = mcp_config.build_mcp_config(CompanyTask(company="Acme"), tmp_path / "n", cli_path="/c.js")
    headed = mcp_config.build_mcp_config(CompanyTask(company="Acme"), tmp_path / "h", cli_path="/c.js", headed=True)
    assert "--headless" in normal.to_config_dict()["mcpServers"]["playwright"]["args"]
    assert "--no-headless" in headed.to_config_dict()["mcpServers"]["playwright"]["args"]


# --------------------------------------------------------------------------- #
# PROCESS
# --------------------------------------------------------------------------- #
def _args_for(company: str, tmp_path: Path) -> list[str]:
    task = CompanyTask(company=company, query_terms=(company,))
    return cli_process.build_copilot_args(
        task, CliProcessConfig(), prompt=f"search {company}",
        mcp_config_path=tmp_path / "mcp.json", usage_path=tmp_path / "u.json", log_dir=tmp_path / "logs",
    )


def test_args_are_a_list_prompt_is_single_element(tmp_path):
    args = _args_for("Acme", tmp_path)
    assert isinstance(args, list)
    i = args.index("--prompt")
    assert args[i + 1] == "search Acme"
    assert "--model" in args and "claude-sonnet-5" in args
    assert "--output-format" in args and "json" in args
    assert "--no-ask-user" in args and "--no-remote-export" in args
    assert "--disable-builtin-mcps" in args


def test_company_query_cannot_inject_shell(tmp_path):
    evil = 'Acme & del C:\\ | rm -rf ~ > x'
    task = CompanyTask(company=evil, query_terms=(evil,))
    prompt = f"target company {evil}"
    args = cli_process.build_copilot_args(
        task, CliProcessConfig(), prompt=prompt,
        mcp_config_path=tmp_path / "m.json", usage_path=tmp_path / "u.json", log_dir=tmp_path / "l")
    # The untrusted value is one argv element, never split into shell tokens.
    assert prompt in args
    argv = cli_process.finalize_argv(Path("copilot.cmd"), args)
    assert prompt in argv  # still a single element after wrapping
    assert not any(el in ("&", "|", ">", "rm", "del") for el in argv)


def test_copilot_cmd_wrapped_via_comspec(tmp_path):
    args = _args_for("Acme", tmp_path)
    argv = cli_process.finalize_argv(Path("C:/x/copilot.cmd"), args)
    assert argv[1] == "/c"
    assert argv[2].lower().endswith("copilot.cmd")
    # ps1 shim path
    ps = cli_process.finalize_argv(Path("C:/x/copilot.ps1"), args)
    assert "-File" in ps and ps[-len(args) - 1].lower().endswith("copilot.ps1")


def test_prompt_shell_safety_guard():
    assert cli_process.prompt_is_shell_safe("plain prompt")
    assert not cli_process.prompt_is_shell_safe("bad %PATH% expansion")
    assert not cli_process.prompt_is_shell_safe("delayed !VAR! expansion")


def test_jsonl_final_assistant_and_result_extraction():
    obj = {"atlas_result_version": 1, "company": "Acme", "status": "SEARCHED_COMPLETE_NO_MATCHES"}
    events = [
        {"type": "tool", "name": "playwright_navigate"},
        {"role": "assistant", "text": "Here is the result:\n```json\n" + json.dumps(obj) + "\n```"},
    ]
    raw = "\n".join(json.dumps(e) for e in events)
    from atlas.browser_backend.jsonl_parser import iter_jsonl
    parsed = list(iter_jsonl(raw))
    text = final_assistant_text(parsed)
    results = extract_result_objects(text)
    assert len(results) == 1 and results[0]["company"] == "Acme"


def test_multiple_result_objects_detected():
    a = json.dumps({"company": "A", "status": "SEARCHED_COMPLETE_NO_MATCHES"})
    b = json.dumps({"company": "B", "status": "SEARCHED_COMPLETE_NO_MATCHES"})
    text = f"```json\n{a}\n```\nand also\n```json\n{b}\n```"
    assert len(extract_result_objects(text)) == 2


def test_usage_credits_parsed(tmp_path):
    up = tmp_path / "usage.json"
    up.write_text(json.dumps({"models": [{"total_nano_aiu": 44_360_000_000}], "extra": {"ai_credits": 0.0}}),
                  encoding="utf-8")
    assert parse_usage_credits(up) == pytest.approx(44.36, abs=0.01)


def test_missing_copilot_returns_127(tmp_path):
    task = CompanyTask(company="Acme")
    cap = cli_process.run_company_process(
        task, CliProcessConfig(), prompt="hi", mcp_config_path=tmp_path / "m.json",
        output_dir=tmp_path / "out", copilot_path=Path(tmp_path / "does-not-exist"))
    assert cap.exit_code == 127


@pytest.mark.slow
def test_process_tree_terminated_by_pid_not_name(tmp_path):
    import subprocess
    kwargs = {}
    if sys.platform.startswith("win"):
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        import os
        kwargs["preexec_fn"] = os.setsid
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], **kwargs)
    assert proc.poll() is None
    cli_process.terminate_tree(proc)
    for _ in range(50):
        if proc.poll() is not None:
            break
        time.sleep(0.1)
    assert proc.poll() is not None  # tree cleaned up
    # No-op on an already-finished process (never touches unrelated processes).
    cli_process.terminate_tree(proc)


# --------------------------------------------------------------------------- #
# POLICY / RESULT
# --------------------------------------------------------------------------- #
def _good_job(**over):
    job = {
        "title": "Java Full Stack Engineer",
        "location": "Bengaluru, India",
        "official_url": "https://careers.example.com/jobs/eng-123",
        "description": "Build Spring Boot microservices with a React frontend. 3 years experience.",
        "mandatory_requirements": ["Java and Spring Boot", "React"],
        "preferred_requirements": [],
        "experience_text": "3 years",
        "lane": "JAVA_FULLSTACK",
        "evidence_snippets": ["Spring Boot microservices with a React frontend"],
    }
    job.update(over)
    return job


def _task():
    return CompanyTask(company="Example", official_domain="example.com", run_id="t", task_id="tid")


def test_canonical_official_url_required():
    assert validation.is_official_or_ats_url("https://careers.example.com/j/1", "example.com")
    assert validation.is_official_or_ats_url("https://boards.greenhouse.io/acme/jobs/1", "acme.com")
    assert not validation.is_official_or_ats_url("http://example.com/j", "example.com")  # not https
    assert not validation.is_official_or_ats_url("https://evil.test/j", "example.com")   # not official/ATS


def test_java_spring_react_accepted():
    v = validation.validate_job_evidence(_good_job(), official_domain="example.com", company="Example")
    assert v.rejection is None and v.accepted is not None
    assert v.accepted.title == "Java Full Stack Engineer"


def test_python_fullstack_react_rejected():
    job = _good_job(title="Full Stack Developer", description="Python, Django and React frontend. 2 years.",
                    mandatory_requirements=["Python", "Django", "React"],
                    evidence_snippets=["Python, Django and React frontend"])
    v = validation.validate_job_evidence(job, official_domain="example.com", company="Example")
    assert v.accepted is None and v.rejection is not None


def test_java_8_is_a_version_not_eight_years():
    job = _good_job(title="Java Backend Engineer", lane="JAVA_BACKEND",
                    description="Experience with Java 8 or above and Spring Boot. 2 years.",
                    mandatory_requirements=["Java 8 or above", "Spring Boot"],
                    evidence_snippets=["Java 8 or above and Spring Boot"])
    v = validation.validate_job_evidence(job, official_domain="example.com", company="Example")
    assert v.accepted is not None, v.failures


@pytest.mark.parametrize("phrase", ["5+ years", "7.5+ years", "8+ years", "12+ years"])
def test_high_experience_rejected(phrase):
    job = _good_job(title="Java Backend Engineer", lane="JAVA_BACKEND",
                    description=f"Spring Boot backend. {phrase} of experience required.",
                    mandatory_requirements=["Java", "Spring Boot"],
                    evidence_snippets=[f"{phrase} of experience required"])
    v = validation.validate_job_evidence(job, official_domain="example.com", company="Example")
    assert v.accepted is None
    assert v.rejection.reason_code == "EXPERIENCE_TOO_HIGH"


@pytest.mark.parametrize("loc", ["London, United Kingdom", "Dallas, United States", "Somewhere Unknown"])
def test_foreign_or_unknown_location_rejected(loc):
    v = validation.validate_job_evidence(_good_job(location=loc), official_domain="example.com", company="Example")
    assert v.accepted is None and v.rejection.reason_code == "NON_INDIA_LOCATION"


def test_empty_title_rejected():
    v = validation.validate_job_evidence(_good_job(title=""), official_domain="example.com", company="Example")
    assert v.accepted is None and v.rejection.reason_code in ("NOT_A_JOB_TITLE", "NON_OFFICIAL_URL")


def test_ungrounded_quote_rejected():
    v = validation.validate_job_evidence(_good_job(evidence_snippets=["a quote that is not on the page"]),
                                         official_domain="example.com", company="Example")
    assert v.accepted is None and v.rejection.reason_code == "UNGROUNDED_QUOTE"


def test_closed_posting_rejected():
    v = validation.validate_job_evidence(
        _good_job(description="Spring Boot role. This job is closed. 2 years.",
                  evidence_snippets=["Spring Boot role"]),
        official_domain="example.com", company="Example")
    assert v.accepted is None and v.rejection.reason_code == "CLOSED_POSTING"


def test_result_contract_good_object_passes():
    obj = {
        "atlas_result_version": 1, "company": "Example", "task_id": "tid", "run_id": "t",
        "status": "SEARCHED_COMPLETE_WITH_MATCHES", "browser_evidence": True,
        "observed_result_state": "results_observed",
        "evidence_urls": ["https://careers.example.com/jobs/eng-123"],
        "jobs": [_good_job()],
    }
    res = validation.validate_result_object(obj, _task(), custom_site=True)
    assert res["failures"] == [] and res["browser_evidence"] is True


def test_card_only_pass_rejected():
    obj = {"company": "Example", "task_id": "tid", "run_id": "t",
           "status": "SEARCHED_COMPLETE_WITH_MATCHES", "browser_evidence": True,
           "jobs": [{"title": "Java Engineer", "location": "India", "official_url": "https://careers.example.com/j/1"}]}
    res = validation.validate_result_object(obj, _task())
    assert any(f.startswith("CARD_ONLY_PASS") for f in res["failures"])


def test_no_browser_evidence_for_custom_completion_rejected():
    obj = {"company": "Example", "task_id": "tid", "run_id": "t",
           "status": "SEARCHED_COMPLETE_NO_MATCHES", "browser_evidence": False, "jobs": []}
    res = validation.validate_result_object(obj, _task(), custom_site=True)
    assert any(f.startswith("NO_BROWSER_EVIDENCE") for f in res["failures"])


def test_false_external_block_rejected():
    obj = {"company": "Example", "task_id": "tid", "run_id": "t",
           "status": "ACCESS_LIMITED_EXTERNAL", "external_block": True,
           "external_block_evidence": "", "browser_evidence": False, "jobs": []}
    res = validation.validate_result_object(obj, _task())
    assert any(f.startswith("UNPROVEN_EXTERNAL_BLOCK") for f in res["failures"])


def test_company_mismatch_rejected():
    obj = {"company": "Other", "task_id": "tid", "run_id": "t",
           "status": "SEARCHED_COMPLETE_NO_MATCHES", "browser_evidence": True, "jobs": []}
    res = validation.validate_result_object(obj, _task())
    assert any(f.startswith("COMPANY_MISMATCH") for f in res["failures"])


def test_build_validated_result_downgrades_when_no_valid_jobs():
    obj = {"company": "Example", "official_domain": "example.com", "task_id": "tid", "run_id": "t",
           "status": "SEARCHED_COMPLETE_WITH_MATCHES", "browser_evidence": True,
           "jobs": [_good_job(location="London, UK")]}  # foreign => not accepted
    result = validation.build_validated_result(obj, _task())
    assert result.status == "SEARCHED_COMPLETE_NO_MATCHES"
    assert result.jobs == [] and len(result.rejections) == 1


# --------------------------------------------------------------------------- #
# RECIPE
# --------------------------------------------------------------------------- #
def _recipe(**over):
    r = {"company": "Example", "official_domain": "example.com",
         "search_url_template": "https://careers.example.com/search?q={q}&loc={loc}",
         "detail_url_pattern": "https://careers.example.com/careers/jobdetails/{slug}",
         "query_param": "q", "location_param": "loc",
         "navigation_strategy": "canonical_href_then_direct_navigation",
         "result_observation_checks": ["results list visible"]}
    r.update(over)
    return r


def test_recipe_append_only_versions(tmp_path):
    store = recipes.RecipeStore(tmp_path / "recipes.jsonl")
    v1 = store.add_candidate(_recipe())
    v2 = store.add_candidate(_recipe(query_param="keyword"))
    assert v1.version == 1 and v2.version == 2


def test_recipe_job_specific_id_rejected():
    with pytest.raises(recipes.RecipeRejected):
        recipes.sanitize_recipe(_recipe(job_id="REQ-99999"))
    with pytest.raises(recipes.RecipeRejected):
        recipes.sanitize_recipe(_recipe(detail_url_pattern="https://careers.example.com/careers/jobdetails/123456"))


def test_recipe_raw_javascript_rejected():
    with pytest.raises(recipes.RecipeRejected):
        recipes.sanitize_recipe(_recipe(search_url_template="javascript:document.location='x'"))


def test_recipe_nonofficial_host_rejected():
    with pytest.raises(recipes.RecipeRejected):
        recipes.sanitize_recipe(_recipe(search_url_template="https://evil.test/search?q={q}"))


def test_recipe_secrets_and_unbounded_actions_rejected():
    with pytest.raises(recipes.RecipeRejected):
        recipes.sanitize_recipe(_recipe(cookie="abc=1"))
    with pytest.raises(recipes.RecipeRejected):
        recipes.sanitize_recipe({**_recipe(), "actions": [{"click": "x"}]})


def test_recipe_apply_behavior_rejected():
    with pytest.raises(recipes.RecipeRejected):
        recipes.sanitize_recipe(_recipe(navigation_strategy="auto-apply then submit"))


def test_recipe_validated_reuse_and_drift(tmp_path):
    store = recipes.RecipeStore(tmp_path / "r.jsonl")
    store.add_candidate(_recipe())
    promoted = store.promote("Example", "example.com", 1, run_id="run1")
    assert promoted.status == recipes.STATUS_VALIDATED
    latest = store.latest_validated("Example", "example.com")
    assert latest is not None and latest.version == 1
    drift = store.add_drift_version(_recipe(query_param="kw"))
    assert drift.version == 2  # drift => new version, never in-place edit


# --------------------------------------------------------------------------- #
# GOVERNOR
# --------------------------------------------------------------------------- #
class _FakeBackend:
    def __init__(self, name, status, valid=False):
        self.name = name
        self._status = status
        self.valid = valid
        self.calls = 0

    def search_company(self, task):
        self.calls += 1
        return BackendResult(task=task, route=self.name, status=self._status, valid=self.valid)


def test_structured_backend_bypasses_copilot():
    cli = _FakeBackend("cli", "SEARCHED_COMPLETE_NO_MATCHES")
    structured = _FakeBackend("structured", "SEARCHED_COMPLETE_WITH_MATCHES", valid=True)
    router = HybridRouter(cli, structured_backend=structured, structured_resolver=lambda t: True)
    task = CompanyTask(company="Acme")
    assert router.resolve_route(task) == ROUTE_STRUCTURED
    res = router.search_company(task)
    assert res.route == ROUTE_STRUCTURED and structured.calls == 1 and cli.calls == 0


def test_custom_site_uses_cli_backend():
    cli = _FakeBackend("cli", "SEARCHED_COMPLETE_NO_MATCHES")
    structured = _FakeBackend("structured", "SEARCHED_COMPLETE_WITH_MATCHES")
    router = HybridRouter(cli, structured_backend=structured, structured_resolver=lambda t: False)
    task = CompanyTask(company="Acme")
    assert router.resolve_route(task) == ROUTE_CLI_PLAYWRIGHT
    router.search_company(task)
    assert cli.calls == 1 and structured.calls == 0


def test_max_concurrency_two():
    assert MAX_COMPANY_CONCURRENCY == 2


def test_one_internal_retry_then_review_queue(tmp_path):
    cli = _FakeBackend("cli", CompanySearchStatus.BROWSER_TOOL_ERROR.value)
    router = HybridRouter(cli)
    task = CompanyTask(company="Acme", run_id="r")
    q = tmp_path / "review.jsonl"
    res = run_company_with_policy(router, task, review_queue_path=q, max_internal_retries=1)
    assert cli.calls == 2  # initial + exactly one retry
    assert q.exists() and len(q.read_text(encoding="utf-8").strip().splitlines()) == 1
    assert res.status == CompanySearchStatus.BROWSER_TOOL_ERROR.value


def test_external_block_is_terminal_no_retry(tmp_path):
    cli = _FakeBackend("cli", CompanySearchStatus.ACCESS_LIMITED_EXTERNAL.value)
    router = HybridRouter(cli)
    ledger = CompletionLedger(tmp_path / "led.jsonl")
    task = CompanyTask(company="Acme", run_id="r")
    q = tmp_path / "review.jsonl"
    res = run_company_with_policy(router, task, ledger=ledger, review_queue_path=q, max_internal_retries=1)
    assert cli.calls == 1  # external block never retried
    assert not q.exists()  # external blocks do not enter the internal repair queue
    assert ledger.is_completed("r", "Acme")


def test_resume_skips_completed_company(tmp_path):
    ledger = CompletionLedger(tmp_path / "led.jsonl")
    ledger.mark("r", "Acme", CompanySearchStatus.SEARCHED_COMPLETE_NO_MATCHES.value, valid=True)
    cli = _FakeBackend("cli", "SEARCHED_COMPLETE_WITH_MATCHES")
    router = HybridRouter(cli)
    res = run_company_with_policy(router, CompanyTask(company="Acme", run_id="r"), ledger=ledger)
    assert cli.calls == 0 and res.route == "skipped"


def test_one_failure_does_not_rerun_completed(tmp_path):
    ledger = CompletionLedger(tmp_path / "led.jsonl")
    ledger.mark("r", "Done", CompanySearchStatus.SEARCHED_COMPLETE_NO_MATCHES.value)
    completed = ledger.completed("r")
    assert "Done" in completed and len(completed) == 1
