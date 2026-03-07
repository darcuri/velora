import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from velora.acpx import CmdResult
from velora.config import get_config
from velora.orchestrator import coordinator_session_name, worker_session_name
from velora.protocol import validate_coordinator_response
from velora.run import run_task_mode_a
from velora.spec import RunSpec
from velora.util import repo_slug


def _execute_response():
    payload = {
        "protocol_version": 1,
        "decision": "execute_work_item",
        "reason": "apply first change",
        "selected_specialist": {"role": "implementer", "runner": "codex"},
        "work_item": {
            "id": "WI-0001",
            "kind": "implement",
            "rationale": "make progress",
            "instructions": ["Do the change."],
            "scope_hints": {"likely_files": ["velora/run.py"], "search_terms": ["run_id"]},
            "acceptance": {"must": ["works"], "must_not": [], "gates": ["tests"]},
            "limits": {"max_diff_lines": 50, "max_commits": 1},
            "commit": {
                "message": "feat: phase 2",
                "footer": {"VELORA_RUN_ID": "task123", "VELORA_ITERATION": 1, "WORK_ITEM_ID": "WI-0001"},
            },
        },
    }
    return validate_coordinator_response(payload)


def _stop_response():
    payload = {
        "protocol_version": 1,
        "decision": "stop_failure",
        "reason": "stop now",
        "selected_specialist": {"role": "investigator", "runner": "codex"},
    }
    return validate_coordinator_response(payload)


