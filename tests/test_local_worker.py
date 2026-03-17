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
    _parse_action,
    _repair_json_newlines,
    _build_scope,
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


class TestRepairJsonNewlines(unittest.TestCase):
    """Tests for _repair_json_newlines — escapes control chars inside strings only."""

    def test_no_strings_unchanged(self):
        text = '{"action": "read_file"}'
        self.assertEqual(_repair_json_newlines(text), text)

    def test_structural_newlines_preserved(self):
        text = '{\n  "action": "read_file",\n  "params": {"path": "x.py"}\n}'
        self.assertEqual(_repair_json_newlines(text), text)

    def test_literal_newline_in_string_escaped(self):
        # Model emits a literal newline inside "content" value
        text = '{"action": "write_file", "params": {"path": "x.py", "content": "line1\nline2"}}'
        result = _repair_json_newlines(text)
        self.assertIn("line1\\nline2", result)
        # Must parse as valid JSON
        obj = json.loads(result)
        self.assertEqual(obj["params"]["content"], "line1\nline2")

    def test_multiline_json_with_embedded_newlines(self):
        # The key case: multi-line JSON AND literal newlines inside strings
        text = (
            '{\n'
            '  "action": "write_file",\n'
            '  "params": {\n'
            '    "path": "foo.py",\n'
            '    "content": "def foo():\n    return 42"\n'
            '  }\n'
            '}'
        )
        result = _repair_json_newlines(text)
        obj = json.loads(result)
        self.assertEqual(obj["action"], "write_file")
        self.assertEqual(obj["params"]["content"], "def foo():\n    return 42")

    def test_escaped_quote_inside_string(self):
        # Backslash-quote should not end the string
        text = '{"action": "write_file", "params": {"content": "say \\"hello\\"\nbye"}}'
        result = _repair_json_newlines(text)
        obj = json.loads(result)
        self.assertIn("hello", obj["params"]["content"])
        self.assertIn("bye", obj["params"]["content"])

    def test_escaped_backslash_before_quote(self):
        # \\\\" — the backslash is escaped, so the quote ends the string
        text = '{"a": "val\\\\"}'
        result = _repair_json_newlines(text)
        obj = json.loads(result)
        self.assertEqual(obj["a"], "val\\")

    def test_tab_in_string_escaped(self):
        text = '{"a": "col1\tcol2"}'
        result = _repair_json_newlines(text)
        obj = json.loads(result)
        self.assertEqual(obj["a"], "col1\tcol2")

    def test_cr_in_string_escaped(self):
        text = '{"a": "line1\r\nline2"}'
        result = _repair_json_newlines(text)
        obj = json.loads(result)
        self.assertEqual(obj["a"], "line1\r\nline2")

    def test_already_escaped_newline_not_double_escaped(self):
        # String already has \\n — should pass through unchanged
        text = '{"a": "line1\\nline2"}'
        result = _repair_json_newlines(text)
        obj = json.loads(result)
        self.assertEqual(obj["a"], "line1\nline2")


class TestParseAction(unittest.TestCase):
    """Tests for _parse_action — LLM response to (action, params)."""

    def test_simple_valid_json(self):
        result = _parse_action('{"action": "read_file", "params": {"path": "x.py"}}')
        self.assertIsNotNone(result)
        action, params = result
        self.assertEqual(action, "read_file")
        self.assertEqual(params["path"], "x.py")

    def test_markdown_fenced_json(self):
        raw = '```json\n{"action": "read_file", "params": {"path": "x.py"}}\n```'
        result = _parse_action(raw)
        self.assertIsNotNone(result)
        self.assertEqual(result[0], "read_file")

    def test_xml_tool_tags_stripped(self):
        raw = '<tool_call>{"action": "read_file", "params": {"path": "x.py"}}</tool_call>'
        result = _parse_action(raw)
        self.assertIsNotNone(result)
        self.assertEqual(result[0], "read_file")

    def test_multiline_json_with_embedded_newlines(self):
        # The bug case: pretty-printed JSON with literal newlines in string values
        raw = (
            '{\n'
            '  "action": "write_file",\n'
            '  "params": {\n'
            '    "path": "foo.py",\n'
            '    "content": "def foo():\n    return 42"\n'
            '  }\n'
            '}'
        )
        result = _parse_action(raw)
        self.assertIsNotNone(result)
        action, params = result
        self.assertEqual(action, "write_file")
        self.assertEqual(params["content"], "def foo():\n    return 42")

    def test_patch_file_with_multiline_old_new(self):
        raw = (
            '{\n'
            '  "action": "patch_file",\n'
            '  "params": {\n'
            '    "path": "main.py",\n'
            '    "old": "x = 1\ny = 2",\n'
            '    "new": "x = 10\ny = 20"\n'
            '  }\n'
            '}'
        )
        result = _parse_action(raw)
        self.assertIsNotNone(result)
        action, params = result
        self.assertEqual(action, "patch_file")
        self.assertEqual(params["old"], "x = 1\ny = 2")
        self.assertEqual(params["new"], "x = 10\ny = 20")

    def test_garbage_returns_none(self):
        self.assertIsNone(_parse_action("I will now read the file"))

    def test_missing_action_returns_none(self):
        self.assertIsNone(_parse_action('{"params": {"path": "x.py"}}'))

    def test_missing_params_returns_none(self):
        self.assertIsNone(_parse_action('{"action": "read_file"}'))

    def test_array_returns_none(self):
        self.assertIsNone(_parse_action('[1, 2, 3]'))


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


