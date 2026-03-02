from __future__ import annotations

import json
import os
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable


def resolve_github_token(
    env: dict[str, str] | None = None,
    runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> str:
    env_map = env if env is not None else os.environ
    token = env_map.get("VELORA_GITHUB_TOKEN")
    if token:
        return token.strip()
    token = env_map.get("GH_TOKEN")
    if token:
        return token.strip()

    run = runner or subprocess.run
    result = run(
        ["gh", "auth", "token"],
        check=True,
        capture_output=True,
        text=True,
    )
    token = result.stdout.strip()
    if not token:
        raise RuntimeError("GitHub token lookup returned empty output")
    return token


class GitHubClient:
    def __init__(self, token: str, api_base: str = "https://api.github.com") -> None:
        self._token = token
        self._api_base = api_base.rstrip("/")

    @classmethod
    def from_env(cls) -> "GitHubClient":
        return cls(resolve_github_token())

    def _request(
        self,
        method: str,
        path: str,
        data: dict[str, Any] | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> Any:
        url = f"{self._api_base}{path}"
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self._token}",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if extra_headers:
            headers.update(extra_headers)
        body: bytes | None = None
        if data is not None:
            body = json.dumps(data).encode("utf-8")
            headers["Content-Type"] = "application/json"

        req = urllib.request.Request(url=url, method=method, headers=headers, data=body)
        try:
            with urllib.request.urlopen(req) as resp:  # nosec B310 (controlled URL)
                payload = resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"GitHub API {method} {path} failed: {exc.code} {detail}") from exc
        return json.loads(payload) if payload else {}

    def create_pull_request(
        self,
        owner: str,
        repo: str,
        title: str,
        body: str,
        head: str,
        base: str = "main",
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/repos/{owner}/{repo}/pulls",
            data={"title": title, "body": body, "head": head, "base": base},
        )

    def get_repo(self, owner: str, repo: str) -> dict[str, Any]:
        return self._request("GET", f"/repos/{owner}/{repo}")

    def get_default_branch(self, owner: str, repo: str) -> str:
        payload = self.get_repo(owner, repo)
        default_branch = payload.get("default_branch", "")
        if not default_branch:
            raise RuntimeError(f"GitHub repo metadata missing default branch for {owner}/{repo}")
        return str(default_branch)

    def post_issue_comment(self, owner: str, repo: str, issue_number: int, body: str) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/repos/{owner}/{repo}/issues/{issue_number}/comments",
            data={"body": body},
        )

    def get_combined_status(self, owner: str, repo: str, sha: str) -> dict[str, Any]:
        return self._request("GET", f"/repos/{owner}/{repo}/commits/{sha}/status")

    def get_check_runs(self, owner: str, repo: str, sha: str) -> dict[str, Any]:
        return self._request(
            "GET",
            f"/repos/{owner}/{repo}/commits/{sha}/check-runs",
            extra_headers={"Accept": "application/vnd.github+json"},
        )

    def get_ci_state(self, owner: str, repo: str, sha: str) -> tuple[str, str]:
        status = self.get_combined_status(owner, repo, sha)
        checks = self.get_check_runs(owner, repo, sha)
        status_state = status.get("state", "pending")

        check_runs = checks.get("check_runs", [])
        conclusions = []
        pending = False
        for run in check_runs:
            run_status = run.get("status")
            run_conclusion = run.get("conclusion")
            if run_status != "completed":
                pending = True
            conclusions.append((run.get("name", "unnamed"), run_status, run_conclusion))

        failure_conclusions = {"failure", "timed_out", "cancelled", "action_required", "startup_failure", "stale"}
        if status_state == "failure":
            return "failure", f"combined-status={status_state}"
        if any(conclusion in failure_conclusions for _, _, conclusion in conclusions):
            return "failure", f"check-runs={conclusions}"

        # GitHub Actions often reports check-runs without setting commit statuses.
        # If all check-runs are completed + successful, treat CI as success even if
        # the combined status endpoint still says "pending".
        if check_runs and not pending and all(conclusion == "success" for _, _, conclusion in conclusions):
            return "success", f"check-runs-success={len(check_runs)}"

        if status_state == "success" and not check_runs:
            return "success", "combined-status-success"
        return "pending", f"combined-status={status_state}; checks={conclusions}"
