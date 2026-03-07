import json
import tempfile
import unittest
from pathlib import Path

from velora.exchange import append_event, read_json, work_item_exchange_paths, write_json


class TestExchange(unittest.TestCase):
    def test_work_item_exchange_paths_are_repo_local(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            paths = work_item_exchange_paths(repo, "run123", "WI-0001")

            self.assertEqual(paths["dir"], repo / ".velora" / "exchange" / "runs" / "run123" / "WI-0001")
            self.assertTrue(paths["dir"].exists())
            self.assertEqual(paths["result"].name, "result.json")
            self.assertEqual(paths["handoff"].name, "handoff.json")
            self.assertEqual(paths["block"].name, "block.json")
            self.assertEqual(paths["events"].name, "events.jsonl")

    def test_write_and_read_json_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "x" / "result.json"
            payload = {"protocol_version": 1, "status": "completed"}
            write_json(path, payload)
            self.assertEqual(read_json(path), payload)

    def test_append_event_writes_jsonl(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            append_event(path, "worker_start", {"run_id": "run123"})

            lines = path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 1)
            payload = json.loads(lines[0])
            self.assertEqual(payload["event"], "worker_start")
            self.assertEqual(payload["run_id"], "run123")
            self.assertIn("ts", payload)
