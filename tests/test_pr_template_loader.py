import tempfile
import unittest
from pathlib import Path

from velora.run import _load_repo_pr_template


class TestPrTemplateLoader(unittest.TestCase):
    def test_root_template_wins(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            (repo / "PULL_REQUEST_TEMPLATE.md").write_text("root template\n", encoding="utf-8")
            (repo / ".github").mkdir()
            (repo / ".github" / "PULL_REQUEST_TEMPLATE.md").write_text("github template\n", encoding="utf-8")
            txt = _load_repo_pr_template(repo)
            self.assertEqual(txt.strip(), "root template")

    def test_directory_default_md_supported(self):
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            d = repo / ".github" / "PULL_REQUEST_TEMPLATE"
            d.mkdir(parents=True)
            (d / "default.md").write_text("dir default\n", encoding="utf-8")
            txt = _load_repo_pr_template(repo)
            self.assertEqual(txt.strip(), "dir default")


if __name__ == "__main__":
    unittest.main()
