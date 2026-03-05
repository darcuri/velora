import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from velora.acpx import CmdResult
from velora.config import get_config
from velora.run import run_task
from velora.spec import RunSpec


class TestRunUsesDefaultBranch(unittest.TestCase):
    def setUp(self):
        get_config.cache_clear()

    def tearDown(self):
        get_config.cache_clear()

    def test_default_branch_used_for_pr_and_diff(self):
        mock_gh = MagicMock()
        mock_gh.get_default_branch.return_value = "develop"
        mock_gh.create_pull_request.return_value = {"html_url": "https://example/pr/1", "number": 1}
        mock_gh.post_issue_comment.return_value = {}

        with (
            patch.dict(os.environ, {"VELORA_ALLOWED_OWNERS": "octocat"}, clear=False),
            patch("velora.run.GitHubClient.from_env", return_value=mock_gh),
            patch("velora.run.ensure_repo_checkout", return_value=Path("/tmp/repo")),
            patch("velora.run.build_task_id", return_value="task123"),
            patch("velora.run.velora_home", return_value=Path("/tmp/velora-home")),
            patch("velora.run.ensure_dir", side_effect=lambda p: p),
            patch("velora.run.upsert_task", return_value={}),
            patch("velora.run._write_text", return_value=None),
            patch("velora.run._append_text", return_value=None),
            patch(
                "velora.run.run_codex",
                return_value=CmdResult(
                    0,
                    "BRANCH: velora/task123\nHEAD_SHA: abc123\nSUMMARY: shipped\n",
                    "",
                ),
            ),
            patch("velora.run._poll_ci", return_value=("success", "ok")),
            patch("velora.run._read_diff_for_review", return_value="diff") as mock_diff,
            patch("velora.run.run_gemini_review", return_value=CmdResult(0, "- NIT: ok\n", "")),
        ):
            get_config.cache_clear()
            result = run_task("octocat/velora", "feature", RunSpec(task="task text"))

        self.assertEqual(result["status"], "ready")
        mock_gh.get_default_branch.assert_called_once_with("octocat", "velora")
        mock_gh.create_pull_request.assert_called_once()
        self.assertEqual(mock_gh.create_pull_request.call_args.kwargs["base"], "develop")
        self.assertEqual(mock_diff.call_args.args[1], "develop")

    def test_base_branch_override_skips_default_branch_lookup(self):
        mock_gh = MagicMock()
        mock_gh.get_default_branch.return_value = "develop"
        mock_gh.create_pull_request.return_value = {"html_url": "https://example/pr/1", "number": 1}
        mock_gh.post_issue_comment.return_value = {}

        with (
            patch.dict(os.environ, {"VELORA_ALLOWED_OWNERS": "octocat"}, clear=False),
            patch("velora.run.GitHubClient.from_env", return_value=mock_gh),
            patch("velora.run.ensure_repo_checkout", return_value=Path("/tmp/repo")) as mock_checkout,
            patch("velora.run.build_task_id", return_value="task123"),
            patch("velora.run.velora_home", return_value=Path("/tmp/velora-home")),
            patch("velora.run.ensure_dir", side_effect=lambda p: p),
            patch("velora.run.upsert_task", return_value={}),
            patch("velora.run._write_text", return_value=None),
            patch("velora.run._append_text", return_value=None),
            patch(
                "velora.run.run_codex",
                return_value=CmdResult(
                    0,
                    "BRANCH: velora/task123\nHEAD_SHA: abc123\nSUMMARY: shipped\n",
                    "",
                ),
            ),
            patch("velora.run._poll_ci", return_value=("success", "ok")),
            patch("velora.run._read_diff_for_review", return_value="diff") as mock_diff,
            patch("velora.run.run_gemini_review", return_value=CmdResult(0, "- NIT: ok\n", "")),
        ):
            get_config.cache_clear()
            result = run_task(
                "octocat/velora",
                "feature",
                RunSpec(task="task text"),
                base_branch="release",
            )

        self.assertEqual(result["status"], "ready")
        mock_gh.get_default_branch.assert_not_called()
        mock_checkout.assert_called_once()
        self.assertEqual(mock_gh.create_pull_request.call_args.kwargs["base"], "release")
        self.assertEqual(mock_diff.call_args.args[1], "release")

    def test_mode_a_outage_classification_avoids_second_delegation(self):
        mock_gh = MagicMock()
        mock_gh.get_default_branch.return_value = "develop"
        mock_gh.create_pull_request.return_value = {"html_url": "https://example/pr/1", "number": 1}
        mock_gh.get_check_runs.return_value = {"check_runs": [{"status": "queued", "conclusion": None}]}
        coord = SimpleNamespace(
            decision="execute_work_item",
            reason="do it",
            selected_specialist=SimpleNamespace(role="implementer", runner="codex"),
            work_item=SimpleNamespace(id="WI-0001", kind="implement"),
        )
        coord_run = SimpleNamespace(response=coord, cmd=SimpleNamespace(usage=None))
        with (
            patch.dict(os.environ, {"VELORA_ALLOWED_OWNERS": "octocat"}, clear=False),
            patch("velora.run.GitHubClient.from_env", return_value=mock_gh),
            patch("velora.run.ensure_repo_checkout", return_value=Path("/tmp/repo")),
            patch("velora.run.build_task_id", return_value="task123"),
            patch("velora.run.velora_home", return_value=Path("/tmp/velora-home")),
            patch("velora.run.ensure_dir", side_effect=lambda p: p),
            patch("velora.run.upsert_task", return_value={}),
            patch("velora.run._write_text", return_value=None),
            patch("velora.run._append_text", return_value=None),
            patch("velora.run.run_coordinator_v1_with_cmd", side_effect=[coord_run, coord_run]) as mock_coord,
            patch("velora.run.build_worker_prompt_v1", return_value="prompt"),
            patch("velora.run.run_codex", return_value=CmdResult(0, "BRANCH: velora/task123\nHEAD_SHA: abc123\nSUMMARY: shipped\n", "")),
            patch("velora.run._poll_ci", return_value=("failure", "stuck-no-progress")),
            patch("velora.run.time.sleep", return_value=None),
        ):
            result = run_task("octocat/velora", "feature", RunSpec(task="task text", max_attempts=3), use_coordinator=True)
        self.assertEqual(result["status"], "failed")
        self.assertIn("CI outage suspected", result["summary"])
        self.assertEqual(mock_coord.call_count, 1)

    def test_mode_a_code_failure_still_retries_with_delegation(self):
        mock_gh = MagicMock()
        mock_gh.get_default_branch.return_value = "develop"
        mock_gh.create_pull_request.return_value = {"html_url": "https://example/pr/1", "number": 1}
        mock_gh.get_check_runs.return_value = {"check_runs": [{"status": "completed", "conclusion": "failure", "output": {"summary": "tests failed"}}]}
        coord = SimpleNamespace(
            decision="execute_work_item",
            reason="do it",
            selected_specialist=SimpleNamespace(role="implementer", runner="codex"),
            work_item=SimpleNamespace(id="WI-0001", kind="implement"),
        )
        coord_run = SimpleNamespace(response=coord, cmd=SimpleNamespace(usage=None))
        with (
            patch.dict(os.environ, {"VELORA_ALLOWED_OWNERS": "octocat"}, clear=False),
            patch("velora.run.GitHubClient.from_env", return_value=mock_gh),
            patch("velora.run.ensure_repo_checkout", return_value=Path("/tmp/repo")),
            patch("velora.run.build_task_id", return_value="task123"),
            patch("velora.run.velora_home", return_value=Path("/tmp/velora-home")),
            patch("velora.run.ensure_dir", side_effect=lambda p: p),
            patch("velora.run.upsert_task", return_value={}),
            patch("velora.run._write_text", return_value=None),
            patch("velora.run._append_text", return_value=None),
            patch("velora.run.run_coordinator_v1_with_cmd", side_effect=[coord_run, coord_run]) as mock_coord,
            patch("velora.run.build_worker_prompt_v1", return_value="prompt"),
            patch("velora.run.run_codex", return_value=CmdResult(0, "BRANCH: velora/task123\nHEAD_SHA: abc123\nSUMMARY: shipped\n", "")),
            patch("velora.run._poll_ci", return_value=("failure", "check-runs=failure")),
        ):
            run_task("octocat/velora", "feature", RunSpec(task="task text", max_attempts=2), use_coordinator=True)
        self.assertEqual(mock_coord.call_count, 2)


if __name__ == "__main__":
    unittest.main()
