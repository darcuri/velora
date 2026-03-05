import unittest
from unittest.mock import patch

from velora.github import GitHubClient
from velora.run import _classify_ci_failure


class TestCiState(unittest.TestCase):
    def test_success_when_check_runs_success_even_if_combined_pending(self):
        gh = GitHubClient("token")
        with patch.object(gh, "get_combined_status", return_value={"state": "pending"}), patch.object(
            gh,
            "get_check_runs",
            return_value={
                "check_runs": [
                    {"name": "test", "status": "completed", "conclusion": "success"},
                    {"name": "lint", "status": "completed", "conclusion": "success"},
                ]
            },
        ):
            state, detail = gh.get_ci_state("o", "r", "sha")
        self.assertEqual(state, "success")
        self.assertIn("check-runs-success", detail)

    def test_classifier_queued_never_started_is_infra(self):
        out = _classify_ci_failure(
            "failure",
            "stuck-no-progress",
            {"check_runs": [{"status": "queued", "conclusion": None, "started_at": None, "completed_at": None}]},
        )
        self.assertEqual(out["classification"], "infra_outage")
        self.assertIn("queued_never_started", out["reason_codes"])
        self.assertIn("poll_stuck_no_progress", out["reason_codes"])

    def test_classifier_zero_runtime_cancelled_timed_out_is_infra(self):
        out = _classify_ci_failure(
            "failure",
            "check-runs",
            {
                "check_runs": [
                    {"status": "completed", "conclusion": "cancelled", "started_at": "2026-03-01T00:00:00Z", "completed_at": "2026-03-01T00:00:01Z"},
                    {"status": "completed", "conclusion": "timed_out", "started_at": "2026-03-01T00:01:00Z", "completed_at": "2026-03-01T00:01:02Z"},
                ]
            },
        )
        self.assertEqual(out["classification"], "infra_outage")
        self.assertIn("infra_like_conclusions", out["reason_codes"])

    def test_classifier_real_failure_with_output_is_code_failure(self):
        out = _classify_ci_failure(
            "failure",
            "check-runs",
            {"check_runs": [{"status": "completed", "conclusion": "failure", "output": {"title": "pytest", "summary": "2 failed"}}]},
        )
        self.assertEqual(out["classification"], "code_failure")
        self.assertIn("explicit_failure_conclusion", out["reason_codes"])
        self.assertIn("failure_output_present", out["reason_codes"])

    def test_classifier_ambiguous_is_unknown(self):
        out = _classify_ci_failure("failure", "combined-status=failure", {"check_runs": []})
        self.assertEqual(out["classification"], "unknown")


if __name__ == "__main__":
    unittest.main()
