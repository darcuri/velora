"""Integration test: investigate → implement flow with non-default test runner.

Creates a temp repo that uses unittest (NOT pytest) to prove the investigator
correctly discovers the test runner and the implement worker uses it instead
of the hardcoded pytest default.

This is the exact scenario that broke dogfood runs before the investigator
feature: the harness ran `python -m pytest -q` against a repo that uses
`python -m unittest discover -s tests`, tests "passed" vacuously (pytest
found nothing) or failed outright.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from velora.acpx import CmdResult
from velora.local_worker import (
    HarnessReason,
    _build_scope,
    build_local_worker_prompt,
    run_local_worker,
)
from velora.protocol import WorkItem


def _init_unittest_repo(path: Path) -> None:
    """Create a minimal Python repo that uses unittest (not pytest)."""
    subprocess.run(["git", "init", str(path)], capture_output=True, check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "test@test.com"],
                    capture_output=True, check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Test"],
                    capture_output=True, check=True)

    # pyproject.toml — no pytest, just setuptools
    (path / "pyproject.toml").write_text(
        '[build-system]\nrequires = ["setuptools"]\n'
        'build-backend = "setuptools.build_meta"\n\n'
        '[project]\nname = "mylib"\nversion = "0.1.0"\n'
    )

    # Makefile with test command
    (path / "Makefile").write_text(
        "test:\n\tpython -m unittest discover -s tests -p 'test_*.py'\n"
    )

    # Source
    (path / "mylib").mkdir()
    (path / "mylib" / "__init__.py").write_text("")
    (path / "mylib" / "math.py").write_text(
        "def add(a: int, b: int) -> int:\n    return a + b\n"
    )

    # Tests using unittest
    (path / "tests").mkdir()
    (path / "tests" / "__init__.py").write_text("")
    (path / "tests" / "test_math.py").write_text(
        "import unittest\nfrom mylib.math import add\n\n"
        "class TestAdd(unittest.TestCase):\n"
        "    def test_add(self):\n"
        "        self.assertEqual(add(1, 2), 3)\n\n"
        "if __name__ == '__main__':\n    unittest.main()\n"
    )

    subprocess.run(["git", "-C", str(path), "add", "."], capture_output=True, check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-m", "init"],
                    capture_output=True, check=True)


def _make_investigate_wi() -> WorkItem:
    return WorkItem.from_dict({
        "id": "WI-INV",
        "kind": "investigate",
        "rationale": "Discover repo test infrastructure before implementing",
        "instructions": [
            "Read pyproject.toml, Makefile, and any config files",
            "Determine the test framework and test command",
            "Report findings via work_complete with a findings dict",
        ],
        "scope_hints": {
            "likely_files": ["pyproject.toml", "Makefile", "setup.cfg"],
            "search_terms": ["test"],
        },
        "acceptance": {
            "must": ["Report test command"],
            "must_not": ["Do not modify files"],
            "gates": [],
        },
        "limits": {"max_diff_lines": 50, "max_commits": 1},
        "commit": {
            "message": "investigate: discover test infrastructure",
            "footer": {
                "VELORA_RUN_ID": "run-integ",
                "VELORA_ITERATION": 1,
                "WORK_ITEM_ID": "WI-INV",
            },
        },
    })


def _make_implement_wi() -> WorkItem:
    return WorkItem.from_dict({
        "id": "WI-IMPL",
        "kind": "implement",
        "rationale": "Add subtract function",
        "instructions": [
            "Add a subtract(a, b) function to mylib/math.py",
            "Add a test for it in tests/test_math.py",
        ],
        "scope_hints": {
            "likely_files": ["mylib/math.py", "tests/test_math.py"],
            "search_terms": ["subtract"],
        },
        "acceptance": {
            "must": ["subtract(5, 3) returns 2"],
            "must_not": [],
            "gates": ["tests"],
        },
        "limits": {"max_diff_lines": 100, "max_commits": 1},
        "commit": {
            "message": "feat: add subtract function",
            "footer": {
                "VELORA_RUN_ID": "run-integ",
                "VELORA_ITERATION": 2,
                "WORK_ITEM_ID": "WI-IMPL",
            },
        },
    })


class TestInvestigateThenImplement(unittest.TestCase):
    """End-to-end: investigate discovers unittest, implement uses it."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.repo = Path(self.tmp)
        _init_unittest_repo(self.repo)
        self.exchange_dir = Path(tempfile.mkdtemp())

    # -- Phase 1: Investigate --

    def test_phase1_investigate_discovers_unittest(self):
        """Simulate an investigator that reads Makefile and discovers unittest."""
        # Mock LLM: list root → read pyproject.toml → read Makefile → work_complete with findings
        responses = [
            CmdResult(0, '{"action": "list_files", "params": {"path": "."}}', ""),
            CmdResult(0, '{"action": "read_file", "params": {"path": "pyproject.toml"}}', ""),
            CmdResult(0, '{"action": "read_file", "params": {"path": "Makefile"}}', ""),
            CmdResult(0, json.dumps({
                "action": "work_complete",
                "params": {
                    "summary": "Repo uses unittest. Test command: python -m unittest discover -s tests -p 'test_*.py'. No pytest.",
                    "findings": {
                        "test_command": "python -m unittest discover -s tests -p test_*.py",
                        "test_framework": "unittest",
                        "test_dirs": ["tests/"],
                    },
                },
            }), ""),
        ]
        wi = _make_investigate_wi()
        with patch("velora.local_worker._call_local_llm_chat", side_effect=responses):
            result = run_local_worker(
                work_item=wi,
                repo_root=self.repo,
                work_branch="velora/wi-inv",
                exchange_dir=self.exchange_dir,
                repo_ref="owner/mylib",
                run_id="run-integ",
                verb="fix",
                objective="add subtract",
                iteration=1,
            )

        self.assertEqual(result.returncode, 0)

        # result.json should exist (not block.json) — investigate succeeded without changes
        result_file = self.exchange_dir / "result.json"
        self.assertTrue(result_file.exists(), "investigate should write result.json, not block.json")
        payload = json.loads(result_file.read_text())
        self.assertEqual(payload["status"], "completed")
        self.assertEqual(payload["files_touched"], [])

        # DISCOVERY evidence should carry the test command
        discovery_entries = [e for e in payload["evidence"] if e.startswith("DISCOVERY:")]
        self.assertEqual(len(discovery_entries), 1)
        discovery = json.loads(discovery_entries[0][len("DISCOVERY:"):])
        self.assertIn("unittest", discovery["test_command"])
        self.assertEqual(discovery["test_framework"], "unittest")

    # -- Phase 2: Implement with discovered commands --

    def test_phase2_implement_uses_discovered_test_command(self):
        """Worker uses discovered unittest command instead of hardcoded pytest."""
        # Simulate the LLM adding subtract() and a test
        new_math = (
            "def add(a: int, b: int) -> int:\n    return a + b\n\n"
            "def subtract(a: int, b: int) -> int:\n    return a - b\n"
        )
        new_test = (
            "import unittest\nfrom mylib.math import add, subtract\n\n"
            "class TestAdd(unittest.TestCase):\n"
            "    def test_add(self):\n"
            "        self.assertEqual(add(1, 2), 3)\n\n"
            "class TestSubtract(unittest.TestCase):\n"
            "    def test_subtract(self):\n"
            "        self.assertEqual(subtract(5, 3), 2)\n\n"
            "if __name__ == '__main__':\n    unittest.main()\n"
        )
        responses = [
            CmdResult(0, '{"action": "read_file", "params": {"path": "mylib/math.py"}}', ""),
            CmdResult(0, '{"action": "read_file", "params": {"path": "tests/test_math.py"}}', ""),
            CmdResult(0, json.dumps({
                "action": "write_file",
                "params": {"path": "mylib/math.py", "content": new_math},
            }), ""),
            CmdResult(0, json.dumps({
                "action": "write_file",
                "params": {"path": "tests/test_math.py", "content": new_test},
            }), ""),
            CmdResult(0, '{"action": "work_complete", "params": {"summary": "Added subtract function and test"}}', ""),
        ]

        wi = _make_implement_wi()
        exchange2 = Path(tempfile.mkdtemp())

        # KEY: pass the discovered test command — this is what the orchestrator would do
        discovered = ["python -m unittest discover -s tests -p test_*.py"]

        with patch("velora.local_worker._call_local_llm_chat", side_effect=responses):
            result = run_local_worker(
                work_item=wi,
                repo_root=self.repo,
                work_branch="velora/wi-impl",
                exchange_dir=exchange2,
                repo_ref="owner/mylib",
                run_id="run-integ",
                verb="fix",
                objective="add subtract",
                iteration=2,
                discovered_test_commands=discovered,
            )

        self.assertEqual(result.returncode, 0)
        result_file = exchange2 / "result.json"
        self.assertTrue(result_file.exists(), "implement should succeed")
        payload = json.loads(result_file.read_text())
        self.assertEqual(payload["status"], "completed")
        self.assertIn("mylib/math.py", payload["files_touched"])
        self.assertIn("tests/test_math.py", payload["files_touched"])

        # Tests should have run with the discovered command
        test_entries = [t for t in payload["tests_run"] if t["status"] == "pass"]
        self.assertTrue(len(test_entries) > 0, "tests should have passed with discovered command")

    # -- Phase 2 negative: without discovery, default pytest would fail --

    def test_phase2_without_discovery_falls_back_to_pytest(self):
        """Without discovered commands, harness uses hardcoded pytest (which may fail)."""
        wi = _make_implement_wi()
        scope = _build_scope(wi, self.repo, "velora/wi-impl")
        # Default: should contain pytest
        self.assertTrue(
            any("pytest" in cmd for cmd in scope.test_commands),
            f"without discovery, should default to pytest but got: {scope.test_commands}",
        )

    def test_phase2_with_discovery_uses_unittest(self):
        """With discovered commands, harness uses unittest."""
        wi = _make_implement_wi()
        discovered = ["python -m unittest discover -s tests -p test_*.py"]
        scope = _build_scope(wi, self.repo, "velora/wi-impl", discovered_test_commands=discovered)
        self.assertEqual(scope.test_commands, discovered)
        self.assertFalse(any("pytest" in cmd for cmd in scope.test_commands))

    # -- Prompt validation --

    def test_investigate_prompt_excludes_mutation_actions(self):
        """Investigate prompt should not offer write/patch/run_tests."""
        wi = _make_investigate_wi()
        prompt = build_local_worker_prompt(
            work_item=wi, repo_ref="owner/mylib",
            work_branch="velora/wi-inv", test_commands=[],
        )
        actions = prompt.split("## Available actions")[1].split("## Rules")[0]
        self.assertIn('"read_file"', actions)
        self.assertIn('"list_files"', actions)
        self.assertNotIn('"write_file"', actions)
        self.assertNotIn('"patch_file"', actions)
        self.assertNotIn('"run_tests"', actions)

    def test_implement_prompt_includes_discovered_commands(self):
        """Implement prompt should show discovered test commands, not default."""
        wi = _make_implement_wi()
        prompt = build_local_worker_prompt(
            work_item=wi, repo_ref="owner/mylib",
            work_branch="velora/wi-impl",
            test_commands=["python -m unittest discover -s tests -p test_*.py"],
        )
        self.assertIn("unittest", prompt)
        self.assertIn("## Test commands available", prompt)


if __name__ == "__main__":
    unittest.main()
