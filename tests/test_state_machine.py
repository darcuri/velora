"""Tests for the state machine wiring of request_review and dismiss_finding decisions."""

from __future__ import annotations

import unittest
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

from velora.protocol import (
    CoordinatorResponse,
    FindingDismissal,
    ProtocolError,
    ReviewBrief,
    ReviewFinding,
    ReviewScope,
    SelectedSpecialist,
)
from velora.protocol import ReviewResult as ProtocolReviewResult
from velora.run import (
    OrchestratorState,
    RunContext,
    _state_dispatching_review,
    _state_processing_dismissal,
    _state_terminal,
)


def _minimal_review_brief(*, brief_id: str = "rb-1", reviewer: str = "gemini") -> ReviewBrief:
    return ReviewBrief(
        id=brief_id,
        reviewer=reviewer,
        model=None,
        objective="Review the diff for correctness",
        acceptance_criteria=["Tests pass"],
        rejection_criteria=["Security issues"],
        areas_of_concern=["edge cases"],
        scope=ReviewScope(
            kind="full_diff",
            base_ref="main",
            head_sha="abc123",
            files=[],
        ),
    )


def _minimal_specialist() -> SelectedSpecialist:
    return SelectedSpecialist(role="reviewer", runner="gemini")


def _minimal_coord_resp(
    *,
    decision: str = "request_review",
    review_brief: ReviewBrief | None = None,
    finding_dismissal: FindingDismissal | None = None,
) -> CoordinatorResponse:
    return CoordinatorResponse(
        protocol_version=1,
        decision=decision,
        reason="test reason",
        selected_specialist=_minimal_specialist(),
        review_brief=review_brief,
        finding_dismissal=finding_dismissal,
    )


class _FakeGH:
    """Stub GitHub client that records calls."""

    def __init__(self) -> None:
        self.comments: list[tuple[str, str, int, str]] = []

    def post_issue_comment(self, owner: str, repo: str, pr_number: int, body: str) -> None:
        self.comments.append((owner, repo, pr_number, body))


def _minimal_ctx(
    *,
    review_enabled: bool = False,
    head_sha: str = "abc123",
    pr_number: int | None = 42,
    coord_resp: CoordinatorResponse | None = None,
    active_review_result: ProtocolReviewResult | None = None,
) -> RunContext:
    """Build a RunContext with just enough state for review/dismissal tests."""
    gh = _FakeGH()
    ctx = RunContext(
        task_id="test-task-1",
        run_id="test-task-1",
        repo_ref="testowner/testrepo",
        verb="fix",
        owner="testowner",
        repo="testrepo",
        base_branch="main",
        work_branch="velora/test-task-1",
        repo_path=Path("/tmp/fake-repo"),
        config=SimpleNamespace(
            mode_a_max_tokens=0,
            mode_a_max_cost_usd=0.0,
            mode_a_no_progress_max=3,
            mode_a_max_wall_seconds=0,
            mode_a_review_enabled=review_enabled,
            specialist_matrix=None,
            max_attempts=5,
        ),
        max_attempts=5,
        max_tokens=0,
        max_wall_seconds=0,
        no_progress_max=3,
        review_enabled=review_enabled,
        iteration=1,
        record={
            "task_id": "test-task-1",
            "head_sha": head_sha,
            "pr_number": pr_number,
            "pr_url": "https://github.com/testowner/testrepo/pull/42",
            "status": "running",
        },
        request={
            "state": {},
            "evaluation": {},
            "history": {},
        },
        active_review_result=active_review_result,
        gh=gh,
        home=Path("/tmp/fake-home"),
        task_dir=Path("/tmp/fake-task-dir"),
        debug=False,
        loop_start=0.0,
    )
    ctx.coord_resp = coord_resp
    ctx.dbg_dir = None
    return ctx


