import unittest

from velora.local_worker import (
    HarnessReason,
    HarnessOutcome,
    assemble_work_result,
    build_local_worker_prompt,
)
from velora.protocol import validate_work_result, WorkItem


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


def _make_work_item() -> WorkItem:
    return WorkItem.from_dict({
        "id": "WI-001",
        "kind": "implement",
        "rationale": "Add the foo feature",
        "instructions": ["Create foo.py", "Add a foo() function that returns 42"],
        "scope_hints": {
            "likely_files": ["src/foo.py", "tests/test_foo.py"],
            "search_terms": ["foo"],
        },
        "acceptance": {
            "must": ["foo() returns 42"],
            "must_not": ["Do not modify existing files"],
            "gates": ["tests"],
        },
        "limits": {"max_diff_lines": 100, "max_commits": 1},
        "commit": {
            "message": "feat: add foo",
            "footer": {
                "VELORA_RUN_ID": "run-001",
                "VELORA_ITERATION": 1,
                "WORK_ITEM_ID": "WI-001",
            },
        },
    })


class TestBuildPrompt(unittest.TestCase):
    def test_prompt_contains_task_details(self):
        wi = _make_work_item()
        prompt = build_local_worker_prompt(
            work_item=wi,
            repo_ref="owner/repo",
            work_branch="velora/wi-001",
            test_commands=["python -m pytest -q"],
        )
        self.assertIn("owner/repo", prompt)
        self.assertIn("velora/wi-001", prompt)
        self.assertIn("WI-001", prompt)
        self.assertIn("Add the foo feature", prompt)
        self.assertIn("src/foo.py", prompt)
        self.assertIn("python -m pytest -q", prompt)

    def test_prompt_contains_propulsion_language(self):
        wi = _make_work_item()
        prompt = build_local_worker_prompt(
            work_item=wi,
            repo_ref="owner/repo",
            work_branch="velora/wi-001",
            test_commands=["python -m pytest -q"],
        )
        self.assertIn("Do not ask questions", prompt)
        self.assertIn("JSON only", prompt)

    def test_prompt_lists_all_actions(self):
        wi = _make_work_item()
        prompt = build_local_worker_prompt(
            work_item=wi,
            repo_ref="owner/repo",
            work_branch="velora/wi-001",
            test_commands=[],
        )
        for action in ["read_file", "list_files", "write_file", "patch_file",
                        "search_files", "run_tests", "work_complete", "work_blocked"]:
            self.assertIn(action, prompt)


if __name__ == "__main__":
    unittest.main()
