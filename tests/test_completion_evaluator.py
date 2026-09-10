from atlas.vscode_hunt.completion_evaluator import evaluate_completion

LANES = ("JAVA_BACKEND", "JAVA_FULLSTACK", "REACT_FRONTEND", "DOTNET", "ENTERPRISE_HR_PAYROLL_INTEGRATION")


def result():
    return {"lanes_attempted": list(LANES), "result_states": {lane: {"state": "no_results"} for lane in LANES}, "jobs": [], "detail_urls": [], "evidence_quotes": [], "browser_errors": [], "source_health": {"status": "healthy"}, "completion_claim": True}


def test_all_lane_states_complete():
    assert evaluate_completion(result(), LANES).action == "COMPLETE"


def test_missing_lane_requires_follow_up():
    value = result(); value["lanes_attempted"] = list(LANES[:-1]); value["result_states"].pop(LANES[-1])
    decision = evaluate_completion(value, LANES)
    assert decision.action == "FOLLOW_UP_REQUIRED"
    assert "lane:ENTERPRISE_HR_PAYROLL_INTEGRATION" in decision.missing_obligations


def test_job_without_detail_requires_follow_up():
    value = result(); value["jobs"] = [{"title": "Java Backend Engineer"}]
    assert evaluate_completion(value, LANES).action == "FOLLOW_UP_REQUIRED"
