import unittest

from velora.protocol import ProtocolError, validate_work_result


def _valid_completed_payload() -> dict:
    return {
        "protocol_version": 1,
        "work_item_id": "WI-0001",
        "status": "completed",
        "summary": "Implemented fix and verified behavior.",
        "branch": "velora/wi-0001-fix",
        "head_sha": "abc123def456",
        "files_touched": ["velora/protocol.py", "tests/test_work_result_protocol.py"],
        "tests_run": [
            {
                "command": "pytest tests/test_work_result_protocol.py",
                "status": "pass",
                "details": "1 passed",
            }
        ],
        "blockers": [],
        "follow_up": ["Run full test suite before merge."],
        "evidence": ["Added strict schema validation for worker output."],
    }


class TestWorkResultProtocol(unittest.TestCase):
    def test_completed_work_result_valid(self) -> None:
        result = validate_work_result(_valid_completed_payload())
        self.assertEqual(result.protocol_version, 1)
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.branch, "velora/wi-0001-fix")
        self.assertEqual(len(result.tests_run), 1)
        self.assertEqual(result.tests_run[0].status, "pass")

    def test_blocked_requires_empty_branch_and_sha_and_non_empty_blockers(self) -> None:
        payload = _valid_completed_payload()
        payload["status"] = "blocked"
        payload["branch"] = ""
        payload["head_sha"] = ""
        payload["blockers"] = ["Missing required API token."]
        result = validate_work_result(payload)
        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.branch, "")
        self.assertEqual(result.head_sha, "")
        self.assertEqual(result.blockers, ["Missing required API token."])

    def test_unknown_keys_are_rejected(self) -> None:
        payload = _valid_completed_payload()
        payload["extra"] = True
        with self.assertRaises(ProtocolError):
            validate_work_result(payload)

    def test_missing_required_field_is_rejected(self) -> None:
        payload = _valid_completed_payload()
        del payload["tests_run"]
        with self.assertRaises(ProtocolError):
            validate_work_result(payload)

    def test_completed_requires_non_empty_branch_and_head_sha(self) -> None:
        payload = _valid_completed_payload()
        payload["branch"] = ""
        with self.assertRaises(ProtocolError):
            validate_work_result(payload)

        payload = _valid_completed_payload()
        payload["head_sha"] = ""
        with self.assertRaises(ProtocolError):
            validate_work_result(payload)

    def test_completed_requires_empty_blockers(self) -> None:
        payload = _valid_completed_payload()
        payload["blockers"] = ["Something is blocked."]
        with self.assertRaises(ProtocolError):
            validate_work_result(payload)

    def test_failed_requires_non_empty_blockers(self) -> None:
        payload = _valid_completed_payload()
        payload["status"] = "failed"
        payload["branch"] = ""
        payload["head_sha"] = ""
        payload["blockers"] = []
        with self.assertRaises(ProtocolError):
            validate_work_result(payload)

    def test_failed_requires_empty_branch_and_head_sha(self) -> None:
        payload = _valid_completed_payload()
        payload["status"] = "failed"
        payload["branch"] = "feature/not-allowed"
        payload["head_sha"] = ""
        payload["blockers"] = ["Compiler failure in upstream dependency."]
        with self.assertRaises(ProtocolError):
            validate_work_result(payload)

        payload = _valid_completed_payload()
        payload["status"] = "failed"
        payload["branch"] = ""
        payload["head_sha"] = "abc123"
        payload["blockers"] = ["Compiler failure in upstream dependency."]
        with self.assertRaises(ProtocolError):
            validate_work_result(payload)

    def test_tests_run_entry_validation(self) -> None:
        payload = _valid_completed_payload()
        payload["tests_run"] = [{"command": "pytest", "status": "unknown", "details": ""}]
        with self.assertRaises(ProtocolError):
            validate_work_result(payload)

        payload = _valid_completed_payload()
        payload["tests_run"] = [{"command": "pytest", "status": "pass", "details": "", "extra": "x"}]
        with self.assertRaises(ProtocolError):
            validate_work_result(payload)

    def test_list_entries_must_be_non_empty_strings(self) -> None:
        payload = _valid_completed_payload()
        payload["files_touched"] = [""]
        with self.assertRaises(ProtocolError):
            validate_work_result(payload)

        payload = _valid_completed_payload()
        payload["follow_up"] = [" "]
        with self.assertRaises(ProtocolError):
            validate_work_result(payload)

        payload = _valid_completed_payload()
        payload["evidence"] = [""]
        with self.assertRaises(ProtocolError):
            validate_work_result(payload)


if __name__ == "__main__":
    unittest.main()
