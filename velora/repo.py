from __future__ import annotations

"""Repo checkout + allowlist helpers."""

import subprocess
from pathlib import Path

from .config import get_config
from .github import GitHubClient
from .util import ensure_dir, repo_slug, velora_home


def _allowed_owners() -> set[str]:
    return set(get_config().allowed_owners)


def validate_repo_allowed(repo_ref: str) -> tuple[str, str]:
    parts = repo_ref.split("/")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise ValueError("Repo must be in owner/repo format")
    owner, repo = parts
    allowed = _allowed_owners()
    if not allowed:
        raise ValueError(
            "No allowed owners configured. Set allowed_owners in config.json or set VELORA_ALLOWED_OWNERS "
            "(comma-separated, e.g. VELORA_ALLOWED_OWNERS=octocat)."
        )
    if owner not in allowed:
        allowed_str = ", ".join(sorted(allowed))
        raise ValueError(f"Repository owner is not allowed in v0 (allowed: {allowed_str}/*)")
    return owner, repo


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


def _resolve_origin_head_branch(checkout: Path) -> str | None:
    try:
        ref = _run_checked(["git", "symbolic-ref", "refs/remotes/origin/HEAD"], cwd=checkout).strip()
    except Exception:  # noqa: BLE001
        return None

    prefix = "refs/remotes/origin/"
    if ref.startswith(prefix):
        name = ref[len(prefix) :].strip()
        return name or None
    return None


def ensure_repo_checkout(owner: str, repo: str, home: Path | None = None, *, base_branch: str | None = None) -> Path:
    base = ensure_dir((home or velora_home()) / "repos")
    checkout = base / repo_slug(owner, repo)
    full_name = f"{owner}/{repo}"
    if not checkout.exists():
        _run_checked(["gh", "repo", "clone", full_name, str(checkout)])
        # If the caller requested a non-default base branch, ensure we are on it.
        bb = (base_branch or "").strip()
        if bb:
            _run_checked(["git", "fetch", "--all", "--prune"], cwd=checkout)
            _run_checked(["git", "checkout", "-B", bb, f"origin/{bb}"], cwd=checkout)
        return checkout

    status = _run_checked(["git", "status", "--porcelain"], cwd=checkout).strip()
    if status:
        raise RuntimeError(f"Local repo is not clean: {checkout}")

    _run_checked(["git", "fetch", "--all", "--prune"], cwd=checkout)

    bb = (base_branch or "").strip() or _resolve_origin_head_branch(checkout) or "main"
    _run_checked(["git", "checkout", "-B", bb, f"origin/{bb}"], cwd=checkout)

    return checkout


def get_default_branch(owner: str, repo: str) -> str:
    gh = GitHubClient.from_env()
    return gh.get_default_branch(owner, repo)
