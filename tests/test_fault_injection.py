import os
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, patch

from velora.acpx import CmdResult
from velora.run import (
    CHECKPOINT_AFTER_CI_SUCCESS_BEFORE_REVIEW,
    InternalFaultInjectionTriggered,
    _INTERNAL_FAULT_CHECKPOINT_ENV,
    _INTERNAL_FAULT_ENABLE_ENV,
    _INTERNAL_FAULT_ENABLE_VALUE,
    resume_task,
    run_task,
)
from velora.spec import RunSpec
from velora.state import get_task, save_tasks


def _codex_footer(branch: str, sha: str, summary: str = "shipped") -> str:
    return f"BRANCH: {branch}\nHEAD_SHA: {sha}\nSUMMARY: {summary}\n"


class TestInternalFaultInjection(unittest.TestCase):
    def _repo_path(self, root: Path) -> Path:
        repo_path = root / "repo"
        repo_path.mkdir()
        return repo_path

    def _mock_gh(self) -> MagicMock:
        mock_gh = MagicMock()
        mock_gh.get_default_branch.return_value = "main"
        mock_gh.create_pull_request.return_value = {"html_url": "https://example/pr/1", "number": 1}
        mock_gh.post_issue_comment.return_value = {}
        return mock_gh

    def test_run_task_fault_hook_requires_enable_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            home.mkdir()
            repo_path = self._repo_path(root)
            mock_gh = self._mock_gh()

            env = {_INTERNAL_FAULT_CHECKPOINT_ENV: CHECKPOINT_AFTER_CI_SUCCESS_BEFORE_REVIEW}
            with patch.dict(os.environ, env, clear=False), ExitStack() as stack:
                stack.enter_context(patch("velora.run.validate_repo_allowed", return_value=("darcuri", "velora")))
                stack.enter_context(patch("velora.run.GitHubClient.from_env", return_value=mock_gh))
                stack.enter_context(patch("velora.run.ensure_repo_checkout", return_value=repo_path))
                stack.enter_context(patch("velora.run.build_task_id", return_value="task123"))
                stack.enter_context(
                    patch(
                        "velora.run.run_codex",
                        return_value=CmdResult(0, _codex_footer("velora/task123", "abc123"), ""),
                    )
                )
                stack.enter_context(patch("velora.run._poll_ci", return_value=("success", "ok")))
                stack.enter_context(patch("velora.run._read_diff_for_review", return_value="diff"))
                mock_review = stack.enter_context(patch("velora.run.run_gemini_review", return_value=CmdResult(0, "OK", "")))

                result = run_task("darcuri/velora", "feature", RunSpec(task="task text"), home=home, runner="codex")

            self.assertEqual(result["status"], "ready")
            self.assertEqual(mock_review.call_count, 1)

    def test_run_task_fault_hook_requires_checkpoint_selection(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            home.mkdir()
            repo_path = self._repo_path(root)
            mock_gh = self._mock_gh()

            env = {_INTERNAL_FAULT_ENABLE_ENV: _INTERNAL_FAULT_ENABLE_VALUE}
            with patch.dict(os.environ, env, clear=False), ExitStack() as stack:
                stack.enter_context(patch("velora.run.validate_repo_allowed", return_value=("darcuri", "velora")))
                stack.enter_context(patch("velora.run.GitHubClient.from_env", return_value=mock_gh))
                stack.enter_context(patch("velora.run.ensure_repo_checkout", return_value=repo_path))
                stack.enter_context(patch("velora.run.build_task_id", return_value="task123"))
                stack.enter_context(
                    patch(
                        "velora.run.run_codex",
                        return_value=CmdResult(0, _codex_footer("velora/task123", "abc123"), ""),
                    )
                )
                stack.enter_context(patch("velora.run._poll_ci", return_value=("success", "ok")))
                stack.enter_context(patch("velora.run._read_diff_for_review", return_value="diff"))
                mock_review = stack.enter_context(patch("velora.run.run_gemini_review", return_value=CmdResult(0, "OK", "")))

                result = run_task("darcuri/velora", "feature", RunSpec(task="task text"), home=home, runner="codex")

            self.assertEqual(result["status"], "ready")
            self.assertEqual(mock_review.call_count, 1)

    def test_run_task_fault_hook_fires_after_ci_success_and_persists_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            home.mkdir()
            repo_path = self._repo_path(root)
            mock_gh = self._mock_gh()

            env = {
                _INTERNAL_FAULT_ENABLE_ENV: _INTERNAL_FAULT_ENABLE_VALUE,
                _INTERNAL_FAULT_CHECKPOINT_ENV: CHECKPOINT_AFTER_CI_SUCCESS_BEFORE_REVIEW,
            }
            with patch.dict(os.environ, env, clear=False), ExitStack() as stack:
                stack.enter_context(patch("velora.run.validate_repo_allowed", return_value=("darcuri", "velora")))
                stack.enter_context(patch("velora.run.GitHubClient.from_env", return_value=mock_gh))
                stack.enter_context(patch("velora.run.ensure_repo_checkout", return_value=repo_path))
                stack.enter_context(patch("velora.run.build_task_id", return_value="task123"))
                stack.enter_context(
                    patch(
                        "velora.run.run_codex",
                        return_value=CmdResult(0, _codex_footer("velora/task123", "abc123"), ""),
                    )
                )
                stack.enter_context(patch("velora.run._poll_ci", return_value=("success", "ok")))
                stack.enter_context(patch("velora.run._read_diff_for_review", return_value="diff"))
                mock_review = stack.enter_context(patch("velora.run.run_gemini_review", return_value=CmdResult(0, "OK", "")))

                with self.assertRaises(InternalFaultInjectionTriggered):
                    run_task("darcuri/velora", "feature", RunSpec(task="task text"), home=home, runner="codex")

            persisted = get_task("task123", home=home)
            self.assertIsNotNone(persisted)
            self.assertEqual(persisted["persisted_checkpoint"], CHECKPOINT_AFTER_CI_SUCCESS_BEFORE_REVIEW)
            self.assertEqual(persisted["ci_state"], "success")
            self.assertEqual(persisted["ci_detail"], "ok")
            self.assertEqual(mock_review.call_count, 0)
            mock_gh.post_issue_comment.assert_not_called()

    def test_resume_task_fault_hook_fires_after_ci_success_and_persists_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            home.mkdir()
            repo_path = self._repo_path(root)
            save_tasks(
                {
                    "version": 1,
                    "tasks": [
                        {
                            "task_id": "task123",
                            "repo": "darcuri/velora",
                            "verb": "feature",
                            "task": "task text",
                            "status": "running",
                            "branch": "velora/task123",
                            "head_sha": "abc123",
                            "pr_number": 1,
                            "pr_url": "https://example/pr/1",
                            "summary": "resume me",
                            "created_at": "2026-03-05T00:00:00+00:00",
                            "updated_at": "2026-03-05T00:00:00+00:00",
                        }
                    ],
                },
                home=home,
            )
            mock_gh = self._mock_gh()

            env = {
                _INTERNAL_FAULT_ENABLE_ENV: _INTERNAL_FAULT_ENABLE_VALUE,
                _INTERNAL_FAULT_CHECKPOINT_ENV: CHECKPOINT_AFTER_CI_SUCCESS_BEFORE_REVIEW,
            }
            with patch.dict(os.environ, env, clear=False), ExitStack() as stack:
                stack.enter_context(patch("velora.run.validate_repo_allowed", return_value=("darcuri", "velora")))
                stack.enter_context(patch("velora.run.GitHubClient.from_env", return_value=mock_gh))
                stack.enter_context(patch("velora.run.ensure_repo_checkout", return_value=repo_path))
                stack.enter_context(patch("velora.run._run_checked", return_value=""))
                stack.enter_context(patch("velora.run._poll_ci", return_value=("success", "ok")))
                stack.enter_context(patch("velora.run._read_diff_for_review", return_value="diff"))
                mock_review = stack.enter_context(patch("velora.run.run_gemini_review", return_value=CmdResult(0, "OK", "")))

                with self.assertRaises(InternalFaultInjectionTriggered):
                    resume_task("task123", home=home)

            persisted = get_task("task123", home=home)
            self.assertIsNotNone(persisted)
            self.assertEqual(persisted["persisted_checkpoint"], CHECKPOINT_AFTER_CI_SUCCESS_BEFORE_REVIEW)
            self.assertEqual(persisted["ci_state"], "success")
            self.assertEqual(persisted["ci_detail"], "ok")
            self.assertEqual(mock_review.call_count, 0)
            mock_gh.post_issue_comment.assert_not_called()


if __name__ == "__main__":
    unittest.main()
