"""End-to-end integration tests for the structured review protocol flow.

These tests exercise the full review protocol flow through the state machine,
covering request_review, dismiss_finding, and the review_enabled policy gate.
"""

import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any
import unittest
from unittest.mock import MagicMock, patch

from velora.acpx import CmdResult
from velora.config import get_config
from velora.protocol import ProtocolError, validate_coordinator_response
from velora.run import run_task_mode_a
from velora.spec import RunSpec


# ---------------------------------------------------------------------------
# Response helpers
# ---------------------------------------------------------------------------


def _execute_response(*, runner: str = "codex", work_item_id: str = "WI-0001"):
    payload = {
        "protocol_version": 1,
        "decision": "execute_work_item",
        "reason": "apply change",
        "selected_specialist": {"role": "implementer", "runner": runner},
        "work_item": {
            "id": work_item_id,
            "kind": "implement",
            "rationale": "make progress",
            "instructions": ["Do the change."],
            "scope_hints": {"likely_files": ["velora/run.py"], "search_terms": ["work_result"]},
            "acceptance": {"must": ["works"], "must_not": [], "gates": ["tests"]},
            "limits": {"max_diff_lines": 50, "max_commits": 1},
            "commit": {
                "message": "feat: phase 4",
                "footer": {
                    "VELORA_RUN_ID": "task123",
                    "VELORA_ITERATION": 1,
                    "WORK_ITEM_ID": work_item_id,
                },
            },
        },
    }
    return validate_coordinator_response(payload)


def _finalize_response(reason: str = "done") -> Any:
    payload = {
        "protocol_version": 1,
        "decision": "finalize_success",
        "reason": reason,
        "selected_specialist": {"role": "investigator", "runner": "codex"},
    }
    return validate_coordinator_response(payload)


def _request_review_response(*, brief_id: str = "RB-0001", head_sha: str = "abc123") -> Any:
    payload = {
        "protocol_version": 1,
        "decision": "request_review",
        "reason": "request structured review",
        "selected_specialist": {"role": "reviewer", "runner": "gemini"},
        "review_brief": {
            "id": brief_id,
            "reviewer": "gemini",
            "model": None,
            "objective": "Verify correctness of changes",
            "acceptance_criteria": ["Tests pass", "No regressions"],
            "rejection_criteria": [],
            "areas_of_concern": [],
            "scope": {
                "kind": "full_diff",
                "base_ref": "main",
                "head_sha": head_sha,
                "files": [],
            },
        },
    }
    return validate_coordinator_response(payload)


def _dismiss_finding_response(*, finding_ids: list[str], justification: str = "Acceptable risk") -> Any:
    payload = {
        "protocol_version": 1,
        "decision": "dismiss_finding",
        "reason": "dismissing findings",
        "selected_specialist": {"role": "reviewer", "runner": "claude"},
        "finding_dismissal": {
            "finding_ids": finding_ids,
            "justification": justification,
        },
    }
    return validate_coordinator_response(payload)


def _work_result_json(
    *,
    status: str = "completed",
    branch: str = "velora/task123",
    sha: str = "abc123",
    work_item_id: str = "WI-0001",
) -> str:
    if status == "completed":
        blockers: list[str] = []
    else:
        blockers = ["blocked on missing credentials"]
        branch = ""
        sha = ""
    return (
        "{"
        f'"protocol_version":1,"work_item_id":"{work_item_id}","status":"{status}","summary":"worker summary",'
        f'"branch":"{branch}","head_sha":"{sha}","files_touched":["velora/run.py"],'
        '"tests_run":[{"command":"pytest tests","status":"pass","details":"ok"}],'
        f'"blockers":{json.dumps(blockers)},"follow_up":["next"],"evidence":["proof"]'
        "}"
    )


def _run_codex_writing_result(
    payload: str,
    *,
    repo_path: str = "/tmp/repo",
    work_item_id: str = "WI-0001",
):
    result_path = Path(repo_path) / ".velora" / "exchange" / "runs" / "task123" / work_item_id / "result.json"

    def _runner(*args, **kwargs):
        result_path.parent.mkdir(parents=True, exist_ok=True)
        result_path.write_text(payload, encoding="utf-8")
        return CmdResult(0, "worker chatter", "")

    return _runner


