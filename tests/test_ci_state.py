import unittest
from unittest.mock import patch

from velora.github import GitHubClient


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


if __name__ == "__main__":
    unittest.main()
