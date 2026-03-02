from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import Any

from .acpx import parse_codex_footer, run_codex, run_gemini_review
from .github import GitHubClient
from .state import upsert_task
from .spec import RunSpec
from .config import get_config
from .util import build_task_id, ensure_dir, now_iso, repo_slug, velora_home

def _allowed_owners() -> set[str]:
    return set(get_config().allowed_owners)


VALID_VERBS = {"feature", "fix", "refactor"}


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


def ensure_repo_checkout(owner: str, repo: str, home: Path | None = None) -> Path:
    base = ensure_dir((home or velora_home()) / "repos")
    checkout = base / repo_slug(owner, repo)
    full_name = f"{owner}/{repo}"
    if not checkout.exists():
        _run_checked(["gh", "repo", "clone", full_name, str(checkout)])
        return checkout

    status = _run_checked(["git", "status", "--porcelain"], cwd=checkout).strip()
    if status:
        raise RuntimeError(f"Local repo is not clean: {checkout}")
    _run_checked(["git", "fetch", "--all", "--prune"], cwd=checkout)
    _run_checked(["git", "pull", "--ff-only"], cwd=checkout)
    return checkout


def _task_title(verb: str, task: str, title_override: str | None = None) -> str:
    if title_override and title_override.strip():
        return title_override.strip()
    return f"[{verb}] {task}".strip()


def _task_body(task_id: str, summary: str, extra_body: str | None) -> str:
    body = f"VELORA task_id: {task_id}\n\n{summary}".strip()
    if extra_body and extra_body.strip():
        body += "\n\n" + extra_body.strip()
    return body + "\n"


def _build_codex_prompt(
    task_id: str,
    repo_ref: str,
    verb: str,
    task_text: str,
    attempt: int,
    fix_context: str | None,
) -> str:
    lines = [
        f"You are working on {repo_ref}.",
        f"Task ID: {task_id}",
        f"Verb: {verb}",
        f"Task: {task_text}",
        f"Attempt: {attempt}",
        "",
        "Requirements:",
        "- Checkout branch velora/" + task_id + " (create it if it does not exist)",
        "- If a PR already exists for this task, continue pushing to the same branch (do not open a new PR)",
        "- Implement requested change",
        "- Run local checks/tests",
        "- Commit and push the branch",
        "- Print this machine-readable footer exactly:",
        "BRANCH: <branch-name>",
        "HEAD_SHA: <commit-sha>",
        "SUMMARY: <one-line summary>",
    ]
    if fix_context:
        lines.extend(["", "Context to fix:", fix_context])
    return "\n".join(lines)


def _append_text(path: Path, text: str) -> None:
    with path.open("a", encoding="utf-8") as fh:
        fh.write(text)
        if not text.endswith("\n"):
            fh.write("\n")


def _write_text(path: Path, text: str) -> None:
    with path.open("w", encoding="utf-8") as fh:
        fh.write(text)
        if not text.endswith("\n"):
            fh.write("\n")


def _poll_ci(
    gh: GitHubClient,
    owner: str,
    repo: str,
    head_sha: str,
    ci_log_path: Path,
    poll_seconds: int = 30,
    stuck_warn_seconds: int = 15 * 60,
    stuck_fail_seconds: int = 30 * 60,
) -> tuple[str, str]:
    last_snapshot = ""
    last_progress = time.time()
    warned = False

    while True:
        state, detail = gh.get_ci_state(owner, repo, head_sha)
        snapshot = f"{state}:{detail}"
        if snapshot != last_snapshot:
            last_snapshot = snapshot
            last_progress = time.time()
            warned = False
            _append_text(ci_log_path, f"[{now_iso()}] progress {snapshot}")
        if state in {"success", "failure"}:
            return state, detail

        idle = time.time() - last_progress
        if idle >= stuck_warn_seconds and not warned:
            warned = True
            _append_text(ci_log_path, f"[{now_iso()}] warning no progress for {int(idle)} seconds")
        if idle >= stuck_fail_seconds:
            _append_text(ci_log_path, f"[{now_iso()}] failure no progress for {int(idle)} seconds")
            return "failure", "stuck-no-progress"

        time.sleep(poll_seconds)


def _read_diff_for_review(repo_path: Path, base_ref: str, head_sha: str) -> str:
    return _run_checked(["git", "diff", f"origin/{base_ref}...{head_sha}"], cwd=repo_path)


