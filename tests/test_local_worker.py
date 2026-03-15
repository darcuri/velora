import json
import subprocess
import unittest

import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

from velora.local_worker import (
    HarnessReason,
    HarnessOutcome,
    assemble_work_result,
    build_local_worker_prompt,
    ConversationManager,
    run_local_worker_loop,
)
from velora.protocol import validate_work_result, WorkItem
from velora.worker_actions import WorkerScope
from velora.acpx import CmdResult


class TestHarnessOutcome(unittest.TestCase):
    def test_success_outcome(self):
        outcome = HarnessOutcome(success=True, reason=HarnessReason.SUCCESS, evidence=[])
        self.assertTrue(outcome.success)
        self.assertEqual(outcome.reason, HarnessReason.SUCCESS)

    def test_failure_outcome(self):
        outcome = HarnessOutcome(
            success=False,
            reason=HarnessReason.TESTS_EXHAUSTED,
            evidence=["FAILED tests/test_foo.py::test_bar"],
        )
        self.assertFalse(outcome.success)
        self.assertEqual(outcome.reason, HarnessReason.TESTS_EXHAUSTED)


class TestAssembleWorkResult(unittest.TestCase):
    def test_success_produces_valid_completed_work_result(self):
        outcome = HarnessOutcome(success=True, reason=HarnessReason.SUCCESS, evidence=["all tests passed"])
        wr = assemble_work_result(
            outcome=outcome,
            work_item_id="WI-001",
            summary="Added feature X",
            branch="velora/wi-001",
            head_sha="abc123def456",
            files_touched=["src/main.py"],
            tests_run=[{"command": "python -m pytest -q", "status": "pass", "details": "1 passed"}],
        )
        # Must survive protocol validation
        validated = validate_work_result(wr)
        self.assertEqual(validated.status, "completed")
        self.assertEqual(validated.branch, "velora/wi-001")
        self.assertEqual(validated.head_sha, "abc123def456")
        self.assertEqual(validated.blockers, [])

    def test_blocked_produces_valid_blocked_work_result(self):
        outcome = HarnessOutcome(
            success=False,
            reason=HarnessReason.SCOPE_INSUFFICIENT,
            evidence=["need access to velora/config.py"],
        )
        wr = assemble_work_result(
            outcome=outcome,
            work_item_id="WI-001",
            summary="Could not complete",
            branch="",
            head_sha="",
            files_touched=[],
            tests_run=[],
        )
        validated = validate_work_result(wr)
        self.assertEqual(validated.status, "blocked")
        self.assertEqual(validated.blockers[0], "SCOPE_INSUFFICIENT")

    def test_failed_produces_valid_failed_work_result(self):
        outcome = HarnessOutcome(
            success=False,
            reason=HarnessReason.TESTS_EXHAUSTED,
            evidence=["FAILED test_foo.py"],
        )
        wr = assemble_work_result(
            outcome=outcome,
            work_item_id="WI-001",
            summary="Tests kept failing",
            branch="",
            head_sha="",
            files_touched=["src/main.py"],
            tests_run=[{"command": "python -m pytest -q", "status": "fail", "details": "1 failed"}],
        )
        validated = validate_work_result(wr)
        self.assertEqual(validated.status, "failed")
        self.assertIn("TESTS_EXHAUSTED", validated.blockers)


def _make_work_item(
    *,
    gates: list[str] | None = None,
    likely_files: list[str] | None = None,
) -> WorkItem:
    return WorkItem.from_dict({
        "id": "WI-001",
        "kind": "implement",
        "rationale": "Add the foo feature",
        "instructions": ["Create foo.py", "Add a foo() function that returns 42"],
        "scope_hints": {
            "likely_files": likely_files if likely_files is not None else ["src/foo.py", "tests/test_foo.py"],
            "search_terms": ["foo"],
        },
        "acceptance": {
            "must": ["foo() returns 42"],
            "must_not": ["Do not modify existing files"],
            "gates": gates if gates is not None else ["tests"],
        },
        "limits": {"max_diff_lines": 100, "max_commits": 1},
        "commit": {
            "message": "feat: add foo",
            "footer": {
                "VELORA_RUN_ID": "run-001",
                "VELORA_ITERATION": 1,
                "WORK_ITEM_ID": "WI-001",
            },
        },
    })


