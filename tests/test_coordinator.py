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
        self.assertIn("MUST be EXACTLY one of: 50, 100, 200, 400", prompt)

    def test_render_can_embed_replay_memory(self) -> None:
        prompt = render_coordinator_prompt_v1(
            {
                "protocol_version": 1,
                "hello": "world",
                "policy": {"specialist_matrix": {"implementer": ["codex"], "docs": ["claude", "codex"]}},
            },
            replay_memory="# Coordinator Replay\n\nPrior run summary",
        )
        self.assertIn("### Replay context", prompt)
        self.assertIn("Prior run summary", prompt)
        self.assertIn("trust CoordinatorRequest", prompt)
        self.assertIn("### Allowed specialist matrix for this run", prompt)
        self.assertIn("- implementer: codex", prompt)

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

    def test_run_coordinator_accepts_fenced_json(self) -> None:
        valid = {
            "protocol_version": 1,
            "decision": "finalize_success",
            "reason": "done",
            "selected_specialist": {"role": "investigator", "runner": "claude"},
        }
        fenced = "```json\n" + __import__("json").dumps(valid) + "\n```"
        with patch("velora.coordinator.run_claude") as mocked:
            mocked.return_value = type(
                "R",
                (),
                {"returncode": 0, "stdout": fenced, "stderr": ""},
            )()
            resp = run_coordinator_v1(session_name="s", cwd=Path("."), request={"x": 1})
            self.assertEqual(resp.decision, "finalize_success")

    def test_can_run_coordinator_on_codex_runner(self) -> None:
        valid = {
            "protocol_version": 1,
            "decision": "finalize_success",
            "reason": "done",
            "selected_specialist": {"role": "investigator", "runner": "claude"},
        }
        with patch("velora.coordinator.run_codex") as mocked:
            mocked.return_value = type(
                "R",
                (),
                {"returncode": 0, "stdout": __import__("json").dumps(valid), "stderr": ""},
            )()
            resp = run_coordinator_v1(session_name="s", cwd=Path("."), request={"x": 1}, runner="codex")
            self.assertEqual(resp.decision, "finalize_success")


if __name__ == "__main__":
    unittest.main()
