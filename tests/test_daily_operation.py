"""Phase 1E/F Stage 6 — root daily LangGraph graph, crash/resume, scheduler."""

from __future__ import annotations

import datetime

import pytest

from atlas.candidate.eligibility import CandidateProfile, RankableJob
from atlas.candidate.importer import build_synthetic_ledger
from atlas.config import load_settings
from atlas.orchestration.daily_state import DailyPhase, DAILY_PHASE_ORDER
from atlas.runtime.daily import AuthExpired, DailyRunner
from atlas.runtime.scheduler_install import WindowsDailyScheduler, display_command

TODAY = datetime.date(2026, 9, 8)


@pytest.fixture
def settings(tmp_path):
    return load_settings(
        production_output_root=str(tmp_path / "prod"),
        checkpoint_db=str(tmp_path / "cp.sqlite"),
        logs_dir=str(tmp_path / "logs"),
    )


@pytest.fixture
def candidate():
    return CandidateProfile.from_ledger(
        build_synthetic_ledger(), total_experience_years=4.5,
        target_lanes=("JAVA_BACKEND", "GENERAL_SOFTWARE"),
    )


def _jobs(n=4):
    out = []
    for i in range(n):
        out.append(RankableJob(
            job_key=f"job_daily_{i}", company=f"Co{i}", title="Java Backend Engineer",
            location="Bengaluru, India", lane="JAVA_BACKEND",
            mandatory_requirements=("Java", "Spring Boot"), preferred_requirements=("MySQL",),
            experience_text="2+ years", eligibility_text="Bengaluru, India", posted_date=TODAY,
            verification_state="VERIFIED_OFFICIAL", has_live_official_page=True,
            url=f"https://co{i}.example/jobs/1",
        ))
    return out


# --------------------------------------------------------------------------- #
# End-to-end daily graph
# --------------------------------------------------------------------------- #
def test_daily_run_end_to_end(settings, candidate):
    runner = DailyRunner(settings, "DAILY_E2E", jobs=_jobs(), candidate=candidate, build_docx=False)
    res = runner.run()
    assert res.terminal_state == "COMPLETE"
    assert res.report_valid is True
    assert res.latest_updated is True
    assert res.selected >= 1
    from pathlib import Path
    assert Path(res.workbook_path).is_file()
    # every non-terminal phase ran exactly once
    counts = runner.phase_run_counts()
    for phase in DAILY_PHASE_ORDER:
        if phase == DailyPhase.COMPLETE:
            continue
        assert counts.get(phase.value) == 1, f"{phase.value} ran {counts.get(phase.value)} times"


def test_crash_and_fresh_runner_resume_no_rerun(settings, candidate):
    jobs = _jobs()
    # first process: stop after DEEP_EVALUATE (simulated crash before publish)
    r1 = DailyRunner(settings, "DAILY_RESUME", jobs=jobs, candidate=candidate,
                     build_docx=False, stop_after_phase=DailyPhase.DEEP_EVALUATE)
    res1 = r1.run()
    assert res1.terminal_state is None or res1.terminal_state != "COMPLETE"
    counts_after_crash = r1.phase_run_counts()
    assert counts_after_crash.get("DEEP_EVALUATE") == 1
    assert "PUBLISH_LATEST" not in counts_after_crash  # never reached before crash

    # second (fresh) process: same run_id + checkpoint db -> resumes exact remaining work
    r2 = DailyRunner(settings, "DAILY_RESUME", jobs=_jobs(), candidate=candidate, build_docx=False)
    res2 = r2.resume()
    assert res2.terminal_state == "COMPLETE"
    assert res2.latest_updated is True

    # completed phases were NOT rerun across the crash boundary (each == 1)
    counts = r2.phase_run_counts()
    for phase in DAILY_PHASE_ORDER:
        if phase == DailyPhase.COMPLETE:
            continue
        assert counts.get(phase.value) == 1, f"{phase.value} ran {counts.get(phase.value)} times"


def test_daily_status_reports_terminal(settings, candidate):
    runner = DailyRunner(settings, "DAILY_STATUS", jobs=_jobs(), candidate=candidate, build_docx=False)
    runner.run()
    status = runner.status()
    assert status["terminal_state"] == "COMPLETE"
    assert status["manifest_status"] == "COMPLETE"
    assert set(status["phases_completed"]) >= {"TRIAGE", "BUILD_REPORT", "PUBLISH_LATEST"}


def test_application_packs_built_under_run_dir(settings, candidate):
    runner = DailyRunner(settings, "DAILY_PACKS", jobs=_jobs(), candidate=candidate, build_docx=False)
    runner.run()
    from pathlib import Path
    packs_dir = Path(runner.paths.application_packs_dir)
    built = list(packs_dir.glob("*/v1/application_brief.md"))
    assert built, "expected at least one application pack under the run dir"


