from velora.run import run_review_stage


def test_run_review_stage_returns_approve_when_no_issues() -> None:
    result = run_review_stage({"issues_found": []})
    assert result.outcome == "approve"
    assert result.issues_found == []
    assert "no follow-up issues" in result.summary


def test_run_review_stage_returns_repair_when_issues_found() -> None:
    result = run_review_stage({"issues_found": ["Missing edge-case test"]})
    assert result.outcome == "repair"
    assert result.issues_found == ["Missing edge-case test"]
    assert "found 1 follow-up issue" in result.summary
