import unittest
import tempfile
from pathlib import Path

from velora.worker_actions import WorkerScope, resolve_scoped_path, ScopeViolation


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


if __name__ == "__main__":
    unittest.main()
