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


if __name__ == "__main__":
    unittest.main()
