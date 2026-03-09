import unittest
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from contextlib import redirect_stdout

from velora.audit import AuditEvent, append_event
from velora.cli import main


class TestCliSmoke(unittest.TestCase):
    def test_status(self):
        self.assertEqual(main(["status"]), 0)

    def test_audit_inspect_prints_summary_for_fixture_run(self):
        with TemporaryDirectory() as tmp:
            run_id = "run-001"
            append_event(
                run_id,
                AuditEvent(
                    run_id=run_id,
                    iteration=0,
                    event_type="run_start",
                    timestamp="2026-03-09T00:00:00+00:00",
                    payload={"objective_snippet": "ship audit inspect"},
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
            append_event(
                run_id,
                AuditEvent(
                    run_id=run_id,
                    iteration=1,
                    event_type="run_end",
                    timestamp="2026-03-09T00:00:02+00:00",
                    payload={"status": "ready"},
                ),
                base_dir=Path(tmp),
            )

            with (
                redirect_stdout(StringIO()) as out,
                unittest.mock.patch("velora.cli.Path.cwd", return_value=Path(tmp)),
            ):
                rc = main(["audit", "inspect", "--run", run_id])

            self.assertEqual(rc, 0)
            text = out.getvalue()
            self.assertIn("run_id: run-001", text)
            self.assertIn("final_status: ready", text)

            with (
                redirect_stdout(StringIO()) as out_latest,
                unittest.mock.patch("velora.cli.Path.cwd", return_value=Path(tmp)),
            ):
                rc_latest = main(["audit", "inspect"])

            self.assertEqual(rc_latest, 0)
            self.assertIn("run_id: run-001", out_latest.getvalue())


if __name__ == "__main__":
    unittest.main()
