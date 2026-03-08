import os
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
import unittest
from unittest.mock import MagicMock, patch

from velora.acpx import CmdResult
from velora.config import get_config
from velora.protocol import ProtocolError, validate_coordinator_response
from velora.run import _parse_worker_work_result, run_task_mode_a
from velora.spec import RunSpec


def _execute_response():
    payload = {
        "protocol_version": 1,
        "decision": "execute_work_item",
        "reason": "apply change",
        "selected_specialist": {"role": "implementer", "runner": "codex"},
        "work_item": {
            "id": "WI-0001",
            "kind": "implement",
            "rationale": "make progress",
            "instructions": ["Do the change."],
            "scope_hints": {"likely_files": ["velora/run.py"], "search_terms": ["work_result"]},
            "acceptance": {"must": ["works"], "must_not": [], "gates": ["tests"]},
            "limits": {"max_diff_lines": 50, "max_commits": 1},
            "commit": {
                "message": "feat: phase 4",
                "footer": {"VELORA_RUN_ID": "task123", "VELORA_ITERATION": 1, "WORK_ITEM_ID": "WI-0001"},
            },
        },
    }
    return validate_coordinator_response(payload)


def _stop_response(reason: str = "stop") -> Any:
    payload = {
        "protocol_version": 1,
        "decision": "stop_failure",
        "reason": reason,
        "selected_specialist": {"role": "investigator", "runner": "codex"},
    }
    return validate_coordinator_response(payload)


def _work_result_json(*, status: str = "completed", branch: str = "velora/task123", sha: str = "abc123") -> str:
    if status == "completed":
        blockers = []
    else:
        blockers = ["blocked on missing credentials"]
        branch = ""
        sha = ""
    return (
        '{'
        f'"protocol_version":1,"work_item_id":"WI-0001","status":"{status}","summary":"worker summary",'
        f'"branch":"{branch}","head_sha":"{sha}","files_touched":["velora/run.py"],'
        '"tests_run":[{"command":"pytest tests","status":"pass","details":"ok"}],'
        f'"blockers":{json.dumps(blockers)},"follow_up":["next"],"evidence":["proof"]'
        '}'
    )


def _run_codex_writing_result(payload: str, *, repo_path: str = "/tmp/repo", filename: str = "result.json"):
    result_path = Path(repo_path) / ".velora" / "exchange" / "runs" / "task123" / "WI-0001" / filename

    def _runner(*args, **kwargs):
        result_path.parent.mkdir(parents=True, exist_ok=True)
        result_path.write_text(payload, encoding="utf-8")
        return CmdResult(0, "worker chatter", "")

    return _runner


