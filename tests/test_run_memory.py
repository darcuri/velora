import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from velora.run import run_task
from velora.run_memory import (
    build_coordinator_brief,
    coordinator_replay_paths,
    render_coordinator_memory,
    seed_run_replay,
    sync_run_replay,
)
from velora.spec import RunSpec


class TestRunMemory(unittest.TestCase):
    def test_seed_run_replay_writes_initial_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_path = Path(tmp) / "repo"
            repo_path.mkdir(parents=True)
            request = {
                "run_id": "task123",
                "iteration": 1,
                "objective": "Repair replay handling without regressing worker brief quality",
                "repo": {
                    "owner": "octocat",
                    "name": "velora",
                    "default_branch": "main",
                    "work_branch": "velora/task123",
                },
                "history": {"no_progress_streak": 0},
            }

            paths = seed_run_replay(repo_path, request=request, max_attempts=5, verb="fix")

            self.assertTrue(paths["history"].exists())
            self.assertTrue(paths["brief"].exists())
            self.assertTrue(paths["memory"].exists())

            history = [json.loads(line) for line in paths["history"].read_text(encoding="utf-8").splitlines() if line.strip()]
            self.assertEqual(len(history), 1)
            self.assertEqual(history[0]["event"], "run_started")
            self.assertEqual(history[0]["iteration"], 0)
            self.assertEqual(history[0]["data"]["verb"], "fix")

            brief = json.loads(paths["brief"].read_text(encoding="utf-8"))
            self.assertEqual(brief["run_id"], "task123")
            self.assertEqual(brief["repo"]["owner"], "octocat")
            self.assertEqual(brief["objective"]["verb"], "fix")
            self.assertEqual(brief["iteration"]["max"], 5)
            self.assertEqual(brief["status"]["state"], "starting")

            memory_text = paths["memory"].read_text(encoding="utf-8")
            self.assertIn("# Coordinator Replay", memory_text)
            self.assertIn("Run: task123", memory_text)
            self.assertIn("Repair replay handling", memory_text)
            self.assertIn("CoordinatorRequest is authoritative", memory_text)

    def test_brief_and_memory_render_initial_state(self) -> None:
        request = {
            "run_id": "task123",
            "iteration": 1,
            "objective": "Do a careful fix",
            "repo": {
                "owner": "octocat",
                "name": "velora",
                "default_branch": "main",
                "work_branch": "velora/task123",
            },
            "history": {"no_progress_streak": 2},
        }

        brief = build_coordinator_brief(request=request, max_attempts=8, verb="fix")
        memory_text = render_coordinator_memory(brief)

        self.assertEqual(brief["iteration"]["current"], 1)
        self.assertEqual(brief["iteration"]["no_progress_streak"], 2)
        self.assertEqual(brief["quality_gates"]["tests"], "unknown")
        self.assertIn("Iteration: 1 of 8", memory_text)
        self.assertIn("Do a careful fix", memory_text)

    def test_sync_run_replay_reflects_latest_decision_and_worker_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_path = Path(tmp) / "repo"
            repo_path.mkdir(parents=True)
            request = {
                "run_id": "task123",
                "iteration": 2,
                "objective": "Do a careful fix",
                "repo": {
                    "owner": "octocat",
                    "name": "velora",
                    "default_branch": "main",
                    "work_branch": "velora/task123",
                },
                "history": {"no_progress_streak": 1},
                "state": {
                    "latest_coordinator_decision": {
                        "decision": "execute_work_item",
                        "reason": "Tighten validation",
                        "selected_specialist": {"role": "implementer", "runner": "claude"},
                        "work_item": {
                            "id": "WI-0002",
                            "kind": "repair",
                            "rationale": "Tighten validation around replay sync",
                            "scope_hints": {"likely_files": ["velora/run_memory.py"], "search_terms": ["sync_run_replay"]},
                        },
                    },
                    "latest_worker_result": {
                        "work_item_id": "WI-0002",
                        "status": "completed",
                        "summary": "Updated replay syncing and tests",
                        "head_sha": "abc123",
                        "follow_up": ["Wire direct coordinator backend to consume replay"],
                        "tests_run": [{"command": "pytest", "status": "pass", "details": "ok"}],
                        "blockers": [],
                    },
                    "latest_ci": {"state": "success", "detail": "pytest passed", "classification": None},
                },
            }

            paths = sync_run_replay(repo_path, request=request, max_attempts=8, verb="fix")
            brief = json.loads(paths["brief"].read_text(encoding="utf-8"))
            memory_text = paths["memory"].read_text(encoding="utf-8")

            self.assertEqual(brief["status"]["last_decision"], "execute_work_item")
            self.assertEqual(brief["latest_work_item"]["id"], "WI-0002")
            self.assertEqual(brief["latest_outcome"]["kind"], "ci_result")
            self.assertEqual(brief["quality_gates"]["tests"], "pass")
            self.assertEqual(brief["quality_gates"]["ci"], "pass")
            self.assertIn("Wire direct coordinator backend to consume replay", brief["open_loops"])
            self.assertIn("Last decision: execute_work_item", memory_text)
            self.assertIn("Last work item: WI-0002 repair via claude/implementer", memory_text)
            self.assertIn("Latest outcome: success: pytest passed", memory_text)

    def test_mode_a_run_seeds_replay_bundle_before_first_coordinator_turn(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo_path = tmp_path / "repo"
            repo_path.mkdir(parents=True)
            velora_home = tmp_path / "velora-home"
            velora_home.mkdir(parents=True)

            mock_gh = MagicMock()
            mock_gh.get_default_branch.return_value = "main"
            coord = SimpleNamespace(
                decision="finalize_success",
                reason="all good",
                selected_specialist=SimpleNamespace(role="implementer", runner="claude"),
                work_item=None,
            )
            coord_run = SimpleNamespace(response=coord, cmd=SimpleNamespace(usage=None))

            with (
                patch.dict(os.environ, {"VELORA_ALLOWED_OWNERS": "octocat"}, clear=False),
                patch("velora.run.GitHubClient.from_env", return_value=mock_gh),
                patch("velora.run.ensure_repo_checkout", return_value=repo_path),
                patch("velora.run.build_task_id", return_value="task123"),
                patch("velora.run.velora_home", return_value=velora_home),
                patch("velora.run.run_coordinator", return_value=coord_run),
                patch("velora.run.upsert_task", return_value={}),
            ):
                result = run_task("octocat/velora", "fix", RunSpec(task="Do a careful fix"), use_coordinator=True)

            self.assertEqual(result["status"], "ready")
            paths = coordinator_replay_paths(repo_path, "task123")
            self.assertTrue(paths["history"].exists())
            self.assertTrue(paths["brief"].exists())
            self.assertTrue(paths["memory"].exists())

            history = [json.loads(line) for line in paths["history"].read_text(encoding="utf-8").splitlines() if line.strip()]
            self.assertEqual([entry["event"] for entry in history], ["run_started", "coordinator_decision", "run_terminal"])
            self.assertEqual(history[0]["data"]["verb"], "fix")

            brief = json.loads(paths["brief"].read_text(encoding="utf-8"))
            self.assertEqual(brief["status"]["state"], "ready")
            self.assertEqual(brief["status"]["last_decision"], "finalize_success")


if __name__ == "__main__":
    unittest.main()