class TestCoordinatorSessionScoping(unittest.TestCase):
    def setUp(self):
        get_config.cache_clear()

    def tearDown(self):
        get_config.cache_clear()

    def test_coordinator_session_name_uses_run_id(self):
        with patch.dict(os.environ, {"VELORA_CLAUDE_SESSION_PREFIX": "coord-"}, clear=False):
            get_config.cache_clear()
            first = coordinator_session_name("octocat", "velora", "run-1")
            second = coordinator_session_name("octocat", "velora", "run-2")

        prefix = f"coord-{repo_slug('octocat', 'velora')}"
        self.assertEqual(first, f"{prefix}-run-1-coord")
        self.assertEqual(second, f"{prefix}-run-2-coord")
        self.assertNotEqual(first, second)

    def test_worker_session_name_uses_run_id_and_runner_prefix(self):
        with patch.dict(
            os.environ,
            {"VELORA_CLAUDE_SESSION_PREFIX": "coord-", "VELORA_CODEX_SESSION_PREFIX": "worker-"},
            clear=False,
        ):
            get_config.cache_clear()
            first = worker_session_name("octocat", "velora", "run-1", "codex")
            second = worker_session_name("octocat", "velora", "run-2", "codex")
            third = worker_session_name("octocat", "velora", "run-1", "claude")

        slug = repo_slug("octocat", "velora")
        self.assertEqual(first, f"worker-{slug}-run-1-worker")
        self.assertEqual(second, f"worker-{slug}-run-2-worker")
        self.assertEqual(third, f"coord-{slug}-run-1-worker")
        self.assertNotEqual(first, second)
        self.assertNotEqual(first, third)

    def test_mode_a_reuses_same_run_scoped_coordinator_session_each_iteration(self):
        gh = MagicMock()
        gh.get_default_branch.return_value = "main"
        gh.create_pull_request.return_value = {"html_url": "https://example/pr/1", "number": 1}
        gh.post_issue_comment.return_value = {}

        coord_runs = [
            SimpleNamespace(response=_execute_response(), cmd=CmdResult(0, "", "")),
            SimpleNamespace(response=_stop_response(), cmd=CmdResult(0, "", "")),
        ]

        with (
            patch.dict(
                os.environ,
                {"VELORA_ALLOWED_OWNERS": "octocat", "VELORA_CLAUDE_SESSION_PREFIX": "coord-"},
                clear=False,
            ),
            patch("velora.run.build_task_id", return_value="task123"),
            patch("velora.run.velora_home", return_value=Path("/tmp/velora-home")),
            patch("velora.run.ensure_dir", side_effect=lambda p: p),
            patch("velora.run.upsert_task", return_value={}),
            patch("velora.run.GitHubClient.from_env", return_value=gh),
            patch("velora.run.ensure_repo_checkout", return_value=Path("/tmp/repo")),
            patch("velora.run.run_coordinator_v1_with_cmd", side_effect=coord_runs) as run_coord,
            patch("velora.run.run_codex", return_value=CmdResult(0, "worker output", "")),
            patch(
                "velora.run.parse_codex_footer",
                return_value={"branch": "velora/task123", "head_sha": "abc123", "summary": "done"},
            ),
            patch("velora.run._poll_ci", return_value=("failure", "tests failed")),
            patch("velora.run._cleanup_repo_detritus", return_value=None),
            patch("velora.run._append_text", return_value=None),
            patch("velora.run._write_text", return_value=None),
            patch("velora.run._dbg", return_value=None),
        ):
            get_config.cache_clear()
            result = run_task_mode_a("octocat/velora", "feature", RunSpec(task="test task", max_attempts=2))

        self.assertEqual(result["status"], "failed")
        self.assertEqual(run_coord.call_count, 2)
        sessions = [call.kwargs["session_name"] for call in run_coord.call_args_list]
        expected = f"coord-{repo_slug('octocat', 'velora')}-task123-coord"
        self.assertEqual(sessions, [expected, expected])

    def test_mode_a_reuses_same_run_scoped_worker_session_each_iteration(self):
        gh = MagicMock()
        gh.get_default_branch.return_value = "main"
        gh.create_pull_request.return_value = {"html_url": "https://example/pr/1", "number": 1}
        gh.post_issue_comment.return_value = {}

        coord_runs = [
            SimpleNamespace(response=_execute_response(), cmd=CmdResult(0, "", "")),
            SimpleNamespace(response=_execute_response(), cmd=CmdResult(0, "", "")),
            SimpleNamespace(response=_stop_response(), cmd=CmdResult(0, "", "")),
        ]

        with (
            patch.dict(
                os.environ,
                {
                    "VELORA_ALLOWED_OWNERS": "octocat",
                    "VELORA_CLAUDE_SESSION_PREFIX": "coord-",
                    "VELORA_CODEX_SESSION_PREFIX": "worker-",
                },
                clear=False,
            ),
            patch("velora.run.build_task_id", return_value="task123"),
            patch("velora.run.velora_home", return_value=Path("/tmp/velora-home")),
            patch("velora.run.ensure_dir", side_effect=lambda p: p),
            patch("velora.run.upsert_task", return_value={}),
            patch("velora.run.GitHubClient.from_env", return_value=gh),
            patch("velora.run.ensure_repo_checkout", return_value=Path("/tmp/repo")),
            patch("velora.run.run_coordinator_v1_with_cmd", side_effect=coord_runs),
            patch("velora.run.run_codex", return_value=CmdResult(0, "worker output", "")) as run_worker,
            patch(
                "velora.run.parse_codex_footer",
                return_value={"branch": "velora/task123", "head_sha": "abc123", "summary": "done"},
            ),
            patch("velora.run._poll_ci", return_value=("failure", "tests failed")),
            patch("velora.run._cleanup_repo_detritus", return_value=None),
            patch("velora.run._append_text", return_value=None),
            patch("velora.run._write_text", return_value=None),
            patch("velora.run._dbg", return_value=None),
        ):
            get_config.cache_clear()
            result = run_task_mode_a("octocat/velora", "feature", RunSpec(task="test task", max_attempts=3))

        self.assertEqual(result["status"], "failed")
        self.assertEqual(run_worker.call_count, 2)
        sessions = [call.kwargs["session_name"] for call in run_worker.call_args_list]
        expected = f"worker-{repo_slug('octocat', 'velora')}-task123-worker"
        self.assertEqual(sessions, [expected, expected])

    def test_mode_a_worker_sessions_do_not_leak_across_runs(self):
        gh = MagicMock()
        gh.get_default_branch.return_value = "main"
        gh.create_pull_request.return_value = {"html_url": "https://example/pr/1", "number": 1}
        gh.post_issue_comment.return_value = {}

        coord_runs = [
            SimpleNamespace(response=_execute_response(), cmd=CmdResult(0, "", "")),
            SimpleNamespace(response=_stop_response(), cmd=CmdResult(0, "", "")),
            SimpleNamespace(response=_execute_response(), cmd=CmdResult(0, "", "")),
            SimpleNamespace(response=_stop_response(), cmd=CmdResult(0, "", "")),
        ]

        with (
            patch.dict(
                os.environ,
                {"VELORA_ALLOWED_OWNERS": "octocat", "VELORA_CODEX_SESSION_PREFIX": "worker-"},
                clear=False,
            ),
            patch("velora.run.build_task_id", side_effect=["task111", "task222"]),
            patch("velora.run.velora_home", return_value=Path("/tmp/velora-home")),
            patch("velora.run.ensure_dir", side_effect=lambda p: p),
            patch("velora.run.upsert_task", return_value={}),
            patch("velora.run.GitHubClient.from_env", return_value=gh),
            patch("velora.run.ensure_repo_checkout", return_value=Path("/tmp/repo")),
            patch("velora.run.run_coordinator_v1_with_cmd", side_effect=coord_runs),
            patch("velora.run.run_codex", return_value=CmdResult(0, "worker output", "")) as run_worker,
            patch(
                "velora.run.parse_codex_footer",
                return_value={"branch": "velora/task", "head_sha": "abc123", "summary": "done"},
            ),
            patch("velora.run._poll_ci", return_value=("failure", "tests failed")),
            patch("velora.run._cleanup_repo_detritus", return_value=None),
            patch("velora.run._append_text", return_value=None),
            patch("velora.run._write_text", return_value=None),
            patch("velora.run._dbg", return_value=None),
        ):
            get_config.cache_clear()
            run_task_mode_a("octocat/velora", "feature", RunSpec(task="test task", max_attempts=2))
            run_task_mode_a("octocat/velora", "feature", RunSpec(task="test task", max_attempts=2))

        self.assertEqual(run_worker.call_count, 2)
        sessions = [call.kwargs["session_name"] for call in run_worker.call_args_list]
        slug = repo_slug("octocat", "velora")
        self.assertEqual(sessions, [f"worker-{slug}-task111-worker", f"worker-{slug}-task222-worker"])


if __name__ == "__main__":
    unittest.main()
