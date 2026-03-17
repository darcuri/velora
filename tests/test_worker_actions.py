import unittest
import tempfile
from pathlib import Path

from velora.worker_actions import (
    WorkerScope,
    resolve_scoped_path,
    ScopeViolation,
    ActionError,
    execute_read_file,
    execute_list_files,
    execute_write_file,
    execute_patch_file,
    execute_search_files,
    execute_run_tests,
    dispatch_action,
    KNOWN_ACTIONS,
)


class TestPathResolution(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.repo = Path(self.tmp)
        self.scope = WorkerScope(
            repo_root=self.repo,
            allowed_files={"src/main.py", "src/util.py", "tests/test_main.py"},
            allowed_dirs={"src", "tests"},
            test_commands=["python -m pytest -q"],
            work_branch="velora/wi-001",
        )

    def test_resolve_valid_allowed_file(self):
        resolved = resolve_scoped_path(self.scope, "src/main.py")
        self.assertEqual(resolved, self.repo / "src/main.py")

    def test_reject_absolute_path(self):
        with self.assertRaises(ScopeViolation):
            resolve_scoped_path(self.scope, "/etc/passwd")

    def test_reject_dot_dot_traversal(self):
        with self.assertRaises(ScopeViolation):
            resolve_scoped_path(self.scope, "src/../../../etc/passwd")

    def test_reject_path_outside_scope(self):
        with self.assertRaises(ScopeViolation):
            resolve_scoped_path(self.scope, "docs/readme.md", require_allowed_file=True)

    def test_allow_file_in_allowed_dir(self):
        # read_file allows any file in allowed_dirs
        resolved = resolve_scoped_path(self.scope, "src/other.py", require_allowed_file=False)
        self.assertEqual(resolved, self.repo / "src/other.py")

    def test_reject_symlink_outside_repo(self):
        # Create a symlink pointing outside repo
        link = self.repo / "src" / "escape"
        link_target = Path("/tmp")
        (self.repo / "src").mkdir(parents=True, exist_ok=True)
        link.symlink_to(link_target)
        with self.assertRaises(ScopeViolation):
            resolve_scoped_path(self.scope, "src/escape")


class TestReadFile(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.repo = Path(self.tmp)
        (self.repo / "src").mkdir()
        (self.repo / "src" / "main.py").write_text("print('hello')\n")
        self.scope = WorkerScope(
            repo_root=self.repo,
            allowed_files={"src/main.py"},
            allowed_dirs={"src"},
            test_commands=[],
            work_branch="velora/wi-001",
        )

    def test_read_existing_file(self):
        result = execute_read_file(self.scope, {"path": "src/main.py"})
        self.assertEqual(result["status"], "ok")
        self.assertIn("print('hello')", result["result"])

    def test_read_nonexistent_file(self):
        result = execute_read_file(self.scope, {"path": "src/missing.py"})
        self.assertEqual(result["status"], "error")

    def test_read_out_of_scope(self):
        result = execute_read_file(self.scope, {"path": "docs/readme.md"})
        self.assertEqual(result["status"], "error")
        self.assertIn("scope", result["result"].lower())


class TestListFiles(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.repo = Path(self.tmp)
        (self.repo / "src").mkdir()
        (self.repo / "src" / "a.py").write_text("")
        (self.repo / "src" / "b.py").write_text("")
        self.scope = WorkerScope(
            repo_root=self.repo,
            allowed_files={"src/a.py"},
            allowed_dirs={"src"},
            test_commands=[],
            work_branch="velora/wi-001",
        )

    def test_list_allowed_dir(self):
        result = execute_list_files(self.scope, {"path": "src"})
        self.assertEqual(result["status"], "ok")
        self.assertIn("a.py", result["result"])
        self.assertIn("b.py", result["result"])

    def test_list_out_of_scope(self):
        result = execute_list_files(self.scope, {"path": "docs"})
        self.assertEqual(result["status"], "error")


class TestWriteFile(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.repo = Path(self.tmp)
        (self.repo / "src").mkdir()
        self.scope = WorkerScope(
            repo_root=self.repo,
            allowed_files={"src/main.py"},
            allowed_dirs={"src"},
            test_commands=[],
            work_branch="velora/wi-001",
        )

    def test_write_new_file_in_scope(self):
        result = execute_write_file(self.scope, {"path": "src/main.py", "content": "x = 1\n"})
        self.assertEqual(result["status"], "ok")
        self.assertEqual((self.repo / "src" / "main.py").read_text(), "x = 1\n")

    def test_write_out_of_scope(self):
        result = execute_write_file(self.scope, {"path": "src/other.py", "content": "x = 1"})
        self.assertEqual(result["status"], "error")
        self.assertIn("scope", result["result"].lower())


class TestPatchFile(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.repo = Path(self.tmp)
        (self.repo / "src").mkdir()
        (self.repo / "src" / "main.py").write_text("def hello():\n    return 'hello'\n")
        self.scope = WorkerScope(
            repo_root=self.repo,
            allowed_files={"src/main.py"},
            allowed_dirs={"src"},
            test_commands=[],
            work_branch="velora/wi-001",
        )

    def test_patch_unique_match(self):
        result = execute_patch_file(self.scope, {
            "path": "src/main.py",
            "old": "return 'hello'",
            "new": "return 'world'",
        })
        self.assertEqual(result["status"], "ok")
        content = (self.repo / "src" / "main.py").read_text()
        self.assertIn("return 'world'", content)

    def test_patch_no_match(self):
        result = execute_patch_file(self.scope, {
            "path": "src/main.py",
            "old": "return 'nonexistent'",
            "new": "return 'world'",
        })
        self.assertEqual(result["status"], "error")
        self.assertIn("not found", result["result"].lower())

    def test_patch_multiple_matches(self):
        (self.repo / "src" / "main.py").write_text("x = 1\nx = 1\n")
        result = execute_patch_file(self.scope, {
            "path": "src/main.py",
            "old": "x = 1",
            "new": "x = 2",
        })
        self.assertEqual(result["status"], "error")
        self.assertIn("not unique", result["result"].lower())


class TestSearchFiles(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.repo = Path(self.tmp)
        (self.repo / "src").mkdir()
        (self.repo / "src" / "main.py").write_text("def hello():\n    return 'hello'\n")
        (self.repo / "src" / "util.py").write_text("def helper():\n    return hello()\n")
        self.scope = WorkerScope(
            repo_root=self.repo,
            allowed_files={"src/main.py"},
            allowed_dirs={"src"},
            test_commands=[],
            work_branch="velora/wi-001",
        )

    def test_search_literal_match(self):
        result = execute_search_files(self.scope, {"pattern": "hello"})
        self.assertEqual(result["status"], "ok")
        self.assertIn("main.py", result["result"])
        self.assertIn("util.py", result["result"])

    def test_search_no_match(self):
        result = execute_search_files(self.scope, {"pattern": "nonexistent_xyz"})
        self.assertEqual(result["status"], "ok")
        self.assertIn("no matches", result["result"].lower())

    def test_search_caps_results(self):
        # Create a file with >50 matches
        (self.repo / "src" / "big.py").write_text("\n".join(f"line{i} match" for i in range(100)))
        result = execute_search_files(self.scope, {"pattern": "match"})
        self.assertEqual(result["status"], "ok")
        # Should be capped
        lines = [l for l in result["result"].strip().split("\n") if l.strip()]
        self.assertLessEqual(len(lines), 51)  # 50 results + possible cap message


class TestRunTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.repo = Path(self.tmp)
        self.scope = WorkerScope(
            repo_root=self.repo,
            allowed_files=set(),
            allowed_dirs=set(),
            test_commands=["python -m pytest -q"],
            work_branch="velora/wi-001",
        )

    def test_reject_command_not_in_allowlist(self):
        result = execute_run_tests(self.scope, {"command": "rm -rf /"})
        self.assertEqual(result["status"], "error")
        self.assertIn("not in allowlist", result["result"].lower())

    def test_accept_valid_command(self):
        # python -m pytest -q will fail (no tests) but should not be rejected by allowlist
        result = execute_run_tests(self.scope, {"command": "python -m pytest -q"})
        # It ran (returned something), not blocked by allowlist
        self.assertIn(result["status"], {"ok", "error"})
        self.assertNotIn("not in allowlist", result["result"].lower())


class TestDispatch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.repo = Path(self.tmp)
        (self.repo / "src").mkdir()
        (self.repo / "src" / "main.py").write_text("x = 1\n")
        self.scope = WorkerScope(
            repo_root=self.repo,
            allowed_files={"src/main.py"},
            allowed_dirs={"src"},
            test_commands=["python -m pytest -q"],
            work_branch="velora/wi-001",
        )

    def test_dispatch_known_action(self):
        result = dispatch_action(self.scope, "read_file", {"path": "src/main.py"})
        self.assertEqual(result["status"], "ok")

    def test_dispatch_unknown_action(self):
        result = dispatch_action(self.scope, "delete_file", {"path": "src/main.py"})
        self.assertEqual(result["status"], "error")
        self.assertIn("unknown action", result["result"].lower())

    def test_known_actions_list(self):
        expected = {"read_file", "list_files", "write_file", "patch_file",
                    "search_files", "run_tests", "run_probe", "work_complete", "work_blocked"}
        self.assertEqual(set(KNOWN_ACTIONS.keys()), expected)


if __name__ == "__main__":
    unittest.main()
