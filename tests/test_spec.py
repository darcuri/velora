import io
import json
import tempfile
import unittest
from unittest.mock import patch

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

    def test_load_run_spec_from_stdin(self):
        payload = {"task": "stdin task"}
        with patch("sys.stdin", io.StringIO(json.dumps(payload))):
            spec = load_run_spec("-")
        self.assertEqual(spec.task, "stdin task")


if __name__ == "__main__":
    unittest.main()