class TestStateDispatchingReview(unittest.TestCase):
    """Tests for _state_dispatching_review."""

    @patch("velora.run._ctx_audit")
    @patch("velora.run._run_review_with_retry", return_value=("approved", "ok. No issues found."))
    @patch("velora.run._read_diff_for_review", return_value="diff --git a/foo.py b/foo.py\n+pass\n")
    def test_request_review_dispatches_and_returns_to_awaiting_decision(
        self,
        mock_read_diff: MagicMock,
        mock_review: MagicMock,
        mock_audit: MagicMock,
    ) -> None:
        brief = _minimal_review_brief()
        resp = _minimal_coord_resp(review_brief=brief)
        ctx = _minimal_ctx(coord_resp=resp)

        next_state = _state_dispatching_review(ctx)

        self.assertEqual(next_state, OrchestratorState.AWAITING_DECISION)
        # ReviewResult should be stored on ctx.
        self.assertIsNotNone(ctx.active_review_result)
        self.assertEqual(ctx.active_review_result.review_brief_id, brief.id)
        self.assertEqual(ctx.active_review_result.verdict, "approve")
        self.assertTrue(ctx.review_has_occurred)
        # Audit event should have been emitted.
        mock_audit.assert_called()
        # State should have latest_review_result.
        self.assertIn("latest_review_result", ctx.request["state"])
        self.assertEqual(ctx.request["state"]["latest_review_result"]["verdict"], "approve")

    @patch("velora.run._ctx_audit")
    @patch("velora.run._run_review_with_retry", return_value=("blocker", "**BLOCKER:** Security issue found"))
    @patch("velora.run._read_diff_for_review", return_value="diff content")
    def test_request_review_blocker_creates_reject_verdict(
        self,
        mock_read_diff: MagicMock,
        mock_review: MagicMock,
        mock_audit: MagicMock,
    ) -> None:
        brief = _minimal_review_brief()
        resp = _minimal_coord_resp(review_brief=brief)
        ctx = _minimal_ctx(coord_resp=resp)

        next_state = _state_dispatching_review(ctx)

        self.assertEqual(next_state, OrchestratorState.AWAITING_DECISION)
        self.assertIsNotNone(ctx.active_review_result)
        self.assertEqual(ctx.active_review_result.verdict, "reject")
        self.assertTrue(len(ctx.active_review_result.findings) > 0)
        self.assertTrue(any(f.severity == "blocker" for f in ctx.active_review_result.findings))

    @patch("velora.run._ctx_audit")
    @patch("velora.run._run_review_with_retry", return_value=("nits", "NIT: style issue\nNIT: naming"))
    @patch("velora.run._read_diff_for_review", return_value="diff content")
    def test_request_review_nits_creates_approve_verdict_with_findings(
        self,
        mock_read_diff: MagicMock,
        mock_review: MagicMock,
        mock_audit: MagicMock,
    ) -> None:
        brief = _minimal_review_brief()
        resp = _minimal_coord_resp(review_brief=brief)
        ctx = _minimal_ctx(coord_resp=resp)

        next_state = _state_dispatching_review(ctx)

        self.assertEqual(next_state, OrchestratorState.AWAITING_DECISION)
        self.assertIsNotNone(ctx.active_review_result)
        self.assertEqual(ctx.active_review_result.verdict, "approve")

    def test_request_review_requires_head_sha(self) -> None:
        brief = _minimal_review_brief()
        resp = _minimal_coord_resp(review_brief=brief)
        ctx = _minimal_ctx(coord_resp=resp, head_sha="")

        with self.assertRaises(ProtocolError) as cm:
            _state_dispatching_review(ctx)
        self.assertIn("head_sha", str(cm.exception))

    @patch("velora.run._ctx_audit")
    @patch("velora.run._run_review_with_retry", return_value=("approved", "ok."))
    @patch("velora.run._read_diff_for_review", return_value="diff")
    def test_request_review_posts_pr_comment(
        self,
        mock_read_diff: MagicMock,
        mock_review: MagicMock,
        mock_audit: MagicMock,
    ) -> None:
        brief = _minimal_review_brief()
        resp = _minimal_coord_resp(review_brief=brief)
        ctx = _minimal_ctx(coord_resp=resp, pr_number=99)

        _state_dispatching_review(ctx)

        gh = ctx.gh
        self.assertEqual(len(gh.comments), 1)
        self.assertEqual(gh.comments[0][2], 99)  # pr_number


