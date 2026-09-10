from __future__ import annotations

from atlas.vscode_hunt.validation import validate_result


def _envelope(job: dict | None = None) -> dict:
    return {
        "schema_version": 2,
        "run_id": "run",
        "task_id": "run::company",
        "attempt_id": "attempt",
        "company_id": "company",
        "worker_invocation_id": "worker",
        "official_domain": "example.com",
        "career_url": "https://careers.example.com/jobs",
        "queries": ["Java Backend India"],
        "lanes_attempted": ["JAVA_BACKEND"],
        "result_states": [{"lane": "JAVA_BACKEND", "state": "results_observed"}],
        "detail_urls": ["https://careers.example.com/jobs/1"],
        "jobs": [job] if job else [],
        "rejections": [],
        "foreign_leads": [],
        "browser_errors": [],
        "external_block_evidence": "",
        "source_health": {"state": "OK"},
        "evidence_quotes": ["Location: Bengaluru, India"],
        "completion_claim": True,
    }


def _job(**overrides: object) -> dict:
    job = {
        "title": "Java Backend Engineer",
        "company": "Company",
        "location": "Bengaluru, India",
        "official_url": "https://careers.example.com/jobs/1",
        "description": "Build backend services with Java and Spring Boot. Location: Bengaluru, India.",
        "mandatory_requirements": ["Java", "Spring Boot"],
        "preferred_requirements": [],
        "experience_text": "2+ years",
        "lane": "JAVA_BACKEND",
        "evidence_snippets": ["Location: Bengaluru, India"],
    }
    job.update(overrides)
    return job


def _task() -> dict:
    return {"run_id": "run", "task_id": "run::company", "company_name": "Company", "lanes_json": '["JAVA_BACKEND"]'}


def test_valid_india_job_passes() -> None:
    assert validate_result(_envelope(_job()), _task()) == []


def test_foreign_job_is_rejected() -> None:
    errors = validate_result(_envelope(_job(location="Toronto, Canada")), _task())
    assert any("NON_INDIA_LOCATION" in error for error in errors)


def test_card_only_job_is_rejected() -> None:
    errors = validate_result(_envelope(_job(description="", mandatory_requirements=[])), _task())
    assert any("CARD_ONLY" in error for error in errors)


def test_excessive_mandatory_experience_is_rejected() -> None:
    errors = validate_result(_envelope(_job(experience_text="8+ years")), _task())
    assert any("EXPERIENCE_TOO_HIGH" in error for error in errors)


def test_java_version_is_not_years_of_experience() -> None:
    assert validate_result(_envelope(_job(description="Build Java 8 and Spring Boot services. Location: Bengaluru, India.", experience_text="Java 8")), _task()) == []
