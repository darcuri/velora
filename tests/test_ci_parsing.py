import unittest

from velora.run import _build_ci_logs_excerpt, _parse_failing_check_runs_payload


class TestCiParsing(unittest.TestCase):
    def test_parse_multiple_failing_checks_and_signature_order_invariant(self):
        payload_a = {
            "check_runs": [
                {"name": "lint", "status": "completed", "conclusion": "failure", "details_url": "https://ci/lint"},
                {"name": "unit", "status": "completed", "conclusion": "timed_out", "html_url": "https://ci/unit"},
            ]
        }
        payload_b = {"check_runs": list(reversed(payload_a["check_runs"]))}

        checks_a, sig_a = _parse_failing_check_runs_payload(payload_a)
        checks_b, sig_b = _parse_failing_check_runs_payload(payload_b)

        self.assertEqual(len(checks_a), 2)
        self.assertEqual({c["name"] for c in checks_a}, {"lint", "unit"})
        self.assertTrue(sig_a.startswith("checks-2-"))
        self.assertEqual(sig_a, sig_b)
        self.assertEqual([c["url"] for c in checks_a], ["https://ci/lint", "https://ci/unit"])

    def test_parse_mixed_statuses_ignores_non_failing_and_handles_missing_fields(self):
        payload = {
            "check_runs": [
                {"name": "build", "status": "completed", "conclusion": "success"},
                {"name": "test", "status": "in_progress", "conclusion": None},
                {"status": "completed", "conclusion": "failure", "output": {"title": "Bad", "summary": "Oops"}},
            ]
        }
        checks, sig = _parse_failing_check_runs_payload(payload)
        self.assertEqual(len(checks), 1)
        self.assertEqual(checks[0]["name"], "unnamed-check")
        self.assertEqual(checks[0]["url"], "")
        self.assertIn("status=completed", checks[0]["summary"])
        self.assertIn("conclusion=failure", checks[0]["summary"])
        self.assertIn("title=Bad", checks[0]["summary"])
        self.assertIn("summary=Oops", checks[0]["summary"])
        self.assertTrue(sig.startswith("checks-1-"))

    def test_logs_excerpt_truncates_safely(self):
        checks = [
            {
                "name": "very-long-check-name-" + ("x" * 80),
                "kind": "ci",
                "url": "https://ci/example",
                "summary": "status=completed; conclusion=failure; summary=" + ("y" * 300),
            },
            {"name": "lint", "kind": "ci", "url": "", "summary": "status=completed; conclusion=failure"},
            {"name": "unit", "kind": "ci", "url": "", "summary": "status=completed; conclusion=timed_out"},
            {"name": "e2e", "kind": "ci", "url": "", "summary": "status=completed; conclusion=cancelled"},
        ]
        excerpt = _build_ci_logs_excerpt(checks, max_checks=3, max_chars=220)
        self.assertLessEqual(len(excerpt), 220)
        self.assertIn("(+1 more failing checks)", excerpt)
        self.assertTrue(excerpt.endswith("...") or len(excerpt) < 220)


if __name__ == "__main__":
    unittest.main()
