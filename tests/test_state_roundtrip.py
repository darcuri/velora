import tempfile
import unittest
from pathlib import Path

from velora.state import load_tasks, save_tasks, upsert_task


class TestStateRoundTrip(unittest.TestCase):
    def test_registry_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            registry = {"version": 1, "tasks": [{"task_id": "t1", "status": "running"}]}
            save_tasks(registry, home=home)
            loaded = load_tasks(home=home)
            self.assertEqual(loaded, registry)

            upsert_task({"task_id": "t1", "status": "ready"}, home=home)
            updated = load_tasks(home=home)
            self.assertEqual(updated["tasks"][0]["status"], "ready")


if __name__ == "__main__":
    unittest.main()

