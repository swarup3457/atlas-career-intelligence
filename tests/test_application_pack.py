"""Phase 1E/F Stage 4 — fact-grounded, local-only application packages."""

from __future__ import annotations

import datetime
import json

import pytest

from atlas.candidate.application_pack import (
    ApplicationDrafter,
    ApplicationPackBuilder,
    DraftedPackage,
    FactualGroundingReviewer,
    GroundingRejected,
    REQUIRED_MD_FILES,
    validate_package,
)
from atlas.candidate.eligibility import CandidateProfile, RankableJob
from atlas.candidate.importer import build_synthetic_ledger
from atlas.candidate.ranking import DeepEvaluator
from atlas.policy import load_policy

TODAY = datetime.date(2026, 9, 8)


@pytest.fixture(scope="module")
def policy():
    return load_policy()


@pytest.fixture(scope="module")
def candidate():
    return CandidateProfile.from_ledger(
        build_synthetic_ledger(), total_experience_years=4.5,
        target_lanes=("JAVA_BACKEND", "GENERAL_SOFTWARE"),
    )


def _job(**kw):
    base = dict(
        job_key="job_pack_test", company="Acme", title="Java Backend Engineer",
        location="Bengaluru, India", lane="JAVA_BACKEND",
        mandatory_requirements=("Java", "Kubernetes"), preferred_requirements=("Spring Boot",),
        experience_text="2+ years", eligibility_text="Bengaluru, India", posted_date=TODAY,
        verification_state="VERIFIED_OFFICIAL", has_live_official_page=True, url="https://acme.example/jobs/1",
    )
    base.update(kw)
    return RankableJob(**base)


def _evaluation(policy, candidate, job):
    return DeepEvaluator(policy, candidate).evaluate(job, today=TODAY)


def test_package_has_required_files_and_facts_audit(policy, candidate, tmp_path):
    job = _job()
    ev = _evaluation(policy, candidate, job)
    builder = ApplicationPackBuilder(candidate)
    res = builder.build(job, ev, packs_root=tmp_path)
    from pathlib import Path

    vdir = Path(res.pack_dir)
    for name in REQUIRED_MD_FILES:
        assert (vdir / name).is_file()
    assert (vdir / "facts_audit.json").is_file()
    audit = json.loads((vdir / "facts_audit.json").read_text())
    # Java supported, Kubernetes not -> marked do_not_claim
    claims = {f["claim"]: f for f in audit["facts"]}
    assert claims["Java"]["supported"] is True and claims["Java"]["evidence_id"]
    assert claims["Kubernetes"]["supported"] is False
    assert claims["Kubernetes"]["action"] == "do_not_claim"


def test_brief_never_asserts_a_gap(policy, candidate, tmp_path):
    job = _job()
    ev = _evaluation(policy, candidate, job)
    drafted = ApplicationDrafter(candidate).draft(job, ev)
    brief = drafted.md_files["application_brief.md"].lower()
    assert "kubernetes" not in brief  # the genuine gap is never claimed


def test_grounding_rejects_unsupported_fact(candidate):
    job = _job()
    # a poisoned brief that asserts the genuine gap
    poisoned = DraftedPackage(
        job_key=job.job_key,
        md_files={
            "job_snapshot.md": "# snap",
            "match_report.md": "# match",
            "tailoring_plan.md": "# plan",
            "application_brief.md": "# brief\n\nExpert in Kubernetes production ownership.",
        },
        facts_audit={"facts": [{"claim": "Kubernetes", "supported": False, "action": "do_not_claim"}]},
        supported=(), missing=("Kubernetes",), evidence_ids=(),
    )
    result = FactualGroundingReviewer(candidate).review(poisoned, job)
    assert result.ok is False
    assert any("Kubernetes" in v for v in result.violations)


