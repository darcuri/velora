import os
import unittest
from unittest.mock import patch

from velora.config import get_config
from velora.run import validate_repo_allowed


class TestRepoAllowlist(unittest.TestCase):
    def setUp(self):
        get_config.cache_clear()

    def tearDown(self):
        get_config.cache_clear()

    def test_rejects_when_allowlist_missing(self):
        # Defaults are intentionally empty (default-deny).
        with self.assertRaises(ValueError):
            validate_repo_allowed("octocat/hello-world")

    def test_rejects_owner_not_in_allowlist(self):
        with patch.dict(os.environ, {"VELORA_ALLOWED_OWNERS": "octocat"}, clear=False):
            get_config.cache_clear()
            with self.assertRaises(ValueError):
                validate_repo_allowed("someoneelse/hello-world")

    def test_allows_owner_from_env(self):
        with patch.dict(os.environ, {"VELORA_ALLOWED_OWNERS": "octocat"}, clear=False):
            get_config.cache_clear()
            owner, repo = validate_repo_allowed("octocat/hello-world")
            self.assertEqual(owner, "octocat")
            self.assertEqual(repo, "hello-world")


if __name__ == "__main__":
    unittest.main()