def run_task(repo_ref: str, verb: str, spec: RunSpec, home: Path | None = None) -> dict[str, Any]:
    if verb not in VALID_VERBS:
        raise ValueError(f"Invalid verb: {verb}. Allowed: {', '.join(sorted(VALID_VERBS))}")

    task_text = spec.task

    owner, repo = validate_repo_allowed(repo_ref)
    gh = GitHubClient.from_env()
    base_branch = gh.get_default_branch(owner, repo)
    repo_path = ensure_repo_checkout(owner, repo, home=home)

    base_home = home or velora_home()
    task_id = build_task_id()
    task_dir = ensure_dir(base_home / "tasks" / task_id)
    prompt_path = task_dir / "prompt.txt"
    agent_output_path = task_dir / "agent-output.txt"
    review_path = task_dir / "review.txt"

    record: dict[str, Any] = {
        "task_id": task_id,
        "repo": repo_ref,
        "verb": verb,
        "task": task_text,
        "status": "running",
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "pr_url": None,
        "pr_number": None,
        "branch": None,
        "head_sha": None,
        "summary": None,
    }
    upsert_task(record, home=base_home)

    cfg = get_config()
    session_name = f"{cfg.codex_session_prefix}{repo_slug(owner, repo)}"
    fix_context: str | None = None
    max_attempts = spec.max_attempts if spec.max_attempts is not None else cfg.max_attempts
    max_attempts = max(1, min(int(max_attempts), 10))

    for attempt in range(1, max_attempts + 1):
        prompt = _build_codex_prompt(task_id, repo_ref, verb, task_text, attempt, fix_context)
        if attempt == 1:
            _write_text(prompt_path, prompt)
        else:
            _append_text(prompt_path, f"\n---- attempt {attempt} ----\n{prompt}")

        codex_result = run_codex(session_name=session_name, cwd=repo_path, prompt=prompt)
        _append_text(
            agent_output_path,
            f"---- attempt {attempt} rc={codex_result.returncode} ----\n{codex_result.stdout}\n{codex_result.stderr}",
        )
        if codex_result.returncode != 0:
            raise RuntimeError(f"acpx codex failed on attempt {attempt}: {codex_result.stderr.strip()}")

        footer = parse_codex_footer(codex_result.stdout)
        record["branch"] = footer["branch"]
        record["head_sha"] = footer["head_sha"]
        record["summary"] = footer["summary"]
        record["updated_at"] = now_iso()
        upsert_task(record, home=base_home)

        if attempt == 1:
            pr = gh.create_pull_request(
                owner=owner,
                repo=repo,
                title=_task_title(verb, task_text, spec.title),
                body=_task_body(task_id, footer["summary"], spec.body),
                head=footer["branch"],
                base=base_branch,
            )
            record["pr_url"] = pr["html_url"]
            record["pr_number"] = pr["number"]
            record["updated_at"] = now_iso()
            upsert_task(record, home=base_home)

        ci_log = task_dir / f"ci-attempt-{attempt}.log"
        _append_text(ci_log, f"[{now_iso()}] polling CI for {footer['head_sha']}")
        ci_state, ci_detail = _poll_ci(gh, owner, repo, footer["head_sha"], ci_log)
        _append_text(ci_log, f"[{now_iso()}] final {ci_state}: {ci_detail}")

        if ci_state != "success":
            if attempt == max_attempts:
                record["status"] = "failed"
                record["updated_at"] = now_iso()
                record["failure_reason"] = (
                    f"FIRE exhausted after {max_attempts} attempts; last CI detail: {ci_detail}"
                )
                upsert_task(record, home=base_home)
                return {
                    "task_id": task_id,
                    "status": record["status"],
                    "pr_url": record["pr_url"],
                    "summary": record["failure_reason"],
                    "ci_state": ci_state,
                    "ci_detail": ci_detail,
                }
            fix_context = f"Attempt {attempt} CI failure detail: {ci_detail}"
            continue

        # CI success → review gate (and FIRE it too).
        diff_text = _read_diff_for_review(repo_path, base_branch, str(record["head_sha"]))
        gemini = run_gemini_review(diff_text)
        review_text = gemini.stdout.strip()
        if gemini.returncode != 0:
            review_text = f"BLOCKER: Review tool failed: {gemini.stderr.strip()}"

        review_attempt_path = task_dir / f"review-attempt-{attempt}.txt"
        _write_text(review_attempt_path, review_text)

        if record["pr_number"] is None:
            raise RuntimeError("PR number missing; cannot post review comment")
        gh.post_issue_comment(owner, repo, int(record["pr_number"]), review_text)

        if gemini.returncode != 0:
            record["status"] = "not-ready"
            record["updated_at"] = now_iso()
            upsert_task(record, home=base_home)
            return {
                "task_id": task_id,
                "status": record["status"],
                "pr_url": record["pr_url"],
                "summary": record["summary"],
                "ci_state": ci_state,
                "ci_detail": ci_detail,
                "review": review_text,
            }

        if "BLOCKER" in review_text:
            if attempt == max_attempts:
                record["status"] = "not-ready"
                record["updated_at"] = now_iso()
                upsert_task(record, home=base_home)
                return {
                    "task_id": task_id,
                    "status": record["status"],
                    "pr_url": record["pr_url"],
                    "summary": record["summary"],
                    "ci_state": ci_state,
                    "ci_detail": ci_detail,
                    "review": review_text,
                }

            fix_context = f"Attempt {attempt} review blockers to address:\n{review_text}"
            continue

        record["status"] = "ready"
        record["updated_at"] = now_iso()
        upsert_task(record, home=base_home)
        return {
            "task_id": task_id,
            "status": record["status"],
            "pr_url": record["pr_url"],
            "summary": record["summary"],
            "ci_state": ci_state,
            "ci_detail": ci_detail,
            "review": review_text,
        }

    # Should be unreachable.
    record["status"] = "failed"
    record["updated_at"] = now_iso()
    record["failure_reason"] = "FIRE exhausted"
    upsert_task(record, home=base_home)
    return {
        "task_id": task_id,
        "status": record["status"],
        "pr_url": record["pr_url"],
        "summary": record["failure_reason"],
    }
