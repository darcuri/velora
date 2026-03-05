from contextlib import ExitStack
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from velora.acpx import CmdResult
from velora.run import _classify_review_text, _run_review_with_retry, run_task
from velora.spec import RunSpec


def _codex_footer(branch: str, sha: str, summary: str = "shipped") -> str:
    return f"BRANCH: {branch}\nHEAD_SHA: {sha}\nSUMMARY: {summary}\n"


class TestRunReviewGate(unittest.TestCase):
    def _base_patches(self, mock_gh: MagicMock):
        return (
            patch("velora.run.validate_repo_allowed", return_value=("darcuri", "velora")),
            patch("velora.run.GitHubClient.from_env", return_value=mock_gh),
            patch("velora.run.ensure_repo_checkout", return_value=Path("/tmp/repo")),
            patch("velora.run.build_task_id", return_value="task123"),
            patch("velora.run.velora_home", return_value=Path("/tmp/velora-home")),
            patch("velora.run.ensure_dir", side_effect=lambda p: p),
            patch("velora.run.upsert_task", return_value={}),
            patch("velora.run._write_text", return_value=None),
            patch("velora.run._append_text", return_value=None),
            patch("velora.run._poll_ci", return_value=("success", "ok")),
            patch("velora.run._read_diff_for_review", return_value="diff"),
        )

    def test_accepts_ok_variants_as_leading_approval_tokens(self):
        for review_text in ["OK", "OK.", "Ok: Approved."]:
            with self.subTest(review_text=review_text):
                mock_gh = MagicMock()
                mock_gh.get_default_branch.return_value = "main"
                mock_gh.create_pull_request.return_value = {
                    "html_url": "https://example/pr/1",
                    "number": 1,
                }
                mock_gh.post_issue_comment.return_value = {}

                with ExitStack() as stack:
                    for p in self._base_patches(mock_gh):
                        stack.enter_context(p)
                    stack.enter_context(
                        patch(
                            "velora.run.run_codex",
                            return_value=CmdResult(0, _codex_footer("velora/task123", "abc123"), ""),
                        )
                    )
                    stack.enter_context(
                        patch("velora.run.run_gemini_review", return_value=CmdResult(0, review_text, ""))
                    )
                    result = run_task("darcuri/velora", "feature", RunSpec(task="task text"))

                self.assertEqual(result["status"], "ready")
                self.assertEqual(result["review_result"], "approved")

    def test_rejects_non_leading_ok_as_approval(self):
        self.assertEqual(_classify_review_text("Looks good overall. OK."), "malformed")

    def test_accepts_intro_plus_markdown_findings_variants(self):
        review_text = "\n".join(
            [
                "I reviewed this change and found two follow-ups.",
                "",
                "- **BLOCKER:** Null input still raises unexpectedly.",
                "* **NIT**: Consider renaming this variable for clarity.",
            ]
        )
        self.assertEqual(_classify_review_text(review_text), "blocker")

    def test_accepts_finding_continuation_lines_when_indented(self):
        review_text = "\n".join(
            [
                "Findings:",
                "1. NIT: This can be simplified.",
                "   Consider using an early return for readability.",
            ]
        )
        self.assertEqual(_classify_review_text(review_text), "nits")

    def test_rejects_unstructured_prose_even_if_positive(self):
        review_text = "\n".join(
            [
                "Looks good to me overall.",
                "I don't see major issues.",
            ]
        )
        self.assertEqual(_classify_review_text(review_text), "malformed")

    def test_retries_once_when_review_is_malformed_then_accepts_valid_result(self):
        mock_gh = MagicMock()
        mock_gh.get_default_branch.return_value = "main"
        mock_gh.create_pull_request.return_value = {"html_url": "https://example/pr/1", "number": 1}
        mock_gh.post_issue_comment.return_value = {}

        with ExitStack() as stack:
            for p in self._base_patches(mock_gh):
                stack.enter_context(p)
            mock_codex = stack.enter_context(
                patch(
                    "velora.run.run_codex",
                    return_value=CmdResult(0, _codex_footer("velora/task123", "abc123"), ""),
                )
            )
            mock_review = stack.enter_context(
                patch(
                    "velora.run.run_gemini_review",
                    side_effect=[
                        CmdResult(0, "Needs more thought. OK.", ""),
                        CmdResult(0, "- NIT: Looks good overall.", ""),
                    ],
                )
            )
            result = run_task("darcuri/velora", "feature", RunSpec(task="task text"))

        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["review_result"], "nits")
        self.assertEqual(mock_review.call_count, 2)
        self.assertEqual(mock_codex.call_count, 1)

    def test_malformed_review_is_classified_separately_and_does_not_loop_as_blocker(self):
        mock_gh = MagicMock()
        mock_gh.get_default_branch.return_value = "main"
        mock_gh.create_pull_request.return_value = {"html_url": "https://example/pr/1", "number": 1}
        mock_gh.post_issue_comment.return_value = {}

        with ExitStack() as stack:
            for p in self._base_patches(mock_gh):
                stack.enter_context(p)
            mock_codex = stack.enter_context(
                patch(
                    "velora.run.run_codex",
                    return_value=CmdResult(0, _codex_footer("velora/task123", "abc123"), ""),
                )
            )
            mock_review = stack.enter_context(
                patch(
                    "velora.run.run_gemini_review",
                    side_effect=[
                        CmdResult(0, "This is malformed output", ""),
                        CmdResult(0, "Still malformed output", ""),
                    ],
                )
            )
            result = run_task("darcuri/velora", "feature", RunSpec(task="task text"))

        self.assertEqual(result["status"], "not-ready")
        self.assertEqual(result["review_result"], "malformed")
        self.assertEqual(mock_review.call_count, 2)
        self.assertEqual(mock_codex.call_count, 1)
        self.assertTrue(result["review"].startswith("REVIEW_MALFORMED:"))

    def test_real_blocker_still_triggers_fire_loop(self):
        mock_gh = MagicMock()
        mock_gh.get_default_branch.return_value = "main"
        mock_gh.create_pull_request.return_value = {"html_url": "https://example/pr/1", "number": 1}
        mock_gh.post_issue_comment.return_value = {}

        with ExitStack() as stack:
            for p in self._base_patches(mock_gh):
                stack.enter_context(p)
            mock_codex = stack.enter_context(
                patch(
                    "velora.run.run_codex",
                    side_effect=[
                        CmdResult(0, _codex_footer("velora/task123", "abc123"), ""),
                        CmdResult(0, _codex_footer("velora/task123", "def456"), ""),
                    ],
                )
            )
            mock_review = stack.enter_context(
                patch(
                    "velora.run.run_gemini_review",
                    side_effect=[
                        CmdResult(0, "- BLOCKER: The fix misses an edge case.", ""),
                        CmdResult(0, "OK", ""),
                    ],
                )
            )
            result = run_task("darcuri/velora", "feature", RunSpec(task="task text"))

        self.assertEqual(result["status"], "ready")
        self.assertEqual(mock_codex.call_count, 2)
        self.assertEqual(mock_review.call_count, 2)

    def test_debug_forensics_written_for_malformed_review_attempts(self):
        with tempfile.TemporaryDirectory() as td:
            with patch(
                "velora.run.run_gemini_review",
                side_effect=[
                    CmdResult(0, "This output is malformed", ""),
                    CmdResult(0, "Still malformed output", ""),
                ],
            ):
                result, review_text = _run_review_with_retry(
                    "diff-body\n+ token=abc123",
                    debug_task_dir=Path(td),
                )

            self.assertEqual(result, "malformed")
            self.assertTrue(review_text.startswith("REVIEW_MALFORMED:"))

            forensic_path = Path(td) / "review-forensics-try-1.json"
            self.assertTrue(forensic_path.exists())

            payload = json.loads(forensic_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["review_result"], "malformed")
            self.assertIn("Review the code diff for correctness/regressions.", payload["prompt_prefix"])
            self.assertEqual(payload["diff_chars"], len("diff-body\n+ token=abc123"))
            self.assertEqual(len(payload["diff_fingerprint_sha256"]), 64)
            self.assertIn("diff-body", payload["diff_preview"])
            self.assertIn("token=<redacted>", payload["diff_preview"])


if __name__ == "__main__":
    unittest.main()
