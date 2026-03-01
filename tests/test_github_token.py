import subprocess
import unittest
from unittest import mock

from velora.github import resolve_github_token


class TestGitHubTokenResolution(unittest.TestCase):
    def test_prefers_velora_token(self):
        env = {"VELORA_GITHUB_TOKEN": "v1", "GH_TOKEN": "v2"}
        runner = mock.Mock()
        self.assertEqual(resolve_github_token(env=env, runner=runner), "v1")
        runner.assert_not_called()

    def test_falls_back_to_gh_token(self):
        env = {"GH_TOKEN": "v2"}
        runner = mock.Mock()
        self.assertEqual(resolve_github_token(env=env, runner=runner), "v2")
        runner.assert_not_called()

    def test_uses_gh_auth_token_last(self):
        env = {}
        completed = subprocess.CompletedProcess(
            args=["gh", "auth", "token"],
            returncode=0,
            stdout=" from-gh \n",
            stderr="",
        )
        runner = mock.Mock(return_value=completed)
        self.assertEqual(resolve_github_token(env=env, runner=runner), "from-gh")
        runner.assert_called_once()


if __name__ == "__main__":
    unittest.main()

