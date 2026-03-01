import datetime as dt
import tempfile
import unittest
from pathlib import Path

from velora.state import load_tasks, prune_stale_tasks, save_tasks


class TestGcPrune(unittest.TestCase):
    def test_prunes_old_running_tasks(self):
        now = dt.datetime.now(tz=dt.timezone.utc).replace(microsecond=0)
        old = (now - dt.timedelta(hours=48)).isoformat()
        recent = (now - dt.timedelta(hours=1)).isoformat()

        registry = {
            "version": 1,
            "tasks": [
                {"task_id": "old-running", "status": "running", "updated_at": old},
                {"task_id": "recent-running", "status": "running", "updated_at": recent},
                {"task_id": "old-ready", "status": "ready", "updated_at": old},
            ],
        }

        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            save_tasks(registry, home=home)

            dry = prune_stale_tasks(older_than_hours=24, dry_run=True, home=home)
            self.assertEqual(dry["count"], 1)
            self.assertEqual(dry["stale_marked"], ["old-running"])

            # Dry run shouldn't change persisted status.
            after_dry = load_tasks(home=home)
            self.assertEqual(after_dry["tasks"][0]["status"], "running")

            real = prune_stale_tasks(older_than_hours=24, dry_run=False, home=home)
            self.assertEqual(real["count"], 1)

            after = load_tasks(home=home)
            task0 = next(t for t in after["tasks"] if t["task_id"] == "old-running")
            self.assertEqual(task0["status"], "stale")
            self.assertIn("stale_at", task0)


if __name__ == "__main__":
    unittest.main()