def _make_investigate_work_item(
    *,
    likely_files: list[str] | None = None,
) -> WorkItem:
    return WorkItem.from_dict({
        "id": "WI-INV",
        "kind": "investigate",
        "rationale": "Discover repo test infrastructure",
        "instructions": ["Read pyproject.toml, setup.cfg, Makefile", "Determine test framework and command"],
        "scope_hints": {
            "likely_files": likely_files if likely_files is not None else ["pyproject.toml", "setup.cfg"],
            "search_terms": ["test"],
        },
        "acceptance": {
            "must": ["Report test framework and command"],
            "must_not": ["Do not modify files"],
            "gates": [],
        },
        "limits": {"max_diff_lines": 50, "max_commits": 1},
        "commit": {
            "message": "investigate: discover test infrastructure",
            "footer": {
                "VELORA_RUN_ID": "run-001",
                "VELORA_ITERATION": 1,
                "WORK_ITEM_ID": "WI-INV",
            },
        },
    })


class TestInvestigateWorkItem(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.repo = Path(self.tmp)
        _init_git_repo(self.repo)
        self.exchange_dir = Path(tempfile.mkdtemp())

    def test_investigate_succeeds_without_changes(self):
        from velora.local_worker import run_local_worker
        responses = [
            CmdResult(0, '{"action": "list_files", "params": {"path": "."}}', ""),
            CmdResult(0, '{"action": "read_file", "params": {"path": "pyproject.toml"}}', ""),
            CmdResult(0, json.dumps({
                "action": "work_complete",
                "params": {
                    "summary": "Repo uses unittest",
                    "findings": {
                        "test_command": "python -m unittest discover -s tests",
                        "test_framework": "unittest",
                        "test_dirs": ["tests/"],
                    },
                },
            }), ""),
        ]
        wi = _make_investigate_work_item()
        with patch("velora.local_worker._call_local_llm_chat", side_effect=responses):
            cmd_result = run_local_worker(
                work_item=wi,
                repo_root=self.repo,
                work_branch="velora/wi-inv",
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
        self.assertEqual(payload["files_touched"], [])
        # Evidence should contain DISCOVERY: prefixed JSON
        discovery_entries = [e for e in payload["evidence"] if e.startswith("DISCOVERY:")]
        self.assertEqual(len(discovery_entries), 1)
        discovery = json.loads(discovery_entries[0][len("DISCOVERY:"):])
        self.assertEqual(discovery["test_command"], "python -m unittest discover -s tests")

    def test_investigate_without_findings_still_succeeds(self):
        from velora.local_worker import run_local_worker
        responses = [
            CmdResult(0, '{"action": "work_complete", "params": {"summary": "No test config found"}}', ""),
        ]
        wi = _make_investigate_work_item()
        with patch("velora.local_worker._call_local_llm_chat", side_effect=responses):
            cmd_result = run_local_worker(
                work_item=wi,
                repo_root=self.repo,
                work_branch="velora/wi-inv",
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
        # No findings → no DISCOVERY entries
        discovery_entries = [e for e in payload["evidence"] if e.startswith("DISCOVERY:")]
        self.assertEqual(len(discovery_entries), 0)


class TestInvestigateFindings(unittest.TestCase):
    def test_findings_captured_in_loop_outcome(self):
        tmp = tempfile.mkdtemp()
        repo = Path(tmp)
        (repo / "src").mkdir()
        (repo / "src" / "main.py").write_text("x = 1\n")
        scope = _make_scope(repo)
        findings_response = json.dumps({
            "action": "work_complete",
            "params": {
                "summary": "Uses pytest",
                "findings": {"test_command": "python -m pytest -q", "test_framework": "pytest"},
            },
        })
        responses = [CmdResult(0, findings_response, "")]
        with patch("velora.local_worker._call_local_llm_chat", side_effect=responses):
            outcome = run_local_worker_loop(
                scope=scope,
                system_prompt="You are a tool.",
            )
        self.assertTrue(outcome.success)
        self.assertIsNotNone(outcome.llm_findings)
        self.assertEqual(outcome.llm_findings["test_command"], "python -m pytest -q")

    def test_no_findings_leaves_none(self):
        tmp = tempfile.mkdtemp()
        repo = Path(tmp)
        (repo / "src").mkdir()
        (repo / "src" / "main.py").write_text("x = 1\n")
        scope = _make_scope(repo)
        responses = [CmdResult(0, '{"action": "work_complete", "params": {"summary": "done"}}', "")]
        with patch("velora.local_worker._call_local_llm_chat", side_effect=responses):
            outcome = run_local_worker_loop(
                scope=scope,
                system_prompt="You are a tool.",
            )
        self.assertTrue(outcome.success)
        self.assertIsNone(outcome.llm_findings)


class TestBuildScopeDiscoveredCommands(unittest.TestCase):
    def test_discovered_commands_override_gate_commands(self):
        wi = _make_work_item(gates=["tests"])
        scope = _build_scope(
            wi, Path("/tmp/repo"), "velora/wi-001",
            discovered_test_commands=["python -m unittest discover -s tests"],
        )
        self.assertEqual(scope.test_commands, ["python -m unittest discover -s tests"])

    def test_falls_back_to_gate_commands_without_discovery(self):
        wi = _make_work_item(gates=["tests"])
        scope = _build_scope(wi, Path("/tmp/repo"), "velora/wi-001")
        # Should fall back to GATE_COMMANDS default (pytest or env override)
        self.assertTrue(len(scope.test_commands) > 0)

    def test_empty_discovered_list_falls_back(self):
        wi = _make_work_item(gates=["tests"])
        scope = _build_scope(
            wi, Path("/tmp/repo"), "velora/wi-001",
            discovered_test_commands=[],
        )
        # Empty list is falsy, should fall back
        self.assertTrue(len(scope.test_commands) > 0)


class TestInvestigatePrompt(unittest.TestCase):
    def test_investigate_prompt_is_read_only(self):
        wi = _make_investigate_work_item()
        prompt = build_local_worker_prompt(
            work_item=wi,
            repo_ref="owner/repo",
            work_branch="velora/wi-inv",
            test_commands=[],
        )
        # write_file/patch_file/run_tests should NOT appear as available actions
        actions_section = prompt.split("## Available actions")[1].split("## Rules")[0]
        self.assertNotIn('"write_file"', actions_section)
        self.assertNotIn('"patch_file"', actions_section)
        self.assertNotIn('"run_tests"', actions_section)
        # run_probe SHOULD be available for investigate
        self.assertIn('"run_probe"', actions_section)
        self.assertIn("READ-ONLY", prompt)
        self.assertIn("Investigate mode", prompt)

    def test_investigate_prompt_mentions_findings(self):
        wi = _make_investigate_work_item()
        prompt = build_local_worker_prompt(
            work_item=wi,
            repo_ref="owner/repo",
            work_branch="velora/wi-inv",
            test_commands=[],
        )
        self.assertIn("findings", prompt)
        self.assertIn("test_command", prompt)

    def test_implement_prompt_has_write_actions(self):
        wi = _make_work_item()
        prompt = build_local_worker_prompt(
            work_item=wi,
            repo_ref="owner/repo",
            work_branch="velora/wi-001",
            test_commands=["python -m pytest -q"],
        )
        self.assertIn("write_file", prompt)
        self.assertIn("patch_file", prompt)
        self.assertIn("run_tests", prompt)
        self.assertNotIn("Investigate mode", prompt)


if __name__ == "__main__":
    unittest.main()
