import os
import json
import shutil
from pathlib import Path
from types import SimpleNamespace
from typing import Any
import unittest
from unittest.mock import MagicMock, patch

from velora.acpx import CmdResult
from velora.config import get_config
from velora.protocol import ProtocolError, validate_coordinator_response
from velora.run import _append_iteration_history_entry, _parse_worker_work_result, run_task_mode_a
from velora.spec import RunSpec


def _execute_response(*, runner: str = "codex"):
    payload = {
        "protocol_version": 1,
        "decision": "execute_work_item",
        "reason": "apply change",
        "selected_specialist": {"role": "implementer", "runner": runner},
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


def _finalize_response(reason: str = "done") -> Any:
    payload = {
        "protocol_version": 1,
        "decision": "finalize_success",
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

    def test_iteration_history_keeps_only_last_three_work_items(self):
        request = {"history": {"work_items_executed": []}}
        work_item = SimpleNamespace(
            id="WI-0001",
            kind="implement",
            rationale="make progress",
            acceptance=SimpleNamespace(gates=["tests"]),
        )
        specialist = SimpleNamespace(role="implementer", runner="codex", model=None)
        worker_result = _parse_worker_work_result(
            _work_result_json(),
            expected_work_item_id="WI-0001",
            expected_branch="velora/task123",
        )

        for iteration in range(1, 7):
            _append_iteration_history_entry(
                request,
                iteration=iteration,
                work_item=work_item,
                selected_specialist=specialist,
                worker_result=worker_result,
                outcome=f"iteration-{iteration}",
            )

        history = request["history"]["work_items_executed"]
        self.assertEqual(len(history), 3)
        self.assertEqual([entry["iteration"] for entry in history], [4, 5, 6])
        self.assertEqual([entry["outcome"] for entry in history], ["iteration-4", "iteration-5", "iteration-6"])

    def test_coordinator_retries_once_on_transient_error(self):
        gh = MagicMock()
        gh.get_default_branch.return_value = "main"

        coord_runs = [
            RuntimeError("upstream timeout"),
            SimpleNamespace(response=_finalize_response("recovered"), cmd=CmdResult(0, "", "")),
        ]

        with (
            patch.dict(os.environ, {"VELORA_ALLOWED_OWNERS": "octocat"}, clear=False),
            patch("velora.run.build_task_id", return_value="task123"),
            patch("velora.run.velora_home", return_value=Path("/tmp/velora-home")),
            patch("velora.run.ensure_dir", side_effect=lambda p: p),
            patch("velora.run.upsert_task", return_value={}),
            patch("velora.run.GitHubClient.from_env", return_value=gh),
            patch("velora.run.ensure_repo_checkout", return_value=Path("/tmp/repo")),
            patch("velora.run.run_coordinator", side_effect=coord_runs) as run_coord,
            patch("velora.run.time.sleep", return_value=None) as mock_sleep,
            patch("velora.run._append_text", return_value=None),
            patch("velora.run._write_text", return_value=None),
            patch("velora.run._dbg", return_value=None),
        ):
            result = run_task_mode_a("octocat/velora", "feature", RunSpec(task="test", max_attempts=1))

        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["summary"], "recovered")
        self.assertEqual(run_coord.call_count, 2)
        mock_sleep.assert_called_once_with(1)

    def test_coordinator_retries_once_on_protocol_error_and_continues(self):
        gh = MagicMock()
        gh.get_default_branch.return_value = "main"

        coord_runs = [
            ProtocolError("coordinator_response.work_item.id must be a non-empty string"),
            SimpleNamespace(response=_finalize_response("fixed schema"), cmd=CmdResult(0, "", "")),
        ]

        with (
            patch.dict(os.environ, {"VELORA_ALLOWED_OWNERS": "octocat"}, clear=False),
            patch("velora.run.build_task_id", return_value="task123"),
            patch("velora.run.velora_home", return_value=Path("/tmp/velora-home")),
            patch("velora.run.ensure_dir", side_effect=lambda p: p),
            patch("velora.run.upsert_task", return_value={}),
            patch("velora.run.GitHubClient.from_env", return_value=gh),
            patch("velora.run.ensure_repo_checkout", return_value=Path("/tmp/repo")),
            patch("velora.run.run_coordinator", side_effect=coord_runs) as run_coord,
            patch("velora.run._append_text", return_value=None),
            patch("velora.run._write_text", return_value=None),
            patch("velora.run._dbg", return_value=None),
        ):
            result = run_task_mode_a("octocat/velora", "feature", RunSpec(task="test", max_attempts=1))

        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["summary"], "fixed schema")
        self.assertEqual(run_coord.call_count, 2)
        retry_request = run_coord.call_args_list[1].kwargs["request"]
        self.assertIn("repair", retry_request)
        self.assertIn("validation_error", retry_request["repair"])
        self.assertIn("JSON", retry_request["repair"]["instructions"])

    def test_coordinator_protocol_retry_exhaustion_fails_task(self):
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
            patch(
                "velora.run.run_coordinator",
                side_effect=[ProtocolError("first schema error"), ProtocolError("second schema error")],
            ) as run_coord,
            patch("velora.run._write_text", return_value=None),
            patch("velora.run._dbg", return_value=None),
        ):
            result = run_task_mode_a("octocat/velora", "feature", RunSpec(task="test", max_attempts=1))

        self.assertEqual(result["status"], "failed")
        self.assertIn("failed after one retry", result["summary"])
        self.assertIn("second schema error", result["summary"])
        self.assertEqual(run_coord.call_count, 2)

    def test_coordinator_valid_response_does_not_trigger_protocol_retry(self):
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
            patch("velora.run.run_coordinator", return_value=SimpleNamespace(response=_finalize_response("ok"), cmd=CmdResult(0, "", ""))) as run_coord,
            patch("velora.run._append_text", return_value=None),
            patch("velora.run._write_text", return_value=None),
            patch("velora.run._dbg", return_value=None),
        ):
            result = run_task_mode_a("octocat/velora", "feature", RunSpec(task="test", max_attempts=1))

        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["summary"], "ok")
        self.assertEqual(run_coord.call_count, 1)

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
            patch("velora.run.run_coordinator", side_effect=coord_runs),
            patch("velora.run.run_worker", side_effect=_run_codex_writing_result(_work_result_json())),
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

    def test_mode_a_writes_non_empty_run_scoped_audit_log(self):
        gh = MagicMock()
        gh.get_default_branch.return_value = "main"
        gh.create_pull_request.return_value = {"html_url": "https://example/pr/1", "number": 1}
        gh.post_issue_comment.return_value = {}

        audit_path = Path("/tmp/repo/.velora/runs/audit-task/audit.jsonl")
        shutil.rmtree(audit_path.parent, ignore_errors=True)

        def _worker_for_audit_task(*args, **kwargs):
            result_path = Path("/tmp/repo/.velora/exchange/runs/audit-task/WI-0001/result.json")
            result_path.parent.mkdir(parents=True, exist_ok=True)
            result_path.write_text(_work_result_json(branch="velora/audit-task"), encoding="utf-8")
            return CmdResult(0, "worker chatter", "")

        with (
            patch.dict(os.environ, {"VELORA_ALLOWED_OWNERS": "octocat"}, clear=False),
            patch("velora.run.build_task_id", return_value="audit-task"),
            patch("velora.run.velora_home", return_value=Path("/tmp/velora-home")),
            patch("velora.run.ensure_dir", side_effect=lambda p: p),
            patch("velora.run.upsert_task", return_value={}),
            patch("velora.run.GitHubClient.from_env", return_value=gh),
            patch("velora.run.ensure_repo_checkout", return_value=Path("/tmp/repo")),
            patch("velora.run.run_coordinator", return_value=SimpleNamespace(response=_execute_response(), cmd=CmdResult(0, "", ""))),
            patch("velora.run.run_worker", side_effect=_worker_for_audit_task),
            patch("velora.run._poll_ci", return_value=("success", "ok")),
            patch("velora.run._read_diff_for_review", return_value="diff"),
            patch("velora.run.run_gemini_review", return_value=CmdResult(0, "OK", "")),
            patch("velora.run._cleanup_repo_detritus", return_value=None),
            patch("velora.run._append_text", return_value=None),
            patch("velora.run._write_text", return_value=None),
            patch("velora.run._dbg", return_value=None),
        ):
            result = run_task_mode_a("octocat/velora", "feature", RunSpec(task="test", max_attempts=1))

        self.assertEqual(result["status"], "ready")
        self.assertTrue(audit_path.exists())
        lines = [line for line in audit_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        self.assertGreater(len(lines), 0)
        event_types = {json.loads(line)["event_type"] for line in lines}
        self.assertIn("run_start", event_types)
        self.assertIn("work_item_dispatched", event_types)
        self.assertIn("work_item_completed", event_types)
        self.assertIn("run_end", event_types)

    def test_mode_a_optional_post_success_review_handoff_populates_evaluation_and_audit_events(self):
        gh = MagicMock()
        gh.get_default_branch.return_value = "main"
        gh.create_pull_request.return_value = {"html_url": "https://example/pr/1", "number": 1}
        gh.post_issue_comment.return_value = {}

        captured_requests: list[dict[str, Any]] = []

        def _coord_side_effect(*args, **kwargs):
            captured_requests.append(json.loads(json.dumps(kwargs["request"])))
            if len(captured_requests) == 1:
                return SimpleNamespace(response=_execute_response(), cmd=CmdResult(0, "", ""))
            return SimpleNamespace(response=_finalize_response("approved"), cmd=CmdResult(0, "", ""))

        audit_path = Path("/tmp/repo/.velora/runs/review-stage-task/audit.jsonl")
        shutil.rmtree(audit_path.parent, ignore_errors=True)

        def _worker_for_review_stage_task(*args, **kwargs):
            result_path = Path("/tmp/repo/.velora/exchange/runs/review-stage-task/WI-0001/result.json")
            result_path.parent.mkdir(parents=True, exist_ok=True)
            result_path.write_text(_work_result_json(branch="velora/review-stage-task"), encoding="utf-8")
            return CmdResult(0, "worker chatter", "")

        with (
            patch.dict(
                os.environ,
                {
                    "VELORA_ALLOWED_OWNERS": "octocat",
                    "VELORA_MODE_A_REVIEW_ENABLED": "1",
                },
                clear=False,
            ),
            patch("velora.run.build_task_id", return_value="review-stage-task"),
            patch("velora.run.velora_home", return_value=Path("/tmp/velora-home")),
            patch("velora.run.ensure_dir", side_effect=lambda p: p),
            patch("velora.run.upsert_task", return_value={}),
            patch("velora.run.GitHubClient.from_env", return_value=gh),
            patch("velora.run.ensure_repo_checkout", return_value=Path("/tmp/repo")),
            patch("velora.run.run_coordinator", side_effect=_coord_side_effect),
            patch("velora.run.run_worker", side_effect=_worker_for_review_stage_task),
            patch("velora.run._poll_ci", return_value=("success", "ok")),
            patch("velora.run._read_diff_for_review", return_value="diff"),
            patch("velora.run.run_gemini_review", return_value=CmdResult(0, "- NIT: add edge-case test coverage", "")),
            patch("velora.run._cleanup_repo_detritus", return_value=None),
            patch("velora.run._append_text", return_value=None),
            patch("velora.run._write_text", return_value=None),
            patch("velora.run._dbg", return_value=None),
        ):
            result = run_task_mode_a("octocat/velora", "feature", RunSpec(task="test", max_attempts=2))

        self.assertEqual(result["status"], "ready")
        self.assertEqual(len(captured_requests), 2)
        second = captured_requests[1]
        self.assertEqual(second["evaluation"]["outcome"], "post_success_review_repair")
        self.assertEqual(second["evaluation"]["review_result"]["outcome"], "repair")
        self.assertEqual(second["evaluation"]["review_result"]["issues_found"], ["add edge-case test coverage"])

        lines = [line for line in audit_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        event_types = [json.loads(line)["event_type"] for line in lines]
        self.assertIn("review_started", event_types)
        self.assertIn("review_completed", event_types)

    def test_mode_a_respects_explicit_worker_backend_override(self):
        gh = MagicMock()
        gh.get_default_branch.return_value = "main"
        gh.create_pull_request.return_value = {"html_url": "https://example/pr/1", "number": 1}
        gh.post_issue_comment.return_value = {}

        with (
            patch.dict(
                os.environ,
                {"VELORA_ALLOWED_OWNERS": "octocat", "VELORA_WORKER_BACKEND": "direct-claude"},
                clear=False,
            ),
            patch("velora.run.build_task_id", return_value="task123"),
            patch("velora.run.velora_home", return_value=Path("/tmp/velora-home")),
            patch("velora.run.ensure_dir", side_effect=lambda p: p),
            patch("velora.run.upsert_task", return_value={}),
            patch("velora.run.GitHubClient.from_env", return_value=gh),
            patch("velora.run.ensure_repo_checkout", return_value=Path("/tmp/repo")),
            patch(
                "velora.run.run_coordinator",
                return_value=SimpleNamespace(response=_execute_response(runner="claude"), cmd=CmdResult(0, "", "")),
            ),
            patch("velora.run.run_worker", side_effect=_run_codex_writing_result(_work_result_json())) as run_worker,
            patch("velora.run._poll_ci", return_value=("success", "ok")),
            patch("velora.run._read_diff_for_review", return_value="diff"),
            patch("velora.run.run_gemini_review", return_value=CmdResult(0, "OK", "")),
            patch("velora.run._cleanup_repo_detritus", return_value=None),
            patch("velora.run._append_text", return_value=None),
            patch("velora.run._write_text", return_value=None),
            patch("velora.run._dbg", return_value=None),
        ):
            result = run_task_mode_a("octocat/velora", "feature", RunSpec(task="test", max_attempts=1))

        self.assertEqual(result["status"], "ready")
        self.assertEqual(run_worker.call_args.kwargs["runner"], "claude")
        self.assertEqual(run_worker.call_args.kwargs["backend"], "direct-claude")

    def test_mode_a_respects_explicit_direct_codex_worker_backend_override(self):
        gh = MagicMock()
        gh.get_default_branch.return_value = "main"
        gh.create_pull_request.return_value = {"html_url": "https://example/pr/1", "number": 1}
        gh.post_issue_comment.return_value = {}

        with (
            patch.dict(
                os.environ,
                {"VELORA_ALLOWED_OWNERS": "octocat", "VELORA_WORKER_BACKEND": "direct-codex"},
                clear=False,
            ),
            patch("velora.run.build_task_id", return_value="task123"),
            patch("velora.run.velora_home", return_value=Path("/tmp/velora-home")),
            patch("velora.run.ensure_dir", side_effect=lambda p: p),
            patch("velora.run.upsert_task", return_value={}),
            patch("velora.run.GitHubClient.from_env", return_value=gh),
            patch("velora.run.ensure_repo_checkout", return_value=Path("/tmp/repo")),
            patch(
                "velora.run.run_coordinator",
                return_value=SimpleNamespace(response=_execute_response(runner="codex"), cmd=CmdResult(0, "", "")),
            ),
            patch("velora.run.run_worker", side_effect=_run_codex_writing_result(_work_result_json())) as run_worker,
            patch("velora.run._poll_ci", return_value=("success", "ok")),
            patch("velora.run._read_diff_for_review", return_value="diff"),
            patch("velora.run.run_gemini_review", return_value=CmdResult(0, "OK", "")),
            patch("velora.run._cleanup_repo_detritus", return_value=None),
            patch("velora.run._append_text", return_value=None),
            patch("velora.run._write_text", return_value=None),
            patch("velora.run._dbg", return_value=None),
        ):
            result = run_task_mode_a("octocat/velora", "feature", RunSpec(task="test", max_attempts=1))

        self.assertEqual(result["status"], "ready")
        self.assertEqual(run_worker.call_args.kwargs["runner"], "codex")
        self.assertEqual(run_worker.call_args.kwargs["backend"], "direct-codex")

    def test_mode_a_rejects_mismatched_worker_backend_override(self):
        gh = MagicMock()
        gh.get_default_branch.return_value = "main"

        with (
            patch.dict(
                os.environ,
                {"VELORA_ALLOWED_OWNERS": "octocat", "VELORA_WORKER_BACKEND": "direct-claude"},
                clear=False,
            ),
            patch("velora.run.build_task_id", return_value="task123"),
            patch("velora.run.velora_home", return_value=Path("/tmp/velora-home")),
            patch("velora.run.ensure_dir", side_effect=lambda p: p),
            patch("velora.run.upsert_task", return_value={}),
            patch("velora.run.GitHubClient.from_env", return_value=gh),
            patch("velora.run.ensure_repo_checkout", return_value=Path("/tmp/repo")),
            patch(
                "velora.run.run_coordinator",
                return_value=SimpleNamespace(response=_execute_response(runner="codex"), cmd=CmdResult(0, "", "")),
            ),
            patch("velora.run.run_worker") as run_worker,
            patch("velora.run._append_text", return_value=None),
            patch("velora.run._write_text", return_value=None),
            patch("velora.run._dbg", return_value=None),
        ):
            result = run_task_mode_a("octocat/velora", "feature", RunSpec(task="test", max_attempts=1))

        self.assertEqual(result["status"], "failed")
        self.assertIn("Invalid worker backend selection", result["summary"])
        self.assertIn("does not match selected runner", result["summary"])
        run_worker.assert_not_called()

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
            patch("velora.run.run_coordinator", return_value=SimpleNamespace(response=_execute_response(), cmd=CmdResult(0, "", ""))),
            patch("velora.run.run_worker", return_value=CmdResult(0, "not json", "")),
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
            patch("velora.run.run_coordinator", return_value=SimpleNamespace(response=_execute_response(), cmd=CmdResult(0, "", ""))),
            patch("velora.run.run_worker", side_effect=_run_codex_writing_result(_work_result_json(status="blocked"), filename="block.json")),
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
            patch("velora.run.run_coordinator", return_value=SimpleNamespace(response=_execute_response(), cmd=CmdResult(0, "", ""))),
            patch("velora.run.run_worker", side_effect=_run_codex_writing_result(_work_result_json(branch="velora/not-task123"))),
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
            patch("velora.run.run_coordinator", side_effect=_coord_side_effect),
            patch("velora.run.run_worker", side_effect=_run_codex_writing_result(_work_result_json(), filename="handoff.json")),
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
            patch("velora.run.run_coordinator", side_effect=_coord_side_effect),
            patch("velora.run.run_worker", side_effect=_run_codex_writing_result(_work_result_json(status="blocked"), filename="block.json")),
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
            patch("velora.run.run_coordinator", side_effect=_coord_side_effect),
            patch("velora.run.run_worker", side_effect=_run_codex_writing_result(_work_result_json())),
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
            patch("velora.run.run_coordinator", side_effect=_coord_side_effect),
            patch("velora.run.run_worker", side_effect=_run_codex_writing_result(_work_result_json())),
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
