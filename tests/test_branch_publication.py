import subprocess
import tempfile
import unittest
from pathlib import Path

from velora.run import _publish_branch


class TestBranchPublication(unittest.TestCase):
    def test_publish_branch_pushes_expected_head_to_origin(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            remote = root / "remote.git"
            repo = root / "repo"

            subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True, text=True)
            subprocess.run(["git", "init", str(repo)], check=True, capture_output=True, text=True)
            subprocess.run(["git", "config", "user.name", "Velora Test"], cwd=repo, check=True, capture_output=True, text=True)
            subprocess.run(["git", "config", "user.email", "velora-test@example.com"], cwd=repo, check=True, capture_output=True, text=True)
            subprocess.run(["git", "remote", "add", "origin", str(remote)], cwd=repo, check=True, capture_output=True, text=True)

            (repo / "README.md").write_text("base\n", encoding="utf-8")
            subprocess.run(["git", "add", "README.md"], cwd=repo, check=True, capture_output=True, text=True)
            subprocess.run(["git", "commit", "-m", "base"], cwd=repo, check=True, capture_output=True, text=True)
            subprocess.run(["git", "branch", "-M", "main"], cwd=repo, check=True, capture_output=True, text=True)
            subprocess.run(["git", "push", "-u", "origin", "main"], cwd=repo, check=True, capture_output=True, text=True)

            subprocess.run(["git", "checkout", "-b", "velora/task123"], cwd=repo, check=True, capture_output=True, text=True)
            (repo / "README.md").write_text("base\nchange\n", encoding="utf-8")
            subprocess.run(["git", "add", "README.md"], cwd=repo, check=True, capture_output=True, text=True)
            subprocess.run(["git", "commit", "-m", "worker result"], cwd=repo, check=True, capture_output=True, text=True)
            head_sha = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=repo,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()

            _publish_branch(repo_path=repo, branch="velora/task123", expected_head_sha=head_sha)

            remote_sha = subprocess.run(
                ["git", "ls-remote", "--heads", "origin", "velora/task123"],
                cwd=repo,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip().split()[0]

        self.assertEqual(remote_sha, head_sha)


if __name__ == "__main__":
    unittest.main()
