import unittest

from velora.local_worker import (
    HarnessReason,
    HarnessOutcome,
    assemble_work_result,
)
from velora.protocol import validate_work_result


class TestHarnessOutcome(unittest.TestCase):
    def test_success_outcome(self):
        outcome = HarnessOutcome(success=True, reason=HarnessReason.SUCCESS, evidence=[])
        self.assertTrue(outcome.success)
        self.assertEqual(outcome.reason, HarnessReason.SUCCESS)

    def test_failure_outcome(self):
        outcome = HarnessOutcome(
            success=False,
            reason=HarnessReason.TESTS_EXHAUSTED,
            evidence=["FAILED tests/test_foo.py::test_bar"],
        )
        self.assertFalse(outcome.success)
        self.assertEqual(outcome.reason, HarnessReason.TESTS_EXHAUSTED)


class TestAssembleWorkResult(unittest.TestCase):
    def test_success_produces_valid_completed_work_result(self):
        outcome = HarnessOutcome(success=True, reason=HarnessReason.SUCCESS, evidence=["all tests passed"])
        wr = assemble_work_result(
            outcome=outcome,
            work_item_id="WI-001",
            summary="Added feature X",
            branch="velora/wi-001",
            head_sha="abc123def456",
            files_touched=["src/main.py"],
            tests_run=[{"command": "python -m pytest -q", "status": "pass", "details": "1 passed"}],
        )
        # Must survive protocol validation
        validated = validate_work_result(wr)
        self.assertEqual(validated.status, "completed")
        self.assertEqual(validated.branch, "velora/wi-001")
        self.assertEqual(validated.head_sha, "abc123def456")
        self.assertEqual(validated.blockers, [])

    def test_blocked_produces_valid_blocked_work_result(self):
        outcome = HarnessOutcome(
            success=False,
            reason=HarnessReason.SCOPE_INSUFFICIENT,
            evidence=["need access to velora/config.py"],
        )
        wr = assemble_work_result(
            outcome=outcome,
            work_item_id="WI-001",
            summary="Could not complete",
            branch="",
            head_sha="",
            files_touched=[],
            tests_run=[],
        )
        validated = validate_work_result(wr)
        self.assertEqual(validated.status, "blocked")
        self.assertEqual(validated.blockers[0], "SCOPE_INSUFFICIENT")

    def test_failed_produces_valid_failed_work_result(self):
        outcome = HarnessOutcome(
            success=False,
            reason=HarnessReason.TESTS_EXHAUSTED,
            evidence=["FAILED test_foo.py"],
        )
        wr = assemble_work_result(
            outcome=outcome,
            work_item_id="WI-001",
            summary="Tests kept failing",
            branch="",
            head_sha="",
            files_touched=["src/main.py"],
            tests_run=[{"command": "python -m pytest -q", "status": "fail", "details": "1 failed"}],
        )
        validated = validate_work_result(wr)
        self.assertEqual(validated.status, "failed")
        self.assertIn("TESTS_EXHAUSTED", validated.blockers)


if __name__ == "__main__":
    unittest.main()
