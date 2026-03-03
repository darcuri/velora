from __future__ import annotations

"""Orchestration helpers (Mode A).

This module builds the coordinator input (CoordinatorRequest) from a Velora task.

For now, this is intentionally conservative: it produces a minimal, truthful snapshot
of what Velora knows at the moment.
"""

import subprocess
from pathlib import Path
from typing import Any

from .config import get_config
from .github import GitHubClient
from .spec import RunSpec
from .run import VALID_VERBS, ensure_repo_checkout, validate_repo_allowed
from .util import build_task_id, now_iso, repo_slug, velora_home


def coordinator_session_name(owner: str, repo: str) -> str:
    cfg = get_config()
    return f"{cfg.claude_session_prefix}{repo_slug(owner, repo)}-coord"


def _run_checked(cmd: list[str], cwd: Path | None = None) -> str:
    proc = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        text=True,
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"Command failed: {' '.join(cmd)}\n{proc.stderr.strip()}")
    return proc.stdout


def build_initial_coordinator_request(
    repo_ref: str,
    verb: str,
    spec: RunSpec,
    *,
    home: Path | None = None,
) -> tuple[dict[str, Any], Path]:
    """Build the initial CoordinatorRequest and ensure a clean repo checkout.

    Returns: (request_json, repo_path)
    """

    if verb not in VALID_VERBS:
        raise ValueError(f"Invalid verb: {verb}. Allowed: {', '.join(sorted(VALID_VERBS))}")

    owner, repo = validate_repo_allowed(repo_ref)

    gh = GitHubClient.from_env()
    default_branch = gh.get_default_branch(owner, repo)

    repo_path = ensure_repo_checkout(owner, repo, home=home)

    head_sha = _run_checked(["git", "rev-parse", "HEAD"], cwd=repo_path).strip()
    status = _run_checked(["git", "status", "--porcelain"], cwd=repo_path).strip()
    working_tree_clean = not bool(status)

    base_home = home or velora_home()
    run_id = build_task_id()

    request: dict[str, Any] = {
        "protocol_version": 1,
        "run_id": run_id,
        "iteration": 1,
        "objective": spec.task,
        "repo": {
            "owner": owner,
            "name": repo,
            "default_branch": default_branch,
            "work_branch": f"velora/{run_id}",
        },
        "policy": {
            "max_cost_usd": 20,
            "no_progress_max": 4,
            "allow_self_merge": False,
            "required_gates": ["tests", "security"],
        },
        "state": {
            "working_tree_clean": working_tree_clean,
            "last_commit": head_sha,
            "diff_summary": "",
            "notes": [f"created_at={now_iso()}", f"verb={verb}"],
        },
        "evaluation": {
            "status": "none",
            "failing_checks": [],
            "logs_excerpt": "",
        },
        "history": {
            "work_items_executed": [],
            "no_progress_streak": 0,
            "cost_usd_estimate": 0.0,
        },
    }

    return request, repo_path
