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

    def test_render_with_brief_uses_compact_request_and_excludes_history_state(self) -> None:
        request = {
            "protocol_version": 1,
            "run_id": "task123",
            "iteration": 4,
            "objective": "Tighten Mode A context hygiene",
            "repo": {"owner": "octocat", "name": "velora"},
            "policy": {"specialist_matrix": {"implementer": ["codex"]}},
            "evaluation": {"status": "fail", "outcome": "ci_failure"},
            "history": {"work_items_executed": [{"id": f"WI-{i:04d}"} for i in range(20)]},
            "state": {"latest_worker_result": {"summary": "very long state blob" * 20}},
        }
        brief = {"run_id": "task123", "status": {"state": "running"}, "open_loops": ["Fix CI"]}

        full_prompt = render_coordinator_prompt_v1(request)
        compact_prompt = render_coordinator_prompt_v1(request, brief=brief)

        self.assertIn("### Coordinator brief", compact_prompt)
        self.assertIn("\"open_loops\": [", compact_prompt)
        self.assertNotIn("work_items_executed", compact_prompt)
        self.assertNotIn("latest_worker_result", compact_prompt)
        self.assertLess(len(compact_prompt), len(full_prompt))

    def test_render_includes_no_progress_self_audit_only_when_streak_positive(self) -> None:
        with_audit = render_coordinator_prompt_v1(
            {
                "protocol_version": 1,
                "history": {"no_progress_streak": 2},
            }
        )
        without_audit = render_coordinator_prompt_v1(
            {
                "protocol_version": 1,
                "history": {"no_progress_streak": 0},
            }
        )

        self.assertIn("### No-progress self-audit", with_audit)
        self.assertIn("no_progress_streak=2", with_audit)
        self.assertIn("what you are changing in the reason field", with_audit)
        self.assertNotIn("### No-progress self-audit", without_audit)

    def test_prompt_contains_request_review_schema(self) -> None:
        prompt = render_coordinator_prompt_v1({"protocol_version": 1})
        self.assertIn("request_review", prompt)
        self.assertIn("review_brief", prompt)
        self.assertIn('"reviewer": "gemini" | "claude"', prompt)
        self.assertIn("acceptance_criteria", prompt)
        self.assertIn("REQUIRED only when decision=request_review", prompt)

    def test_prompt_contains_dismiss_finding_schema(self) -> None:
        prompt = render_coordinator_prompt_v1({"protocol_version": 1})
        self.assertIn("dismiss_finding", prompt)
        self.assertIn("finding_dismissal", prompt)
        self.assertIn("finding_ids", prompt)
        self.assertIn("justification", prompt)
        self.assertIn("REQUIRED only when decision=dismiss_finding", prompt)

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