def test_grounding_rejects_unverified_year_and_metric(candidate):
    job = _job()
    poisoned = DraftedPackage(
        job_key=job.job_key,
        md_files={
            "job_snapshot.md": "# snap", "match_report.md": "# match", "tailoring_plan.md": "# plan",
            "application_brief.md": "# brief\n\nDelivered 90% latency cut since 1998.",
        },
        facts_audit={"facts": []}, supported=(), missing=(), evidence_ids=(),
    )
    result = FactualGroundingReviewer(candidate).review(poisoned, job)
    assert result.ok is False
    assert any("year" in v for v in result.violations)
    assert any("metric" in v for v in result.violations)


def test_build_raises_on_grounding_violation(policy, candidate, tmp_path, monkeypatch):
    job = _job()
    ev = _evaluation(policy, candidate, job)
    builder = ApplicationPackBuilder(candidate)

    def poisoned_draft(job, evaluation):
        return DraftedPackage(
            job_key=job.job_key,
            md_files={n: "# x" for n in REQUIRED_MD_FILES}
            | {"application_brief.md": "# brief\n\nDeep Kubernetes ownership."},
            facts_audit={"facts": []}, supported=(), missing=("Kubernetes",), evidence_ids=(),
        )

    monkeypatch.setattr(builder.drafter, "draft", poisoned_draft)
    with pytest.raises(GroundingRejected):
        builder.build(job, ev, packs_root=tmp_path)


def test_idempotent_versioning(policy, candidate, tmp_path):
    job = _job()
    ev = _evaluation(policy, candidate, job)
    builder = ApplicationPackBuilder(candidate)
    r1 = builder.build(job, ev, packs_root=tmp_path)
    r2 = builder.build(job, ev, packs_root=tmp_path)
    assert r1.version == 1
    assert r2.version == 1 and r2.idempotent_reuse is True  # identical content reused, not overwritten

    # changed content -> new version, previous version preserved
    job2 = _job(preferred_requirements=("Spring Boot", "MySQL"))
    ev2 = _evaluation(policy, candidate, job2)
    r3 = builder.build(job2, ev2, packs_root=tmp_path)
    assert r3.version == 2
    from pathlib import Path

    assert (Path(tmp_path) / job.job_key / "v1").is_dir()
    assert (Path(tmp_path) / job.job_key / "v2").is_dir()


def test_docx_written_and_reopens(policy, candidate, tmp_path):
    pytest.importorskip("docx")
    from docx import Document
    from pathlib import Path

    job = _job()
    ev = _evaluation(policy, candidate, job)
    res = ApplicationPackBuilder(candidate, write_docx=True).build(job, ev, packs_root=tmp_path)
    assert "resume_tailored.docx" in res.docx_written
    assert "cover_letter.docx" in res.docx_written
    # genuine reopen validation
    doc = Document(str(Path(res.pack_dir) / "resume_tailored.docx"))
    assert len(doc.paragraphs) >= 1
    # PDF honestly reported as unavailable
    assert any("PDF" in lim for lim in res.limitations)


def test_no_docx_degrades_gracefully(policy, candidate, tmp_path):
    job = _job()
    ev = _evaluation(policy, candidate, job)
    res = ApplicationPackBuilder(candidate, write_docx=False).build(job, ev, packs_root=tmp_path)
    assert res.docx_written == ()
    assert any("Markdown + JSON" in lim for lim in res.limitations)
    # markdown package still complete
    from pathlib import Path

    for name in REQUIRED_MD_FILES:
        assert (Path(res.pack_dir) / name).is_file()


def test_no_submission_code_path():
    import atlas.candidate.application_pack as mod

    src = open(mod.__file__, encoding="utf-8").read().lower()
    for forbidden in ("submit(", "apply(", ".click(", "requests.post", "sendkeys", "fill("):
        assert forbidden not in src


def test_validator_flags_missing_sections():
    problems = validate_package({"job_snapshot.md": ""}, {"facts": []})
    assert any("job_snapshot.md" in p for p in problems)
    assert any("application_brief.md" in p for p in problems)