class TestBuildPrompt(unittest.TestCase):
    def test_prompt_contains_task_details(self):
        wi = _make_work_item()
        prompt = build_local_worker_prompt(
            work_item=wi,
            repo_ref="owner/repo",
            work_branch="velora/wi-001",
            test_commands=["python -m pytest -q"],
        )
        self.assertIn("owner/repo", prompt)
        self.assertIn("velora/wi-001", prompt)
        self.assertIn("WI-001", prompt)
        self.assertIn("Add the foo feature", prompt)
        self.assertIn("src/foo.py", prompt)
        self.assertIn("python -m pytest -q", prompt)

    def test_prompt_contains_propulsion_language(self):
        wi = _make_work_item()
        prompt = build_local_worker_prompt(
            work_item=wi,
            repo_ref="owner/repo",
            work_branch="velora/wi-001",
            test_commands=["python -m pytest -q"],
        )
        self.assertIn("Do not ask questions", prompt)
        self.assertIn("JSON only", prompt)

    def test_prompt_lists_all_actions(self):
        wi = _make_work_item()
        prompt = build_local_worker_prompt(
            work_item=wi,
            repo_ref="owner/repo",
            work_branch="velora/wi-001",
            test_commands=[],
        )
        for action in ["read_file", "list_files", "write_file", "patch_file",
                        "search_files", "run_tests", "work_complete", "work_blocked"]:
            self.assertIn(action, prompt)


class TestConversationManager(unittest.TestCase):
    def test_init_with_system_prompt(self):
        cm = ConversationManager(system_prompt="You are a tool.")
        msgs = cm.messages()
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0]["role"], "system")

    def test_append_turn(self):
        cm = ConversationManager(system_prompt="You are a tool.")
        cm.append_assistant('{"action": "read_file", "params": {"path": "x.py"}}')
        cm.append_user('{"status": "ok", "result": "contents"}')
        self.assertEqual(len(cm.messages()), 3)

    def test_context_bytes_tracked(self):
        cm = ConversationManager(system_prompt="short")
        cm.append_assistant("a" * 100)
        cm.append_user("b" * 200)
        self.assertGreater(cm.context_bytes, 0)

    def test_summarization_truncates_old_large_messages(self):
        cm = ConversationManager(system_prompt="sys", recency_window=2)
        # Add 6 turns (3 assistant + 3 user), first user message is huge
        cm.append_assistant("act1")
        cm.append_user("x" * 5000)  # big result, will be old after more turns
        cm.append_assistant("act2")
        cm.append_user("small")
        cm.append_assistant("act3")
        cm.append_user("small2")
        cm.summarize()
        # The big message (index 2, the first user msg) should be truncated
        msgs = cm.messages()
        big_msg = msgs[2]  # first user message
        self.assertIn("[truncated]", big_msg["content"])
        self.assertLess(len(big_msg["content"]), 5000)


def _make_scope(repo: Path) -> WorkerScope:
    return WorkerScope(
        repo_root=repo,
        allowed_files={"src/main.py"},
        allowed_dirs={"src"},
        test_commands=["python -m pytest -q"],
        work_branch="velora/wi-001",
    )


