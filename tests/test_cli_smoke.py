import unittest
import json
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from contextlib import redirect_stdout
from unittest.mock import patch

from velora.audit import AuditEvent, append_event
from velora.cli import main


class TestCliSmoke(unittest.TestCase):
    def _write_audit_fixture(self, tmp: str, run_id: str, with_review_events: bool) -> None:
        append_event(
            run_id,
            AuditEvent(
                run_id=run_id,
                iteration=0,
                event_type="run_start",
                timestamp="2026-03-09T00:00:00+00:00",
                payload={"objective_snippet": "ship audit inspect" * 10},
            ),
            base_dir=Path(tmp),
        )
        append_event(
            run_id,
            AuditEvent(
                run_id=run_id,
                iteration=1,
                event_type="decision_made",
                timestamp="2026-03-09T00:00:01+00:00",
                payload={"decision": "execute_work_item", "reason": "implement"},
            ),
            base_dir=Path(tmp),
        )
        if with_review_events:
            append_event(
                run_id,
                AuditEvent(
                    run_id=run_id,
                    iteration=1,
                    event_type="review_started",
                    timestamp="2026-03-09T00:00:01+00:00",
                    payload={},
                ),
                base_dir=Path(tmp),
            )
            append_event(
                run_id,
                AuditEvent(
                    run_id=run_id,
                    iteration=1,
                    event_type="review_completed",
                    timestamp="2026-03-09T00:00:02+00:00",
                    payload={"outcome": "repair", "summary": "Needs one follow-up"},
                ),
                base_dir=Path(tmp),
            )
        append_event(
            run_id,
            AuditEvent(
                run_id=run_id,
                iteration=1,
                event_type="run_end",
                timestamp="2026-03-09T00:00:03+00:00",
                payload={"status": "ready"},
            ),
            base_dir=Path(tmp),
        )

    def test_status(self):
        self.assertEqual(main(["status"]), 0)

    def test_audit_inspect_prints_summary_for_fixture_run(self):
        with TemporaryDirectory() as tmp:
            run_id = "run-001"
            self._write_audit_fixture(tmp, run_id, with_review_events=True)

            with (
                redirect_stdout(StringIO()) as out,
                patch("velora.cli.Path.cwd", return_value=Path(tmp)),
            ):
                rc = main(["audit", "inspect", "--run", run_id])

            self.assertEqual(rc, 0)
            text = out.getvalue()
            self.assertIn("run_id: run-001", text)
            self.assertIn("final_status: ready", text)
            self.assertIn("review_events:", text)
            self.assertIn("outcome=repair", text)

            with (
                redirect_stdout(StringIO()) as out_latest,
                patch("velora.cli.Path.cwd", return_value=Path(tmp)),
            ):
                rc_latest = main(["audit", "inspect"])

            self.assertEqual(rc_latest, 0)
            self.assertIn("run_id: run-001", out_latest.getvalue())

    def test_audit_inspect_json_output_modes(self):
        with TemporaryDirectory() as tmp:
            run_with_review = "run-001"
            run_without_review = "run-002"
            self._write_audit_fixture(tmp, run_with_review, with_review_events=True)
            self._write_audit_fixture(tmp, run_without_review, with_review_events=False)

            with (
                redirect_stdout(StringIO()) as out_with_review,
                patch("velora.cli.Path.cwd", return_value=Path(tmp)),
            ):
                rc_with_review = main(["audit", "inspect", "--run", run_with_review, "--json"])

            self.assertEqual(rc_with_review, 0)
            payload_with_review = json.loads(out_with_review.getvalue())
            self.assertEqual(payload_with_review["run_id"], run_with_review)
            self.assertEqual(
                set(payload_with_review),
                {"run_id", "objective", "iterations", "decisions", "final_status", "event_count", "review_events"},
            )
            self.assertEqual(payload_with_review["iterations"], 1)
            self.assertIsInstance(payload_with_review["decisions"], list)
            self.assertEqual(payload_with_review["final_status"], "ready")
            self.assertIsInstance(payload_with_review["review_events"], list)
            self.assertLessEqual(len(payload_with_review["objective"]), 120)

            with (
                redirect_stdout(StringIO()) as out_without_review,
                patch("velora.cli.Path.cwd", return_value=Path(tmp)),
            ):
                rc_without_review = main(["audit", "inspect", "--run", run_without_review, "--json"])

            self.assertEqual(rc_without_review, 0)
            payload_without_review = json.loads(out_without_review.getvalue())
            self.assertEqual(
                set(payload_without_review),
                {"run_id", "objective", "iterations", "decisions", "final_status", "event_count"},
            )
            self.assertNotIn("review_events", payload_without_review)


if __name__ == "__main__":
    unittest.main()
