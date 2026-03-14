import unittest
import warnings

from velora.protocol import ProtocolError, validate_coordinator_response


def _valid_execute_payload() -> dict:
    return {
        "protocol_version": 1,
        "decision": "execute_work_item",
        "reason": "Fix failing tests.",
        "selected_specialist": {
            "role": "implementer",
            "runner": "codex",
            "model": "gpt-5.2",
        },
        "work_item": {
            "id": "WI-0001",
            "kind": "repair",
            "rationale": "Repair the two failing tests with minimal diff.",
            "instructions": [
                "Run the failing tests and identify the root cause.",
                "Apply the smallest fix that makes tests pass.",
            ],
            "scope_hints": {
                "likely_files": ["velora/util.py"],
                "search_terms": ["footer", "regex"],
            },
            "acceptance": {
                "must": ["All unit tests pass"],
                "must_not": ["Introduce new dependencies"],
                "gates": ["tests", "security"],
            },
            "limits": {"max_diff_lines": 100, "max_commits": 1},
            "commit": {
                "message": "Repair footer parsing edge cases",
                "footer": {
                    "VELORA_RUN_ID": "run-123",
                    "VELORA_ITERATION": 1,
                    "WORK_ITEM_ID": "WI-0001",
                },
            },
        },
    }


class TestProtocol(unittest.TestCase):
    def test_execute_work_item_valid(self) -> None:
        resp = validate_coordinator_response(_valid_execute_payload())
        self.assertEqual(resp.protocol_version, 1)
        self.assertEqual(resp.decision, "execute_work_item")
        self.assertEqual(resp.selected_specialist.runner, "codex")
        self.assertIsNotNone(resp.work_item)
        assert resp.work_item is not None
        self.assertEqual(resp.work_item.id, "WI-0001")

    def test_finalize_success_requires_specialist_and_omits_work_item(self) -> None:
        payload = {
            "protocol_version": 1,
            "decision": "finalize_success",
            "reason": "Objective satisfied and gates green.",
            "selected_specialist": {"role": "investigator", "runner": "claude"},
        }
        resp = validate_coordinator_response(payload)
        self.assertEqual(resp.decision, "finalize_success")
        self.assertIsNone(resp.work_item)

    def test_stop_failure_requires_specialist_and_omits_work_item(self) -> None:
        payload = {
            "protocol_version": 1,
            "decision": "stop_failure",
            "reason": "Missing auth.",
            "selected_specialist": {"role": "investigator", "runner": "claude"},
        }
        resp = validate_coordinator_response(payload)
        self.assertEqual(resp.decision, "stop_failure")
        self.assertIsNone(resp.work_item)

    def test_missing_selected_specialist_is_protocol_error(self) -> None:
        payload = {
            "protocol_version": 1,
            "decision": "finalize_success",
            "reason": "done",
        }
        with self.assertRaises(ProtocolError):
            validate_coordinator_response(payload)

    def test_finalize_with_work_item_is_protocol_error(self) -> None:
        payload = _valid_execute_payload()
        payload["decision"] = "finalize_success"
        with self.assertRaises(ProtocolError):
            validate_coordinator_response(payload)

    def test_gemini_runner_is_rejected(self) -> None:
        payload = _valid_execute_payload()
        payload["selected_specialist"]["runner"] = "gemini"
        with self.assertRaises(ProtocolError):
            validate_coordinator_response(payload)

    def test_unknown_keys_are_rejected(self) -> None:
        payload = _valid_execute_payload()
        payload["extra"] = 123
        with self.assertRaises(ProtocolError):
            validate_coordinator_response(payload)

    def test_commit_font_typo_normalizes_to_footer_and_warns(self) -> None:
        payload = _valid_execute_payload()
        commit = payload["work_item"]["commit"]
        commit["font"] = commit.pop("footer")

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            resp = validate_coordinator_response(payload)

        assert resp.work_item is not None
        self.assertEqual(resp.work_item.commit.footer["WORK_ITEM_ID"], "WI-0001")
        self.assertTrue(any("normalized" in str(w.message) for w in caught))

    def test_commit_unknown_key_still_rejected(self) -> None:
        payload = _valid_execute_payload()
        payload["work_item"]["commit"]["foobar"] = "x"

        with self.assertRaises(ProtocolError):
            validate_coordinator_response(payload)


if __name__ == "__main__":
    unittest.main()