class TestModeAWorkResultIntegration(unittest.TestCase):
    def setUp(self):
        get_config.cache_clear()
        self.publish_branch = patch("velora.run._publish_branch", return_value=None)
        self.mock_publish_branch = self.publish_branch.start()

    def tearDown(self):
        self.publish_branch.stop()
        get_config.cache_clear()

    def test_parse_worker_work_result_validates_and_binds_work_item_id(self):
        result = _parse_worker_work_result(
            _work_result_json(),
            expected_work_item_id="WI-0001",
            expected_branch="velora/task123",
        )
        self.assertEqual(result.summary, "worker summary")
        self.assertEqual(result.branch, "velora/task123")

        with self.assertRaises(ProtocolError):
            _parse_worker_work_result(_work_result_json(), expected_work_item_id="WI-9999")

        with self.assertRaises(ProtocolError):
            _parse_worker_work_result(
                _work_result_json(branch="velora/wrong-branch"),
                expected_work_item_id="WI-0001",
                expected_branch="velora/task123",
            )

    def test_mode_a_uses_work_result_fields_for_pr_and_ci(self):
        gh = MagicMock()
        gh.get_default_branch.return_value = "main"
        gh.create_pull_request.return_value = {"html_url": "https://example/pr/1", "number": 1}
        gh.post_issue_comment.return_value = {}

        coord_runs = [
            SimpleNamespace(response=_execute_response(), cmd=CmdResult(0, "", "")),
        ]

        with (
            patch.dict(os.environ, {"VELORA_ALLOWED_OWNERS": "octocat"}, clear=False),
            patch("velora.run.build_task_id", return_value="task123"),
            patch("velora.run.velora_home", return_value=Path("/tmp/velora-home")),
            patch("velora.run.ensure_dir", side_effect=lambda p: p),
            patch("velora.run.upsert_task", return_value={}),
            patch("velora.run.GitHubClient.from_env", return_value=gh),
            patch("velora.run.ensure_repo_checkout", return_value=Path("/tmp/repo")),
            patch("velora.run.run_coordinator_v1_with_cmd", side_effect=coord_runs),
            patch("velora.run.run_codex", side_effect=_run_codex_writing_result(_work_result_json())),
            patch("velora.run._poll_ci", return_value=("success", "ok")) as poll_ci,
            patch("velora.run._read_diff_for_review", return_value="diff"),
            patch("velora.run.run_gemini_review", return_value=CmdResult(0, "OK", "")),
            patch("velora.run._cleanup_repo_detritus", return_value=None),
            patch("velora.run._append_text", return_value=None),
            patch("velora.run._write_text", return_value=None),
            patch("velora.run._dbg", return_value=None),
        ):
            result = run_task_mode_a("octocat/velora", "feature", RunSpec(task="test", max_attempts=1))

        self.assertEqual(result["status"], "ready")
        self.mock_publish_branch.assert_called_once_with(
            repo_path=Path("/tmp/repo"),
            branch="velora/task123",
            expected_head_sha="abc123",
        )
        self.assertEqual(gh.create_pull_request.call_args.kwargs["head"], "velora/task123")
        self.assertEqual(poll_ci.call_args.args[3], "abc123")

    def test_mode_a_treats_malformed_worker_output_as_protocol_failure(self):
        gh = MagicMock()
        gh.get_default_branch.return_value = "main"

        with (
            patch.dict(os.environ, {"VELORA_ALLOWED_OWNERS": "octocat"}, clear=False),
            patch("velora.run.build_task_id", return_value="task123"),
            patch("velora.run.velora_home", return_value=Path("/tmp/velora-home")),
            patch("velora.run.ensure_dir", side_effect=lambda p: p),
            patch("velora.run.upsert_task", return_value={}),
            patch("velora.run.GitHubClient.from_env", return_value=gh),
            patch("velora.run.ensure_repo_checkout", return_value=Path("/tmp/repo")),
            patch("velora.run.run_coordinator_v1_with_cmd", return_value=SimpleNamespace(response=_execute_response(), cmd=CmdResult(0, "", ""))),
            patch("velora.run.run_codex", return_value=CmdResult(0, "not json", "")),
            patch("velora.run._cleanup_repo_detritus", return_value=None),
            patch("velora.run._append_text", return_value=None),
            patch("velora.run._write_text", return_value=None),
            patch("velora.run._dbg", return_value=None),
        ):
            result = run_task_mode_a("octocat/velora", "feature", RunSpec(task="test", max_attempts=1))

        self.assertEqual(result["status"], "failed")
        self.assertIn("Worker protocol failure", result["summary"])

    def test_mode_a_worker_blocked_result_skips_ci_polling(self):
        gh = MagicMock()
        gh.get_default_branch.return_value = "main"

        with (
            patch.dict(os.environ, {"VELORA_ALLOWED_OWNERS": "octocat"}, clear=False),
            patch("velora.run.build_task_id", return_value="task123"),
            patch("velora.run.velora_home", return_value=Path("/tmp/velora-home")),
            patch("velora.run.ensure_dir", side_effect=lambda p: p),
            patch("velora.run.upsert_task", return_value={}),
            patch("velora.run.GitHubClient.from_env", return_value=gh),
            patch("velora.run.ensure_repo_checkout", return_value=Path("/tmp/repo")),
            patch("velora.run.run_coordinator_v1_with_cmd", return_value=SimpleNamespace(response=_execute_response(), cmd=CmdResult(0, "", ""))),
            patch("velora.run.run_codex", side_effect=_run_codex_writing_result(_work_result_json(status="blocked"), filename="block.json")),
            patch("velora.run._poll_ci", return_value=("success", "ok")) as poll_ci,
            patch("velora.run._cleanup_repo_detritus", return_value=None),
            patch("velora.run._append_text", return_value=None),
            patch("velora.run._write_text", return_value=None),
            patch("velora.run._dbg", return_value=None),
        ):
            result = run_task_mode_a("octocat/velora", "feature", RunSpec(task="test", max_attempts=1))

        self.assertEqual(result["status"], "failed")
        poll_ci.assert_not_called()
        gh.create_pull_request.assert_not_called()

    def test_mode_a_rejects_completed_result_on_unassigned_branch(self):
        gh = MagicMock()
        gh.get_default_branch.return_value = "main"

        with (
            patch.dict(os.environ, {"VELORA_ALLOWED_OWNERS": "octocat"}, clear=False),
            patch("velora.run.build_task_id", return_value="task123"),
            patch("velora.run.velora_home", return_value=Path("/tmp/velora-home")),
            patch("velora.run.ensure_dir", side_effect=lambda p: p),
            patch("velora.run.upsert_task", return_value={}),
            patch("velora.run.GitHubClient.from_env", return_value=gh),
            patch("velora.run.ensure_repo_checkout", return_value=Path("/tmp/repo")),
            patch("velora.run.run_coordinator_v1_with_cmd", return_value=SimpleNamespace(response=_execute_response(), cmd=CmdResult(0, "", ""))),
            patch("velora.run.run_codex", side_effect=_run_codex_writing_result(_work_result_json(branch="velora/not-task123"))),
            patch("velora.run._poll_ci", return_value=("success", "ok")) as poll_ci,
            patch("velora.run._cleanup_repo_detritus", return_value=None),
            patch("velora.run._append_text", return_value=None),
            patch("velora.run._write_text", return_value=None),
            patch("velora.run._dbg", return_value=None),
        ):
            result = run_task_mode_a("octocat/velora", "feature", RunSpec(task="test", max_attempts=1))

        self.assertEqual(result["status"], "failed")
        self.assertIn("WorkResult.branch mismatch", result["summary"])
        poll_ci.assert_not_called()
        gh.create_pull_request.assert_not_called()

    def test_mode_a_handoff_loops_back_to_coordinator_without_pr(self):
        gh = MagicMock()
        gh.get_default_branch.return_value = "main"
        captured_requests: list[dict[str, Any]] = []

        def _coord_side_effect(*args, **kwargs):
            captured_requests.append(json.loads(json.dumps(kwargs["request"])))
            if len(captured_requests) == 1:
                return SimpleNamespace(response=_execute_response(), cmd=CmdResult(0, "", ""))
            return SimpleNamespace(response=_stop_response("handoff received"), cmd=CmdResult(0, "", ""))

        with (
            patch.dict(os.environ, {"VELORA_ALLOWED_OWNERS": "octocat"}, clear=False),
            patch("velora.run.build_task_id", return_value="task123"),
            patch("velora.run.velora_home", return_value=Path("/tmp/velora-home")),
            patch("velora.run.ensure_dir", side_effect=lambda p: p),
            patch("velora.run.upsert_task", return_value={}),
            patch("velora.run.GitHubClient.from_env", return_value=gh),
            patch("velora.run.ensure_repo_checkout", return_value=Path("/tmp/repo")),
            patch("velora.run.run_coordinator_v1_with_cmd", side_effect=_coord_side_effect),
            patch("velora.run.run_codex", side_effect=_run_codex_writing_result(_work_result_json(), filename="handoff.json")),
            patch("velora.run._cleanup_repo_detritus", return_value=None),
            patch("velora.run._append_text", return_value=None),
            patch("velora.run._write_text", return_value=None),
            patch("velora.run._dbg", return_value=None),
        ):
            result = run_task_mode_a("octocat/velora", "feature", RunSpec(task="test", max_attempts=2))

        self.assertEqual(result["status"], "failed")
        self.assertEqual(len(captured_requests), 2)
        second = captured_requests[1]
        self.assertEqual(second["state"]["latest_handoff"]["status"], "completed")
        self.assertEqual(second["history"]["work_items_executed"][0]["outcome"], "worker_handoff")
        gh.create_pull_request.assert_not_called()

    def test_mode_a_second_iteration_request_includes_worker_result_artifact_history(self):
        gh = MagicMock()
        gh.get_default_branch.return_value = "main"
        captured_requests: list[dict[str, Any]] = []

        def _coord_side_effect(*args, **kwargs):
            captured_requests.append(json.loads(json.dumps(kwargs["request"])))
            if len(captured_requests) == 1:
                return SimpleNamespace(response=_execute_response(), cmd=CmdResult(0, "", ""))
            return SimpleNamespace(response=_stop_response("blocked"), cmd=CmdResult(0, "", ""))

        with (
            patch.dict(os.environ, {"VELORA_ALLOWED_OWNERS": "octocat"}, clear=False),
            patch("velora.run.build_task_id", return_value="task123"),
            patch("velora.run.velora_home", return_value=Path("/tmp/velora-home")),
            patch("velora.run.ensure_dir", side_effect=lambda p: p),
            patch("velora.run.upsert_task", return_value={}),
            patch("velora.run.GitHubClient.from_env", return_value=gh),
            patch("velora.run.ensure_repo_checkout", return_value=Path("/tmp/repo")),
            patch("velora.run.run_coordinator_v1_with_cmd", side_effect=_coord_side_effect),
            patch("velora.run.run_codex", side_effect=_run_codex_writing_result(_work_result_json(status="blocked"), filename="block.json")),
            patch("velora.run._poll_ci", return_value=("success", "ok")) as poll_ci,
            patch("velora.run._cleanup_repo_detritus", return_value=None),
            patch("velora.run._append_text", return_value=None),
            patch("velora.run._write_text", return_value=None),
            patch("velora.run._dbg", return_value=None),
        ):
            result = run_task_mode_a("octocat/velora", "feature", RunSpec(task="test", max_attempts=2))

        self.assertEqual(result["status"], "failed")
        poll_ci.assert_not_called()
        self.assertEqual(len(captured_requests), 2)
        second = captured_requests[1]
        self.assertEqual(second["evaluation"]["outcome"], "worker_blocked")
        self.assertEqual(second["evaluation"]["worker_result_status"], "blocked")
        self.assertEqual(second["state"]["latest_worker_result"]["status"], "blocked")
        entry = second["history"]["work_items_executed"][0]
        self.assertEqual(entry["outcome"], "worker_blocked")
        self.assertEqual(entry["artifacts"]["worker_result"]["status"], "blocked")
        self.assertNotIn("patch_suggestion", entry)

    def test_mode_a_second_iteration_request_includes_ci_and_review_artifact_outcomes(self):
        gh = MagicMock()
        gh.get_default_branch.return_value = "main"
        gh.create_pull_request.return_value = {"html_url": "https://example/pr/1", "number": 1}
        gh.post_issue_comment.return_value = {}
        captured_requests: list[dict[str, Any]] = []

        def _coord_side_effect(*args, **kwargs):
            captured_requests.append(json.loads(json.dumps(kwargs["request"])))
            if len(captured_requests) == 1:
                return SimpleNamespace(response=_execute_response(), cmd=CmdResult(0, "", ""))
            return SimpleNamespace(response=_stop_response("fix review"), cmd=CmdResult(0, "", ""))

        with (
            patch.dict(os.environ, {"VELORA_ALLOWED_OWNERS": "octocat"}, clear=False),
            patch("velora.run.build_task_id", return_value="task123"),
            patch("velora.run.velora_home", return_value=Path("/tmp/velora-home")),
            patch("velora.run.ensure_dir", side_effect=lambda p: p),
            patch("velora.run.upsert_task", return_value={}),
            patch("velora.run.GitHubClient.from_env", return_value=gh),
            patch("velora.run.ensure_repo_checkout", return_value=Path("/tmp/repo")),
            patch("velora.run.run_coordinator_v1_with_cmd", side_effect=_coord_side_effect),
            patch("velora.run.run_codex", side_effect=_run_codex_writing_result(_work_result_json())),
            patch("velora.run._poll_ci", return_value=("success", "ok")),
            patch("velora.run._read_diff_for_review", return_value="diff"),
            patch("velora.run.run_gemini_review", return_value=CmdResult(0, "- BLOCKER: tests failing", "")),
            patch("velora.run._cleanup_repo_detritus", return_value=None),
            patch("velora.run._append_text", return_value=None),
            patch("velora.run._write_text", return_value=None),
            patch("velora.run._dbg", return_value=None),
        ):
            result = run_task_mode_a("octocat/velora", "feature", RunSpec(task="test", max_attempts=2))

        self.assertEqual(result["status"], "failed")
        self.assertEqual(len(captured_requests), 2)
        second = captured_requests[1]
        self.assertEqual(second["evaluation"]["outcome"], "review-blocker")
        self.assertEqual(second["evaluation"]["ci_state"], "success")
        self.assertEqual(second["evaluation"]["review_result"], "blocker")
        self.assertEqual(second["state"]["latest_ci"]["state"], "success")
        self.assertEqual(second["state"]["latest_review"]["result"], "blocker")
        entry = second["history"]["work_items_executed"][0]
        self.assertEqual(entry["artifacts"]["worker_result"]["status"], "completed")
        self.assertEqual(entry["artifacts"]["ci"]["state"], "success")
        self.assertEqual(entry["artifacts"]["review"]["result"], "blocker")

    def test_mode_a_second_iteration_request_includes_ci_failure_artifact_outcome(self):
        gh = MagicMock()
        gh.get_default_branch.return_value = "main"
        gh.create_pull_request.return_value = {"html_url": "https://example/pr/1", "number": 1}
        captured_requests: list[dict[str, Any]] = []

        def _coord_side_effect(*args, **kwargs):
            captured_requests.append(json.loads(json.dumps(kwargs["request"])))
            if len(captured_requests) == 1:
                return SimpleNamespace(response=_execute_response(), cmd=CmdResult(0, "", ""))
            return SimpleNamespace(response=_stop_response("fix ci"), cmd=CmdResult(0, "", ""))

        with (
            patch.dict(os.environ, {"VELORA_ALLOWED_OWNERS": "octocat"}, clear=False),
            patch("velora.run.build_task_id", return_value="task123"),
            patch("velora.run.velora_home", return_value=Path("/tmp/velora-home")),
            patch("velora.run.ensure_dir", side_effect=lambda p: p),
            patch("velora.run.upsert_task", return_value={}),
            patch("velora.run.GitHubClient.from_env", return_value=gh),
            patch("velora.run.ensure_repo_checkout", return_value=Path("/tmp/repo")),
            patch("velora.run.run_coordinator_v1_with_cmd", side_effect=_coord_side_effect),
            patch("velora.run.run_codex", side_effect=_run_codex_writing_result(_work_result_json())),
            patch("velora.run._poll_ci", return_value=("failure", "tests failed")),
            patch("velora.run._cleanup_repo_detritus", return_value=None),
            patch("velora.run._append_text", return_value=None),
            patch("velora.run._write_text", return_value=None),
            patch("velora.run._dbg", return_value=None),
        ):
            result = run_task_mode_a("octocat/velora", "feature", RunSpec(task="test", max_attempts=2))

        self.assertEqual(result["status"], "failed")
        self.assertEqual(len(captured_requests), 2)
        second = captured_requests[1]
        self.assertEqual(second["evaluation"]["outcome"], "ci_failure")
        self.assertEqual(second["evaluation"]["ci_state"], "failure")
        entry = second["history"]["work_items_executed"][0]
        self.assertEqual(entry["outcome"], "ci_failure")
        self.assertEqual(entry["artifacts"]["ci"]["state"], "failure")
        self.assertEqual(entry["artifacts"]["worker_result"]["status"], "completed")


if __name__ == "__main__":
    unittest.main()
