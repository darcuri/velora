import unittest

from velora.config import _parse_specialist_matrix
from velora.protocol import ProtocolError, enforce_specialist_matrix, validate_coordinator_response


DEFAULT_MATRIX = {
    "implementer": {"runners": ["codex"], "models": []},
    "docs": {"runners": ["codex", "claude"], "models": []},
    "refactor": {"runners": ["codex"], "models": []},
    "investigator": {"runners": ["codex", "claude"], "models": []},
}


class TestSpecialistMatrixParsing(unittest.TestCase):
    def test_partial_override_merges_with_defaults(self):
        merged = _parse_specialist_matrix({"implementer": {"runners": ["claude"], "models": []}}, DEFAULT_MATRIX)
        self.assertIn("docs", merged)
        self.assertEqual(merged["implementer"]["runners"], ["claude"])
        self.assertEqual(merged["refactor"]["runners"], ["codex"])


class TestSpecialistMatrixEnforcement(unittest.TestCase):
    def _resp(self, role: str, runner: str, model=None):
        payload = {
            "protocol_version": 1,
            "decision": "finalize_success",
            "reason": "done",
            "selected_specialist": {"role": role, "runner": runner},
        }
        if model is not None:
            payload["selected_specialist"]["model"] = model
        return validate_coordinator_response(payload)

    def test_runner_out_of_bounds_hard_fails(self):
        resp = self._resp("implementer", "claude")
        with self.assertRaises(ProtocolError):
            enforce_specialist_matrix(resp, DEFAULT_MATRIX)

    def test_runner_in_bounds_ok(self):
        resp = self._resp("implementer", "codex")
        enforce_specialist_matrix(resp, DEFAULT_MATRIX)

    def test_model_override_disallowed_by_default(self):
        resp = self._resp("implementer", "codex", model="some-model")
        with self.assertRaises(ProtocolError):
            enforce_specialist_matrix(resp, DEFAULT_MATRIX)


if __name__ == "__main__":
    unittest.main()
