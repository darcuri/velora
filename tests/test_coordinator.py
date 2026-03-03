import unittest
from pathlib import Path
from unittest.mock import patch

from velora.coordinator import render_coordinator_prompt_v1, run_coordinator_v1
from velora.protocol import ProtocolError


class TestCoordinator(unittest.TestCase):
    def test_render_includes_request_json(self) -> None:
        prompt = render_coordinator_prompt_v1({"protocol_version": 1, "hello": "world"})
        self.assertIn("CoordinatorRequest", prompt)
        self.assertIn('"hello": "world"', prompt)

    def test_run_coordinator_rejects_non_json_output(self) -> None:
        with patch("velora.coordinator.run_claude") as mocked:
            mocked.return_value = type(
                "R",
                (),
                {"returncode": 0, "stdout": "not json", "stderr": ""},
            )()
            with self.assertRaises(ProtocolError):
                run_coordinator_v1(session_name="s", cwd=Path("."), request={"x": 1})

    def test_run_coordinator_validates_protocol(self) -> None:
        valid = {
            "protocol_version": 1,
            "decision": "finalize_success",
            "reason": "done",
            "selected_specialist": {"role": "investigator", "runner": "claude"},
        }
        with patch("velora.coordinator.run_claude") as mocked:
            mocked.return_value = type(
                "R",
                (),
                {"returncode": 0, "stdout": __import__("json").dumps(valid), "stderr": ""},
            )()
            resp = run_coordinator_v1(session_name="s", cwd=Path("."), request={"x": 1})
            self.assertEqual(resp.decision, "finalize_success")


if __name__ == "__main__":
    unittest.main()
