import unittest

from velora.protocol import validate_coordinator_response
from velora.worker_prompt import build_worker_prompt_v1


class TestWorkerPrompt(unittest.TestCase):
    def test_build_prompt_includes_work_result_contract(self) -> None:
        resp = validate_coordinator_response(
            {
                "protocol_version": 1,
                "decision": "execute_work_item",
                "reason": "do",
                "selected_specialist": {"role": "implementer", "runner": "codex"},
                "work_item": {
                    "id": "WI-0001",
                    "kind": "implement",
                    "rationale": "Add thing",
                    "instructions": ["Do X"],
                    "scope_hints": {"likely_files": ["a.py"], "search_terms": ["foo"]},
                    "acceptance": {"must": ["tests pass"], "must_not": [], "gates": ["tests"]},
                    "limits": {"max_diff_lines": 100, "max_commits": 1},
                    "commit": {
                        "message": "Add thing",
                        "footer": {"VELORA_RUN_ID": "run-1", "VELORA_ITERATION": 1, "WORK_ITEM_ID": "WI-0001"},
                    },
                },
            }
        )
        assert resp.work_item is not None
        prompt = build_worker_prompt_v1(
            repo_ref="octocat/velora",
            verb="feature",
            objective="Add X",
            run_id="run-1",
            iteration=1,
            work_branch="velora/run-1",
            work_item_path="/tmp/repo/.velora/exchange/runs/run-1/WI-0001/work-item.json",
            result_path="/tmp/repo/.velora/exchange/runs/run-1/WI-0001/result.json",
            work_item=resp.work_item,
        )
        self.assertIn("Checkout branch velora/run-1", prompt)
        self.assertIn("VELORA_RUN_ID: run-1", prompt)
        self.assertIn("WORK_ITEM_ID: WI-0001", prompt)
        self.assertIn("Worker hard blocks (must NOT):", prompt)
        self.assertIn("Do not run git push.", prompt)
        self.assertIn("Velora task truth lives in orchestrator-owned task artifacts", prompt)
        self.assertIn("Do not claim tests passed unless you actually ran them.", prompt)
        self.assertIn("Read the local assignment snapshot from: /tmp/repo/.velora/exchange/runs/run-1/WI-0001/work-item.json", prompt)
        self.assertIn("Write exactly one final outcome file, and only one:", prompt)
        self.assertIn("result.json path: /tmp/repo/.velora/exchange/runs/run-1/WI-0001/result.json", prompt)
        self.assertIn("handoff.json path: /tmp/repo/.velora/exchange/runs/run-1/WI-0001/handoff.json", prompt)
        self.assertIn("block.json path: /tmp/repo/.velora/exchange/runs/run-1/WI-0001/block.json", prompt)
        self.assertIn("Use handoff.json for non-terminal success", prompt)
        self.assertIn('"work_item_id": "<string>"', prompt)
        self.assertIn("Unknown keys are forbidden", prompt)


if __name__ == "__main__":
    unittest.main()
