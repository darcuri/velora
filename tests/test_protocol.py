import unittest

from velora.protocol import (
    ProtocolError,
    validate_coordinator_response,
    validate_review_brief,
    validate_review_result,
)


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


def _valid_review_brief_payload() -> dict:
    return {
        "id": "RB-0001",
        "reviewer": "gemini",
        "model": None,
        "objective": "Verify correctness of footer parsing changes",
        "acceptance_criteria": ["All tests pass", "No regressions"],
        "rejection_criteria": ["New security issues"],
        "areas_of_concern": ["Error handling"],
        "scope": {
            "kind": "full_diff",
            "base_ref": "main",
            "head_sha": "abc123",
            "files": [],
        },
    }


class TestReviewBriefProtocol(unittest.TestCase):
    def test_valid_brief(self) -> None:
        brief = validate_review_brief(_valid_review_brief_payload())
        self.assertEqual(brief.id, "RB-0001")
        self.assertEqual(brief.reviewer, "gemini")
        self.assertIsNone(brief.model)
        self.assertEqual(brief.objective, "Verify correctness of footer parsing changes")
        self.assertEqual(brief.scope.kind, "full_diff")
        self.assertEqual(brief.scope.base_ref, "main")
        self.assertEqual(brief.scope.head_sha, "abc123")
        self.assertEqual(brief.scope.files, [])

    def test_model_override(self) -> None:
        payload = _valid_review_brief_payload()
        payload["model"] = "gemini-2.5-pro"
        brief = validate_review_brief(payload)
        self.assertEqual(brief.model, "gemini-2.5-pro")

    def test_files_scope(self) -> None:
        payload = _valid_review_brief_payload()
        payload["scope"]["kind"] = "files"
        payload["scope"]["files"] = ["velora/protocol.py", "tests/test_protocol.py"]
        brief = validate_review_brief(payload)
        self.assertEqual(brief.scope.kind, "files")
        self.assertEqual(brief.scope.files, ["velora/protocol.py", "tests/test_protocol.py"])

    def test_invalid_reviewer(self) -> None:
        payload = _valid_review_brief_payload()
        payload["reviewer"] = "codex"
        with self.assertRaises(ProtocolError):
            validate_review_brief(payload)

    def test_invalid_scope_kind(self) -> None:
        payload = _valid_review_brief_payload()
        payload["scope"]["kind"] = "partial"
        with self.assertRaises(ProtocolError):
            validate_review_brief(payload)

    def test_missing_objective(self) -> None:
        payload = _valid_review_brief_payload()
        del payload["objective"]
        with self.assertRaises(ProtocolError):
            validate_review_brief(payload)

    def test_unknown_keys_on_brief(self) -> None:
        payload = _valid_review_brief_payload()
        payload["extra"] = "nope"
        with self.assertRaises(ProtocolError):
            validate_review_brief(payload)

    def test_unknown_keys_on_scope(self) -> None:
        payload = _valid_review_brief_payload()
        payload["scope"]["extra"] = "nope"
        with self.assertRaises(ProtocolError):
            validate_review_brief(payload)

    def test_empty_acceptance_criteria_allowed(self) -> None:
        payload = _valid_review_brief_payload()
        payload["acceptance_criteria"] = []
        brief = validate_review_brief(payload)
        self.assertEqual(brief.acceptance_criteria, [])


def _valid_review_finding(*, severity: str = "blocker", **overrides: object) -> dict:
    base: dict = {
        "id": "RF-001",
        "severity": severity,
        "category": "correctness",
        "location": "velora/protocol.py:42",
        "description": "Missing validation for edge case",
        "criterion_id": 0,
    }
    base.update(overrides)
    return base


def _valid_review_result_payload(*, verdict: str = "reject", findings: list[dict] | None = None) -> dict:
    if findings is None:
        if verdict == "reject":
            findings = [_valid_review_finding(severity="blocker")]
        else:
            findings = []
    return {
        "review_brief_id": "RB-0001",
        "verdict": verdict,
        "findings": findings,
        "summary": "Review complete.",
    }


class TestReviewResultProtocol(unittest.TestCase):
    def test_valid_reject_result(self) -> None:
        result = validate_review_result(_valid_review_result_payload(verdict="reject"))
        self.assertEqual(result.review_brief_id, "RB-0001")
        self.assertEqual(result.verdict, "reject")
        self.assertEqual(len(result.findings), 1)
        self.assertEqual(result.findings[0].severity, "blocker")
        self.assertEqual(result.summary, "Review complete.")

    def test_valid_approve_result(self) -> None:
        result = validate_review_result(_valid_review_result_payload(verdict="approve", findings=[]))
        self.assertEqual(result.verdict, "approve")
        self.assertEqual(result.findings, [])

    def test_approve_with_nits_allowed(self) -> None:
        nit = _valid_review_finding(severity="nit")
        result = validate_review_result(_valid_review_result_payload(verdict="approve", findings=[nit]))
        self.assertEqual(result.verdict, "approve")
        self.assertEqual(len(result.findings), 1)
        self.assertEqual(result.findings[0].severity, "nit")

    def test_approve_with_blocker_is_error(self) -> None:
        blocker = _valid_review_finding(severity="blocker")
        with self.assertRaises(ProtocolError):
            validate_review_result(_valid_review_result_payload(verdict="approve", findings=[blocker]))

    def test_reject_without_blocker_is_error(self) -> None:
        nit = _valid_review_finding(severity="nit")
        with self.assertRaises(ProtocolError):
            validate_review_result(_valid_review_result_payload(verdict="reject", findings=[nit]))

    def test_invalid_verdict(self) -> None:
        payload = _valid_review_result_payload()
        payload["verdict"] = "maybe"
        with self.assertRaises(ProtocolError):
            validate_review_result(payload)

    def test_invalid_severity(self) -> None:
        bad_finding = _valid_review_finding(severity="critical")
        bad_finding["severity"] = "critical"
        with self.assertRaises(ProtocolError):
            validate_review_result(_valid_review_result_payload(verdict="reject", findings=[bad_finding]))

    def test_invalid_category(self) -> None:
        bad_finding = _valid_review_finding()
        bad_finding["category"] = "performance"
        with self.assertRaises(ProtocolError):
            validate_review_result(_valid_review_result_payload(verdict="reject", findings=[bad_finding]))

    def test_unknown_keys_in_finding(self) -> None:
        bad_finding = _valid_review_finding()
        bad_finding["extra"] = "nope"
        with self.assertRaises(ProtocolError):
            validate_review_result(_valid_review_result_payload(verdict="reject", findings=[bad_finding]))

    def test_unknown_keys_in_result(self) -> None:
        payload = _valid_review_result_payload()
        payload["extra"] = "nope"
        with self.assertRaises(ProtocolError):
            validate_review_result(payload)

    def test_finding_with_no_criterion_id(self) -> None:
        finding = _valid_review_finding()
        finding["criterion_id"] = None
        result = validate_review_result(_valid_review_result_payload(verdict="reject", findings=[finding]))
        self.assertIsNone(result.findings[0].criterion_id)

    def test_finding_with_empty_location(self) -> None:
        finding = _valid_review_finding()
        finding["location"] = ""
        result = validate_review_result(_valid_review_result_payload(verdict="reject", findings=[finding]))
        self.assertEqual(result.findings[0].location, "")


if __name__ == "__main__":
    unittest.main()