def _coord_ns(resp: Any) -> SimpleNamespace:
    """Wrap a validated coordinator response in the SimpleNamespace expected by the loop."""
    return SimpleNamespace(response=resp, cmd=CmdResult(0, "", ""))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestReviewProtocolIntegration(unittest.TestCase):
    """Integration tests for the structured review protocol flow."""

    def setUp(self):
        get_config.cache_clear()
        self.publish_branch = patch("velora.run._publish_branch", return_value=None)
        self.mock_publish_branch = self.publish_branch.start()

    def tearDown(self):
        self.publish_branch.stop()
        get_config.cache_clear()

    # ------------------------------------------------------------------
    # 1. Full review cycle: request_review → approve → finalize_success
    # ------------------------------------------------------------------

    def test_full_review_cycle_request_approve_finalize(self):
        """execute_work_item → CI passes → request_review (approved) → finalize_success."""
        gh = MagicMock()
        gh.get_default_branch.return_value = "main"
        gh.create_pull_request.return_value = {"html_url": "https://example/pr/1", "number": 1}
        gh.post_issue_comment.return_value = {}

        coord_runs = [
            _coord_ns(_execute_response()),
            _coord_ns(_request_review_response()),
            _coord_ns(_finalize_response("all good")),
        ]

        with (
            patch.dict(
                os.environ,
                {"VELORA_ALLOWED_OWNERS": "octocat", "VELORA_MODE_A_REVIEW_ENABLED": "1"},
                clear=False,
            ),
            patch("velora.run.build_task_id", return_value="task123"),
            patch("velora.run.velora_home", return_value=Path("/tmp/velora-home")),
            patch("velora.run.ensure_dir", side_effect=lambda p: p),
            patch("velora.run.upsert_task", return_value={}),
            patch("velora.run.GitHubClient.from_env", return_value=gh),
            patch("velora.run.ensure_repo_checkout", return_value=Path("/tmp/repo")),
            patch("velora.run.run_coordinator", side_effect=coord_runs),
            patch("velora.run.run_worker", side_effect=_run_codex_writing_result(_work_result_json())),
            patch("velora.run._poll_ci", return_value=("success", "ok")),
            patch("velora.run._read_diff_for_review", return_value="fake diff"),
            patch("velora.run._run_review_with_retry", return_value=("approved", "OK looks good")),
            patch("velora.run._cleanup_repo_detritus", return_value=None),
            patch("velora.run._append_text", return_value=None),
            patch("velora.run._write_text", return_value=None),
            patch("velora.run._dbg", return_value=None),
        ):
            result = run_task_mode_a("octocat/velora", "feature", RunSpec(task="test", max_attempts=5))

        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["summary"], "all good")

    # ------------------------------------------------------------------
    # 2. Full review cycle: reject → fix → re-review → approve → finalize
    # ------------------------------------------------------------------

    def test_full_review_cycle_request_reject_fix_rereview_finalize(self):
        """execute_work_item → CI passes → request_review (blocker) → fix → CI passes → request_review (approved) → finalize."""
        gh = MagicMock()
        gh.get_default_branch.return_value = "main"
        gh.create_pull_request.return_value = {"html_url": "https://example/pr/1", "number": 1}
        gh.post_issue_comment.return_value = {}

        coord_runs = [
            # Iteration 1: execute work item
            _coord_ns(_execute_response()),
            # Iteration 2 (after CI): request review — reviewer rejects with blocker
            _coord_ns(_request_review_response(brief_id="RB-0001")),
            # Iteration 3: coordinator asks for a fix based on review findings
            _coord_ns(_execute_response(work_item_id="WI-0002")),
            # Iteration 4 (after CI): request re-review — reviewer approves
            _coord_ns(_request_review_response(brief_id="RB-0002")),
            # Iteration 5: finalize
            _coord_ns(_finalize_response("fixed and approved")),
        ]

        review_calls = [
            # First review: blocker (called from _state_polling_ci legacy path AND from _state_dispatching_review)
            ("approved", "OK"),           # legacy review in POLLING_CI for iteration 1
            ("blocker", "- **BLOCKER:** Missing null check in run.py"),  # structured review RB-0001
            ("approved", "OK"),           # legacy review in POLLING_CI for iteration 3
            ("approved", "OK all clear"), # structured review RB-0002
        ]

        worker_call_count = [0]

        def _worker_side_effect(*args, **kwargs):
            worker_call_count[0] += 1
            wi_id = "WI-0001" if worker_call_count[0] == 1 else "WI-0002"
            result_path = Path("/tmp/repo/.velora/exchange/runs/task123") / wi_id / "result.json"
            result_path.parent.mkdir(parents=True, exist_ok=True)
            result_path.write_text(_work_result_json(work_item_id=wi_id), encoding="utf-8")
            return CmdResult(0, "worker chatter", "")

        with (
            patch.dict(
                os.environ,
                {"VELORA_ALLOWED_OWNERS": "octocat", "VELORA_MODE_A_REVIEW_ENABLED": "1"},
                clear=False,
            ),
            patch("velora.run.build_task_id", return_value="task123"),
            patch("velora.run.velora_home", return_value=Path("/tmp/velora-home")),
            patch("velora.run.ensure_dir", side_effect=lambda p: p),
            patch("velora.run.upsert_task", return_value={}),
            patch("velora.run.GitHubClient.from_env", return_value=gh),
            patch("velora.run.ensure_repo_checkout", return_value=Path("/tmp/repo")),
            patch("velora.run.run_coordinator", side_effect=coord_runs),
            patch("velora.run.run_worker", side_effect=_worker_side_effect),
            patch("velora.run._poll_ci", return_value=("success", "ok")),
            patch("velora.run._read_diff_for_review", return_value="fake diff"),
            patch("velora.run._run_review_with_retry", side_effect=review_calls),
            patch("velora.run._cleanup_repo_detritus", return_value=None),
            patch("velora.run._append_text", return_value=None),
            patch("velora.run._write_text", return_value=None),
            patch("velora.run._dbg", return_value=None),
        ):
            result = run_task_mode_a("octocat/velora", "feature", RunSpec(task="test", max_attempts=10))

        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["summary"], "fixed and approved")
        # Verify multiple worker invocations occurred
        self.assertEqual(worker_call_count[0], 2)

    # ------------------------------------------------------------------
    # 3. Full review cycle: reject → dismiss_finding → finalize
    # ------------------------------------------------------------------

    def test_full_review_cycle_dismiss_finding_then_finalize(self):
        """execute_work_item → CI passes → request_review (blocker) → dismiss_finding → finalize."""
        gh = MagicMock()
        gh.get_default_branch.return_value = "main"
        gh.create_pull_request.return_value = {"html_url": "https://example/pr/1", "number": 1}
        gh.post_issue_comment.return_value = {}

        coord_runs = [
            # Iteration 1: execute work item
            _coord_ns(_execute_response()),
            # Iteration 2 (after CI): request review — reviewer rejects with blocker
            _coord_ns(_request_review_response(brief_id="RB-0001")),
            # Iteration 3: dismiss the blocker finding
            _coord_ns(_dismiss_finding_response(finding_ids=["RB-0001-f0"], justification="False positive; tested manually")),
            # Iteration 4: finalize
            _coord_ns(_finalize_response("dismissed and done")),
        ]

        review_calls = [
            ("approved", "OK"),  # legacy review in POLLING_CI for iteration 1
            ("blocker", "- **BLOCKER:** Potential regression in edge case"),  # structured review RB-0001
        ]

        with (
            patch.dict(
                os.environ,
                {"VELORA_ALLOWED_OWNERS": "octocat", "VELORA_MODE_A_REVIEW_ENABLED": "1"},
                clear=False,
            ),
            patch("velora.run.build_task_id", return_value="task123"),
            patch("velora.run.velora_home", return_value=Path("/tmp/velora-home")),
            patch("velora.run.ensure_dir", side_effect=lambda p: p),
            patch("velora.run.upsert_task", return_value={}),
            patch("velora.run.GitHubClient.from_env", return_value=gh),
            patch("velora.run.ensure_repo_checkout", return_value=Path("/tmp/repo")),
            patch("velora.run.run_coordinator", side_effect=coord_runs),
            patch("velora.run.run_worker", side_effect=_run_codex_writing_result(_work_result_json())),
            patch("velora.run._poll_ci", return_value=("success", "ok")),
            patch("velora.run._read_diff_for_review", return_value="fake diff"),
            patch("velora.run._run_review_with_retry", side_effect=review_calls),
            patch("velora.run._cleanup_repo_detritus", return_value=None),
            patch("velora.run._append_text", return_value=None),
            patch("velora.run._write_text", return_value=None),
            patch("velora.run._dbg", return_value=None),
        ):
            result = run_task_mode_a("octocat/velora", "feature", RunSpec(task="test", max_attempts=10))

        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["summary"], "dismissed and done")

    # ------------------------------------------------------------------
    # 4. review_enabled blocks finalize_success without prior review
    # ------------------------------------------------------------------

    def test_review_enabled_blocks_finalize_without_review(self):
        """With review_enabled=True, finalize_success without any request_review raises ProtocolError.

        The policy gate in _state_terminal checks review_has_occurred, which is only
        set by _state_dispatching_review (structured request_review) and by the legacy
        review in _state_polling_ci.  To trigger the gate, the coordinator must issue
        finalize_success before any work/CI/review cycle has occurred -- i.e., as the
        very first decision.
        """
        gh = MagicMock()
        gh.get_default_branch.return_value = "main"

        coord_runs = [
            # Iteration 1: coordinator immediately tries to finalize without any work or review
            _coord_ns(_finalize_response("done without review")),
        ]

        with (
            patch.dict(
                os.environ,
                {"VELORA_ALLOWED_OWNERS": "octocat", "VELORA_MODE_A_REVIEW_ENABLED": "1"},
                clear=False,
            ),
            patch("velora.run.build_task_id", return_value="task123"),
            patch("velora.run.velora_home", return_value=Path("/tmp/velora-home")),
            patch("velora.run.ensure_dir", side_effect=lambda p: p),
            patch("velora.run.upsert_task", return_value={}),
            patch("velora.run.GitHubClient.from_env", return_value=gh),
            patch("velora.run.ensure_repo_checkout", return_value=Path("/tmp/repo")),
            patch("velora.run.run_coordinator", side_effect=coord_runs),
            patch("velora.run._append_text", return_value=None),
            patch("velora.run._write_text", return_value=None),
            patch("velora.run._dbg", return_value=None),
        ):
            with self.assertRaises(ProtocolError) as ctx:
                run_task_mode_a("octocat/velora", "feature", RunSpec(task="test", max_attempts=5))

            self.assertIn("finalize_success is not allowed", str(ctx.exception))
            self.assertIn("review_enabled=True", str(ctx.exception))
            self.assertIn("no request_review", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
