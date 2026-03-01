import json
import tempfile
import unittest

from velora.spec import load_run_spec


class TestRunSpec(unittest.TestCase):
    def test_load_run_spec_from_file(self):
        payload = {"task": "do the thing", "title": "My PR", "max_attempts": 2}
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=True) as fh:
            fh.write(json.dumps(payload))
            fh.flush()
            spec = load_run_spec(fh.name)
        self.assertEqual(spec.task, "do the thing")
        self.assertEqual(spec.title, "My PR")
        self.assertEqual(spec.max_attempts, 2)


if __name__ == "__main__":
    unittest.main()
