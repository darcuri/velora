import unittest

from velora.run import validate_repo_allowed


class TestRepoAllowlist(unittest.TestCase):
    def test_allows_darcuri_repo(self):
        owner, repo = validate_repo_allowed("darcuri/velora")
        self.assertEqual(owner, "darcuri")
        self.assertEqual(repo, "velora")

    def test_rejects_other_owner(self):
        with self.assertRaises(ValueError):
            validate_repo_allowed("octocat/hello-world")


if __name__ == "__main__":
    unittest.main()