def test_auth_expired_yields_waiting_for_human(settings, candidate):
    def official():
        raise AuthExpired("linkedin", "atlas portals auth --family linkedin")

    runner = DailyRunner(settings, "DAILY_WAIT", jobs=_jobs(), candidate=candidate,
                         official_exec=official, build_docx=False)
    res = runner.run()
    assert res.terminal_state == "WAITING_FOR_HUMAN"
    # publish never happened
    assert res.latest_updated in (None, False)


def test_live_market_exec_crash_resume_reloads_persisted_leads(settings, candidate):
    """A live-style run seeds leads via market_exec (non-deterministic in
    reality). After a crash before publish, a FRESH runner with NO seed jobs and
    NO market_exec must resume EXACTLY from the durably persisted lead set."""
    from atlas.runtime.live_sources import LivePortalDiscovery
    from tests.test_live_sources import FakeAdapter, _result

    def _market_exec():
        disco = LivePortalDiscovery(lane="JAVA_BACKEND", location="India", max_pages=1,
                                    adapter_factory=lambda fam: FakeAdapter([_result(i) for i in range(4)]))
        return disco.discover(("linkedin",)).jobs

    # process 1: seed via market_exec, stop after DEEP_EVALUATE (crash before publish)
    r1 = DailyRunner(settings, "DAILY_LIVE_RESUME", jobs=[], candidate=candidate,
                     market_exec=_market_exec, build_docx=False,
                     stop_after_phase=DailyPhase.DEEP_EVALUATE, live=True)
    r1.run()
    from pathlib import Path
    leads_file = Path(r1.paths.run_dir) / "discovered_leads.json"
    assert leads_file.is_file()  # leads durably persisted at CANONICALIZE

    # process 2: fresh runner, NO seed jobs, NO market_exec -> reloads persisted leads
    r2 = DailyRunner(settings, "DAILY_LIVE_RESUME", jobs=[], candidate=candidate,
                     market_exec=None, build_docx=False, live=True)
    res2 = r2.resume()
    assert res2.terminal_state == "COMPLETE"
    assert res2.jobs_discovered == 4  # exact same 4 leads, not re-discovered
    assert res2.latest_updated is True
    # EXECUTE_MARKET ran exactly once (only in process 1); never rerun on resume
    assert r2.phase_run_counts().get("EXECUTE_MARKET") == 1


# --------------------------------------------------------------------------- #
# Scheduler installer (dry-run default, explicit enable, quoted paths)
# --------------------------------------------------------------------------- #
def test_scheduler_dry_run_default_does_not_execute():
    calls = []
    sched = WindowsDailyScheduler(python_exe=r"C:\Program Files\Atlas\python.exe",
                                  runner=lambda argv: calls.append(argv))
    action = sched.install("07:30")
    assert action.dry_run is True and action.enabled is False
    assert calls == []  # nothing executed in dry-run
    # the schtasks command is generated with quoted paths
    assert "/Create" in action.argv and "07:30" in action.argv
    assert '"C:\\Program Files\\Atlas\\python.exe"' in action.command


def test_scheduler_enable_executes_with_quoted_paths():
    captured = {}

    class FakeResult:
        returncode = 0
        stdout = "SUCCESS"
        stderr = ""

    def fake_runner(argv):
        captured["argv"] = list(argv)
        return FakeResult()

    sched = WindowsDailyScheduler(python_exe=r"C:\Program Files\Atlas\python.exe", runner=fake_runner)
    action = sched.install("23:15", enable=True)
    assert action.dry_run is False and action.enabled is True
    assert captured["argv"][0] == "schtasks"
    # the /TR command embeds the quoted python path and the daily run invocation
    tr_index = captured["argv"].index("/TR") + 1
    assert captured["argv"][tr_index].startswith('"C:\\Program Files\\Atlas\\python.exe"')
    assert "daily run --live --background" in captured["argv"][tr_index]


def test_scheduler_rejects_bad_time():
    sched = WindowsDailyScheduler()
    with pytest.raises(ValueError):
        sched.install("25:00")
    with pytest.raises(ValueError):
        sched.install("7am")


def test_scheduler_disable_and_remove():
    seen = []
    sched = WindowsDailyScheduler(runner=lambda argv: seen.append(list(argv)) or type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})())
    d = sched.disable()
    r = sched.remove()
    assert d.argv[:2] == ("schtasks", "/Change") and "/DISABLE" in d.argv
    assert r.argv[:2] == ("schtasks", "/Delete") and "/F" in r.argv


def test_scheduled_command_never_opens_auth_window():
    sched = WindowsDailyScheduler()
    tr = sched.task_run_command()
    # background/headless daily run; no portals auth (which opens a visible window)
    assert "--background" in tr
    assert "portals auth" not in tr
