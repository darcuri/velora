import os
import unittest
from unittest.mock import patch

from velora.run import validate_repo_allowed


class TestRepoAllowlist(unittest.TestCase):
    def test_allows_darcuri_repo_by_default(self):
        owner, repo = validate_repo_allowed("darcuri/velora")
        self.assertEqual(owner, "darcuri")
        self.assertEqual(repo, "velora")

    def test_rejects_other_owner_by_default(self):
        with self.assertRaises(ValueError):
            validate_repo_allowed("octocat/hello-world")

    def test_allows_owner_from_env(self):
        with patch.dict(os.environ, {"VELORA_ALLOWED_OWNERS": "octocat"}, clear=False):
            owner, repo = validate_repo_allowed("octocat/hello-world")
            self.assertEqual(owner, "octocat")
            self.assertEqual(repo, "hello-world")


if __name__ == "__main__":
    unittest.main()