class TestHarnessLoop(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.repo = Path(self.tmp)
        (self.repo / "src").mkdir()
        (self.repo / "src" / "main.py").write_text("x = 1\n")

    def _mock_llm_responses(self, responses: list[str]):
        """Create a side_effect that returns CmdResult for each response."""
        results = [CmdResult(returncode=0, stdout=r, stderr="") for r in responses]
        return results

    def test_work_complete_terminates_loop(self):
        responses = self._mock_llm_responses([
            '{"action": "read_file", "params": {"path": "src/main.py"}}',
            '{"action": "work_complete", "params": {"summary": "read the file"}}',
        ])
        with patch("velora.local_worker._call_local_llm_chat", side_effect=responses):
            outcome = run_local_worker_loop(
                scope=_make_scope(self.repo),
                system_prompt="You are a tool.",
            )
        self.assertEqual(outcome.reason, HarnessReason.SUCCESS)
        self.assertEqual(outcome.llm_summary, "read the file")

    def test_work_blocked_terminates_loop(self):
        responses = self._mock_llm_responses([
            '{"action": "work_blocked", "params": {"reason": "SCOPE_INSUFFICIENT", "blockers": ["need config.py"]}}',
        ])
        with patch("velora.local_worker._call_local_llm_chat", side_effect=responses):
            outcome = run_local_worker_loop(
                scope=_make_scope(self.repo),
                system_prompt="You are a tool.",
            )
        self.assertEqual(outcome.reason, HarnessReason.SCOPE_INSUFFICIENT)
        self.assertFalse(outcome.success)

    def test_iteration_cap_terminates_loop(self):
        # 25 read_file actions — exceeds default cap of 20
        responses = self._mock_llm_responses(
            ['{"action": "read_file", "params": {"path": "src/main.py"}}'] * 25
        )
        with patch("velora.local_worker._call_local_llm_chat", side_effect=responses):
            outcome = run_local_worker_loop(
                scope=_make_scope(self.repo),
                system_prompt="You are a tool.",
                iteration_cap=20,
            )
        self.assertEqual(outcome.reason, HarnessReason.ITERATION_LIMIT)

    def test_parse_failure_cap_terminates_loop(self):
        responses = self._mock_llm_responses([
            "this is not json",
            "also not json",
            "still not json",
        ])
        with patch("velora.local_worker._call_local_llm_chat", side_effect=responses):
            outcome = run_local_worker_loop(
                scope=_make_scope(self.repo),
                system_prompt="You are a tool.",
                parse_failure_cap=3,
            )
        self.assertEqual(outcome.reason, HarnessReason.PARSE_FAILURES)


def _init_git_repo(path: Path) -> None:
    """Create a minimal git repo with an initial commit."""
    subprocess.run(["git", "init", str(path)], capture_output=True, check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "test@test.com"], capture_output=True, check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Test"], capture_output=True, check=True)
    (path / "README.md").write_text("init\n")
    subprocess.run(["git", "-C", str(path), "add", "."], capture_output=True, check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-m", "init"], capture_output=True, check=True)


class TestEndgame(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.repo = Path(self.tmp)
        _init_git_repo(self.repo)
        (self.repo / "src").mkdir()
        (self.repo / "src" / "main.py").write_text("x = 1\n")
        subprocess.run(["git", "-C", str(self.repo), "add", "."], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-m", "add src"], capture_output=True, check=True)
        # Create work branch
        subprocess.run(["git", "-C", str(self.repo), "checkout", "-b", "velora/wi-001"], capture_output=True, check=True)

    def test_diff_audit_detects_no_changes(self):
        from velora.local_worker import _run_endgame
        scope = WorkerScope(
            repo_root=self.repo,
            allowed_files={"src/main.py"},
            allowed_dirs={"src"},
            test_commands=[],
            work_branch="velora/wi-001",
        )
        outcome = _run_endgame(scope=scope, work_item=_make_work_item(), llm_summary="done")
        self.assertEqual(outcome.reason, HarnessReason.NO_CHANGES)

    def test_diff_audit_detects_scope_violation(self):
        from velora.local_worker import _run_endgame
        # Modify a file outside scope
        (self.repo / "README.md").write_text("modified\n")
        scope = WorkerScope(
            repo_root=self.repo,
            allowed_files={"src/main.py"},
            allowed_dirs={"src"},
            test_commands=[],
            work_branch="velora/wi-001",
        )
        outcome = _run_endgame(scope=scope, work_item=_make_work_item(), llm_summary="done")
        self.assertEqual(outcome.reason, HarnessReason.SCOPE_VIOLATION)

    def test_diff_audit_detects_diff_limit(self):
        from velora.local_worker import _run_endgame
        # Create a change that exceeds max_diff_lines (100 in test WorkItem)
        big_content = "\n".join(f"line{i} = {i}" for i in range(200))
        (self.repo / "src" / "main.py").write_text(big_content)
        scope = WorkerScope(
            repo_root=self.repo,
            allowed_files={"src/main.py"},
            allowed_dirs={"src"},
            test_commands=[],
            work_branch="velora/wi-001",
        )
        outcome = _run_endgame(scope=scope, work_item=_make_work_item(), llm_summary="done")
        self.assertEqual(outcome.reason, HarnessReason.DIFF_LIMIT)

    def test_successful_endgame_commits(self):
        from velora.local_worker import _run_endgame
        # Modify a file in scope
        (self.repo / "src" / "main.py").write_text("x = 2\n")
        scope = WorkerScope(
            repo_root=self.repo,
            allowed_files={"src/main.py"},
            allowed_dirs={"src"},
            test_commands=[],
            work_branch="velora/wi-001",
        )
        wi = _make_work_item(gates=[])
        outcome = _run_endgame(scope=scope, work_item=wi, llm_summary="changed x")
        self.assertEqual(outcome.reason, HarnessReason.SUCCESS)
        self.assertTrue(outcome.success)
        self.assertTrue(outcome.head_sha)  # should have a commit SHA
        self.assertIn("src/main.py", outcome.files_touched)


class TestRunLocalWorker(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.repo = Path(self.tmp)
        _init_git_repo(self.repo)
        (self.repo / "src").mkdir()
        (self.repo / "src" / "main.py").write_text("x = 1\n")
        subprocess.run(["git", "-C", str(self.repo), "add", "."], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-m", "add src"], capture_output=True, check=True)
        self.exchange_dir = Path(tempfile.mkdtemp())

    def test_blocked_outcome_writes_block_json(self):
        from velora.local_worker import run_local_worker
        responses = [
            CmdResult(0, '{"action": "work_blocked", "params": {"reason": "SCOPE_INSUFFICIENT", "blockers": ["need config"]}}', ""),
        ]
        wi = _make_work_item()
        with patch("velora.local_worker._call_local_llm_chat", side_effect=responses):
            cmd_result = run_local_worker(
                work_item=wi,
                repo_root=self.repo,
                work_branch="velora/wi-001",
                exchange_dir=self.exchange_dir,
                repo_ref="owner/repo",
                run_id="run-001",
                verb="fix",
                objective="fix the thing",
                iteration=1,
            )
        self.assertEqual(cmd_result.returncode, 0)
        block_file = self.exchange_dir / "block.json"
        self.assertTrue(block_file.exists())
        payload = json.loads(block_file.read_text())
        self.assertEqual(payload["status"], "blocked")
        self.assertIn("SCOPE_INSUFFICIENT", payload["blockers"])

    def test_success_outcome_writes_result_json(self):
        from velora.local_worker import run_local_worker
        responses = [
            CmdResult(0, '{"action": "patch_file", "params": {"path": "src/main.py", "old": "x = 1", "new": "x = 2"}}', ""),
            CmdResult(0, '{"action": "work_complete", "params": {"summary": "changed x to 2"}}', ""),
        ]
        wi = _make_work_item(gates=[], likely_files=["src/main.py"])
        with patch("velora.local_worker._call_local_llm_chat", side_effect=responses):
            cmd_result = run_local_worker(
                work_item=wi,
                repo_root=self.repo,
                work_branch="velora/wi-001",
                exchange_dir=self.exchange_dir,
                repo_ref="owner/repo",
                run_id="run-001",
                verb="fix",
                objective="fix the thing",
                iteration=1,
            )
        self.assertEqual(cmd_result.returncode, 0)
        result_file = self.exchange_dir / "result.json"
        self.assertTrue(result_file.exists())
        payload = json.loads(result_file.read_text())
        self.assertEqual(payload["status"], "completed")
        self.assertIn("src/main.py", payload["files_touched"])

    def test_dirty_tree_writes_block_json(self):
        from velora.local_worker import run_local_worker
        # Make the tree dirty
        (self.repo / "src" / "main.py").write_text("x = dirty\n")
        wi = _make_work_item(gates=[])
        cmd_result = run_local_worker(
            work_item=wi,
            repo_root=self.repo,
            work_branch="velora/wi-001",
            exchange_dir=self.exchange_dir,
            repo_ref="owner/repo",
            run_id="run-001",
            verb="fix",
            objective="fix the thing",
            iteration=1,
        )
        self.assertEqual(cmd_result.returncode, 0)
        block_file = self.exchange_dir / "block.json"
        self.assertTrue(block_file.exists())
        payload = json.loads(block_file.read_text())
        self.assertIn("COMMIT_FAILED", payload["blockers"])

    def test_no_changes_writes_block_json(self):
        from velora.local_worker import run_local_worker
        # Worker completes without making changes
        responses = [
            CmdResult(0, '{"action": "work_complete", "params": {"summary": "nothing to do"}}', ""),
        ]
        wi = _make_work_item(gates=[])
        with patch("velora.local_worker._call_local_llm_chat", side_effect=responses):
            cmd_result = run_local_worker(
                work_item=wi,
                repo_root=self.repo,
                work_branch="velora/wi-001",
                exchange_dir=self.exchange_dir,
                repo_ref="owner/repo",
                run_id="run-001",
                verb="fix",
                objective="fix the thing",
                iteration=1,
            )
        self.assertEqual(cmd_result.returncode, 0)
        block_file = self.exchange_dir / "block.json"
        self.assertTrue(block_file.exists())
        payload = json.loads(block_file.read_text())
        self.assertEqual(payload["status"], "failed")
        self.assertIn("NO_CHANGES", payload["blockers"])


if __name__ == "__main__":
    unittest.main()