class TestStateProcessingDismissal(unittest.TestCase):
    """Tests for _state_processing_dismissal."""

    @patch("velora.run._ctx_audit")
    def test_dismiss_finding_validates_against_active_review(
        self,
        mock_audit: MagicMock,
    ) -> None:
        """Dismissal with no prior review should raise ProtocolError."""
        dismissal = FindingDismissal(finding_ids=["f1"], justification="not relevant")
        resp = _minimal_coord_resp(
            decision="dismiss_finding",
            finding_dismissal=dismissal,
        )
        ctx = _minimal_ctx(coord_resp=resp, active_review_result=None)

        with self.assertRaises(ProtocolError) as cm:
            _state_processing_dismissal(ctx)
        self.assertIn("prior review result", str(cm.exception))

    @patch("velora.run._ctx_audit")
    def test_dismiss_finding_validates_finding_ids(
        self,
        mock_audit: MagicMock,
    ) -> None:
        """Dismissal with invalid finding ID should raise ProtocolError."""
        active_review = ProtocolReviewResult(
            review_brief_id="rb-1",
            verdict="reject",
            findings=[
                ReviewFinding(
                    id="rb-1-f0",
                    severity="blocker",
                    category="correctness",
                    location="foo.py",
                    description="bug found",
                    criterion_id=None,
                ),
            ],
            summary="Found issues",
        )
        dismissal = FindingDismissal(finding_ids=["nonexistent-id"], justification="not relevant")
        resp = _minimal_coord_resp(
            decision="dismiss_finding",
            finding_dismissal=dismissal,
        )
        ctx = _minimal_ctx(coord_resp=resp, active_review_result=active_review)

        with self.assertRaises(ProtocolError) as cm:
            _state_processing_dismissal(ctx)
        self.assertIn("nonexistent-id", str(cm.exception))

    @patch("velora.run._ctx_audit")
    def test_dismiss_finding_succeeds_with_valid_ids(
        self,
        mock_audit: MagicMock,
    ) -> None:
        """Valid dismissal should return AWAITING_DECISION."""
        active_review = ProtocolReviewResult(
            review_brief_id="rb-1",
            verdict="reject",
            findings=[
                ReviewFinding(
                    id="rb-1-f0",
                    severity="blocker",
                    category="correctness",
                    location="foo.py",
                    description="bug found",
                    criterion_id=None,
                ),
                ReviewFinding(
                    id="rb-1-f1",
                    severity="nit",
                    category="style",
                    location="bar.py",
                    description="naming",
                    criterion_id=None,
                ),
            ],
            summary="Found issues",
        )
        dismissal = FindingDismissal(finding_ids=["rb-1-f0"], justification="false positive")
        resp = _minimal_coord_resp(
            decision="dismiss_finding",
            finding_dismissal=dismissal,
        )
        ctx = _minimal_ctx(coord_resp=resp, active_review_result=active_review)

        next_state = _state_processing_dismissal(ctx)

        self.assertEqual(next_state, OrchestratorState.AWAITING_DECISION)
        mock_audit.assert_called()
        # State should have dismissal info.
        self.assertIn("latest_dismissal", ctx.request["state"])
        self.assertEqual(ctx.request["state"]["latest_dismissal"]["remaining_blocker_count"], 0)
        self.assertEqual(ctx.request["state"]["latest_dismissal"]["remaining_finding_count"], 1)


class TestReviewEnabledGate(unittest.TestCase):
    """Tests for the review_enabled policy gate in _state_terminal."""

    def test_review_enabled_blocks_finalize_without_review(self) -> None:
        """finalize_success with review_enabled=True and no prior review should raise ProtocolError."""
        resp = _minimal_coord_resp(decision="finalize_success")
        # Construct a CoordinatorResponse that matches finalize_success -- no payload fields needed.
        resp = CoordinatorResponse(
            protocol_version=1,
            decision="finalize_success",
            reason="All done",
            selected_specialist=_minimal_specialist(),
        )
        ctx = _minimal_ctx(coord_resp=resp, review_enabled=True)
        ctx.review_has_occurred = False
        ctx.iter_start = 1.0

        with self.assertRaises(ProtocolError) as cm:
            _state_terminal(ctx)
        self.assertIn("review_enabled", str(cm.exception))
        self.assertIn("request_review", str(cm.exception))

    @patch("velora.run._ctx_audit")
    @patch("velora.run._ctx_replay_event")
    @patch("velora.run._ctx_sync_replay")
    @patch("velora.run.upsert_task")
    def test_review_enabled_allows_finalize_after_review(
        self,
        mock_upsert: MagicMock,
        mock_sync: MagicMock,
        mock_replay: MagicMock,
        mock_audit: MagicMock,
    ) -> None:
        """finalize_success with review_enabled=True after a review has occurred should succeed."""
        resp = CoordinatorResponse(
            protocol_version=1,
            decision="finalize_success",
            reason="All done",
            selected_specialist=_minimal_specialist(),
        )
        ctx = _minimal_ctx(coord_resp=resp, review_enabled=True)
        ctx.review_has_occurred = True
        ctx.iter_start = 1.0

        next_state = _state_terminal(ctx)

        self.assertEqual(next_state, OrchestratorState.DONE)
        self.assertIsNotNone(ctx.result)
        self.assertEqual(ctx.result["status"], "ready")

    @patch("velora.run._ctx_audit")
    @patch("velora.run._ctx_replay_event")
    @patch("velora.run._ctx_sync_replay")
    @patch("velora.run.upsert_task")
    def test_stop_failure_allowed_without_review(
        self,
        mock_upsert: MagicMock,
        mock_sync: MagicMock,
        mock_replay: MagicMock,
        mock_audit: MagicMock,
    ) -> None:
        """stop_failure should be allowed even without a prior review, regardless of review_enabled."""
        resp = CoordinatorResponse(
            protocol_version=1,
            decision="stop_failure",
            reason="Cannot proceed",
            selected_specialist=_minimal_specialist(),
        )
        ctx = _minimal_ctx(coord_resp=resp, review_enabled=True)
        ctx.review_has_occurred = False
        ctx.iter_start = 1.0

        next_state = _state_terminal(ctx)

        self.assertEqual(next_state, OrchestratorState.DONE)
        self.assertIsNotNone(ctx.result)
        self.assertEqual(ctx.result["status"], "failed")


if __name__ == "__main__":
    unittest.main()
