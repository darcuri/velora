from __future__ import annotations

import os
import shutil
import subprocess
import time
import hashlib
from pathlib import Path
from typing import Any

from .acpx import parse_codex_footer, run_claude, run_codex, run_gemini_review
from .config import get_config
from .constants import VALID_VERBS
from .coordinator import run_coordinator_v1
from .github import GitHubClient
from .orchestrator import coordinator_session_name
from .repo import ensure_repo_checkout, validate_repo_allowed
from .spec import RunSpec
from .state import get_task, upsert_task
from .util import build_task_id, ensure_dir, now_iso, repo_slug, velora_home
from .worker_prompt import build_worker_prompt_v1


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


def _task_title(verb: str, task: str, title_override: str | None = None) -> str:
    if title_override and title_override.strip():
        return title_override.strip()
    return f"[{verb}] {task}".strip()


def _task_body(task_id: str, summary: str, extra_body: str | None) -> str:
    body = f"VELORA task_id: {task_id}\n\n{summary}".strip()
    if extra_body and extra_body.strip():
        body += "\n\n" + extra_body.strip()
    return body + "\n"


def _mode_a_status_for_terminal_decision(decision: str) -> str:
    if decision == "finalize_success":
        return "ready"
    if decision == "stop_failure":
        return "failed"
    raise ValueError(f"Unsupported terminal decision: {decision}")


def _is_oscillating_failure_signatures(sigs: list[str]) -> bool:
    """Detect a simple ABAB oscillation in the last 4 failure signatures."""

    if len(sigs) < 4:
        return False
    a, b, c, d = sigs[-4:]
    return a == c and b == d and a != b


def _truncate_text(text: str, limit: int) -> str:
    text = " ".join(str(text or "").split())
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    if limit < 3:
        return text[:limit]
    if limit == 3:
        return "..."
    keep = max(0, limit - 3)
    cut = text[:keep]
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    out = (cut or text[:keep]) + "..."
    return out[:limit]


def _parse_failing_check_runs_payload(payload: dict[str, Any]) -> tuple[list[dict[str, str]], str]:
    runs = payload.get("check_runs", [])
    if not isinstance(runs, list):
        return [], ""

    failure_conclusions = {"failure", "timed_out", "cancelled", "action_required", "startup_failure", "stale"}
    pairs: list[tuple[str, str]] = []
    failing: list[dict[str, str]] = []
    for run in runs:
        if not isinstance(run, dict):
            continue
        conclusion = str(run.get("conclusion") or "none").strip().lower()
        if conclusion not in failure_conclusions:
            continue
        name = _truncate_text(str(run.get("name") or "unnamed-check"), 120)
        status = str(run.get("status") or "unknown").strip().lower()
        url = str(run.get("details_url") or run.get("html_url") or "")
        output = run.get("output")
        title = ""
        summary = ""
        if isinstance(output, dict):
            title = _truncate_text(str(output.get("title") or ""), 160)
            summary = _truncate_text(str(output.get("summary") or ""), 240)
        summary_parts = [f"status={status}", f"conclusion={conclusion}"]
        if title:
            summary_parts.append(f"title={title}")
        if summary:
            summary_parts.append(f"summary={summary}")
        failing.append({"name": name, "kind": "ci", "url": url, "summary": _truncate_text("; ".join(summary_parts), 420)})
        pairs.append((name.lower(), conclusion))

    if not pairs:
        return failing, ""
    sig_base = "|".join(f"{name}:{conclusion}" for name, conclusion in sorted(pairs))
    digest = hashlib.sha256(sig_base.encode("utf-8")).hexdigest()[:16]
    return failing, f"checks-{len(pairs)}-{digest}"


def _build_ci_logs_excerpt(failing_checks: list[dict[str, str]], max_checks: int = 3, max_chars: int = 600) -> str:
    if not failing_checks:
        return ""
    lines: list[str] = []
    shown = 0
    for check in failing_checks[: max(1, max_checks)]:
        name = _truncate_text(str(check.get("name") or "unnamed-check"), 80)
        summary = _truncate_text(str(check.get("summary") or ""), 180)
        lines.append(f"- {name}: {summary}".strip())
        shown += 1
    remaining = max(0, len(failing_checks) - shown)
    if not remaining:
        return _truncate_text("\n".join(lines), max_chars)

    marker = f"(+{remaining} more failing checks)"
    budget = max(0, max_chars - len(marker) - 1)
    body = _truncate_text("\n".join(lines), budget)
    if not body:
        return _truncate_text(marker, max_chars)
    return f"{body}\n{marker}"


def _digest_failure_detail(detail: str) -> str:
    digest = hashlib.sha256(str(detail).encode("utf-8")).hexdigest()[:16]
    return f"detail-{digest}"


def _footer_head_sha(footer: object) -> str:
    if not isinstance(footer, dict):
        return ""
    return str(footer.get("head_sha") or "").strip()


def _build_codex_prompt(
    task_id: str,
    repo_ref: str,
    verb: str,
    task_text: str,
    attempt: int,
    fix_context: str | None,
) -> str:
    # Legacy prompt (pre-coordinator path).
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


def _cleanup_repo_detritus(repo_path: Path) -> None:
    """Best-effort cleanup of common untracked junk.

    This prevents Velora's repo cleanliness preflight from getting tripped by things
    like __pycache__ and .pytest_cache.

    We intentionally only remove well-known, safe-to-delete directories.
    """

    candidates = [
        "__pycache__",
        "tests/__pycache__",
        ".pytest_cache",
    ]
    for rel in candidates:
        p = repo_path / rel
        if p.exists():
            shutil.rmtree(p, ignore_errors=True)


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


def _format_preflight_error(exc: Exception) -> str:
    msg = str(exc).strip() or exc.__class__.__name__

    # GitHub transient clone/fetch issues.
    if "requested URL returned error: 500" in msg or "Internal Server Error" in msg or "HTTP 500" in msg:
        return (
            "GitHub returned HTTP 500 during repo sync (clone/fetch/pull). "
            "This appears transient/outside Velora. Retry in a few minutes. "
            f"(detail: {msg})"
        )

    return msg


def _fail_task(record: dict[str, Any], *, home: Path, task_dir: Path, detail: str) -> dict[str, Any]:
    """Mark a task record as failed and write an error artifact.

    This is used to avoid "silent" failures where the process dies after creating a task.
    """

    record["status"] = "failed"
    record["updated_at"] = now_iso()
    record["failure_reason"] = detail
    upsert_task(record, home=home)

    try:
        _write_text(task_dir / "error.txt", detail)
    except Exception:
        # Best-effort; never mask the original failure.
        pass

    return {
        "task_id": record.get("task_id"),
        "status": record.get("status"),
        "pr_url": record.get("pr_url"),
        "summary": detail,
    }


def run_task(
    repo_ref: str,
    verb: str,
    spec: RunSpec,
    home: Path | None = None,
    runner: str | None = None,
    *,
    use_coordinator: bool = False,
) -> dict[str, Any]:
    """Run a VELORA task.

    - Legacy mode (default): direct worker prompt (Codex/Claude) + FIRE loop.
    - Mode A (use_coordinator=True): Coordinator (control-plane) emits WorkItems;
      workers execute; CI + review feed back into coordinator.
    """

    if use_coordinator:
        return run_task_mode_a(repo_ref, verb, spec, home=home)
    return run_task_legacy(repo_ref, verb, spec, home=home, runner=runner)


def resume_task(task_id: str, home: Path | None = None) -> dict[str, Any]:
    """Resume a previously started task.

    v0 scope: take an existing branch/commit and finish the remaining pipeline:
    ensure PR exists → poll CI → run review → set final status.

    This is designed to recover from transient failures (GitHub 500s, network hiccups,
    process interruptions) without starting a new task/branch.
    """

    base_home = home or velora_home()
    task = get_task(task_id, home=base_home)
    if task is None:
        raise ValueError(f"Unknown task_id: {task_id}")

    repo_ref = str(task.get("repo") or "")
    verb = str(task.get("verb") or "")
    task_text = str(task.get("task") or "")
    if not repo_ref or not verb or not task_text:
        raise ValueError(f"Task record missing required fields (repo/verb/task): {task_id}")

    owner, repo = validate_repo_allowed(repo_ref)
    gh = GitHubClient.from_env()
    base_branch = gh.get_default_branch(owner, repo)

    repo_path = ensure_repo_checkout(owner, repo, home=home)

    branch = str(task.get("branch") or f"velora/{task_id}")
    _run_checked(["git", "checkout", branch], cwd=repo_path)

    head_sha = str(task.get("head_sha") or "").strip()
    if not head_sha:
        head_sha = _run_checked(["git", "rev-parse", "HEAD"], cwd=repo_path).strip()
        task["head_sha"] = head_sha

    summary = str(task.get("summary") or "(resume)").strip()

    task_dir = ensure_dir(base_home / "tasks" / task_id)

    # Create PR if missing.
    if not task.get("pr_number"):
        pr = gh.create_pull_request(
            owner=owner,
            repo=repo,
            title=_task_title(verb, task_text, None),
            body=_task_body(task_id, summary, None),
            head=branch,
            base=base_branch,
        )
        task["pr_url"] = pr["html_url"]
        task["pr_number"] = pr["number"]

    # Poll CI.
    ci_log = task_dir / "ci-resume.log"
    _append_text(ci_log, f"[{now_iso()}] resuming CI poll for {head_sha}")
    ci_state, ci_detail = _poll_ci(gh, owner, repo, head_sha, ci_log)
    _append_text(ci_log, f"[{now_iso()}] final {ci_state}: {ci_detail}")

    if ci_state != "success":
        task["status"] = "not-ready"
        task["updated_at"] = now_iso()
        task["failure_reason"] = f"CI not successful on resume: {ci_detail}"
        upsert_task(task, home=base_home)
        return {
            "task_id": task_id,
            "status": task["status"],
            "pr_url": task.get("pr_url"),
            "summary": task.get("failure_reason"),
            "ci_state": ci_state,
            "ci_detail": ci_detail,
        }

    # Review gate.
    diff_text = _read_diff_for_review(repo_path, base_branch, head_sha)
    gemini = run_gemini_review(diff_text)
    review_text = gemini.stdout.strip()
    if gemini.returncode != 0:
        review_text = f"BLOCKER: Review tool failed: {gemini.stderr.strip()}"

    review_path = task_dir / "review-resume.txt"
    _write_text(review_path, review_text)

    pr_number = int(task["pr_number"])
    gh.post_issue_comment(owner, repo, pr_number, review_text)

    if gemini.returncode != 0 or "BLOCKER" in review_text:
        task["status"] = "not-ready"
    else:
        task["status"] = "ready"

    task["updated_at"] = now_iso()
    upsert_task(task, home=base_home)

    return {
        "task_id": task_id,
        "status": task["status"],
        "pr_url": task.get("pr_url"),
        "summary": task.get("summary"),
        "ci_state": ci_state,
        "ci_detail": ci_detail,
        "review": review_text,
    }


def run_task_legacy(
    repo_ref: str,
    verb: str,
    spec: RunSpec,
    home: Path | None = None,
    runner: str | None = None,
) -> dict[str, Any]:
    if verb not in VALID_VERBS:
        raise ValueError(f"Invalid verb: {verb}. Allowed: {', '.join(sorted(VALID_VERBS))}")

    task_text = spec.task

    base_home = home or velora_home()
    task_id = build_task_id()
    task_dir = ensure_dir(base_home / "tasks" / task_id)
    prompt_path = task_dir / "prompt.txt"
    agent_output_path = task_dir / "agent-output.txt"

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

    try:
        owner, repo = validate_repo_allowed(repo_ref)
        gh = GitHubClient.from_env()
        base_branch = gh.get_default_branch(owner, repo)
        repo_path = ensure_repo_checkout(owner, repo, home=home)
    except Exception as exc:  # noqa: BLE001
        detail = _format_preflight_error(exc)
        record["status"] = "failed"
        record["updated_at"] = now_iso()
        record["failure_reason"] = detail
        upsert_task(record, home=base_home)
        return {"task_id": task_id, "status": record["status"], "pr_url": None, "summary": detail}

    cfg = get_config()
    effective_runner = (runner or cfg.runner).strip().lower()
    if effective_runner not in {"codex", "claude"}:
        raise ValueError("runner must be one of: codex, claude")

    session_prefix = cfg.codex_session_prefix if effective_runner == "codex" else cfg.claude_session_prefix
    session_name = f"{session_prefix}{repo_slug(owner, repo)}"

    fix_context: str | None = None
    max_attempts = spec.max_attempts if spec.max_attempts is not None else cfg.max_attempts
    max_attempts = max(1, min(int(max_attempts), 10))

    for attempt in range(1, max_attempts + 1):
        prompt = _build_codex_prompt(task_id, repo_ref, verb, task_text, attempt, fix_context)
        if attempt == 1:
            _write_text(prompt_path, prompt)
        else:
            _append_text(prompt_path, f"\n---- attempt {attempt} ----\n{prompt}")

        agent_result = (
            run_codex(session_name=session_name, cwd=repo_path, prompt=prompt)
            if effective_runner == "codex"
            else run_claude(session_name=session_name, cwd=repo_path, prompt=prompt)
        )
        _append_text(
            agent_output_path,
            f"---- attempt {attempt} runner={effective_runner} rc={agent_result.returncode} ----\n{agent_result.stdout}\n{agent_result.stderr}",
        )
        if agent_result.returncode != 0:
            raise RuntimeError(
                f"acpx {effective_runner} failed on attempt {attempt}: {(agent_result.stderr or agent_result.stdout).strip()}"
            )

        _cleanup_repo_detritus(repo_path)

        footer = parse_codex_footer(agent_result.stdout)
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
                record["failure_reason"] = f"FIRE exhausted after {max_attempts} attempts; last CI detail: {ci_detail}"
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

    record["status"] = "failed"
    record["updated_at"] = now_iso()
    record["failure_reason"] = "FIRE exhausted"
    upsert_task(record, home=base_home)
    return {"task_id": task_id, "status": record["status"], "pr_url": record["pr_url"], "summary": record["failure_reason"]}


def run_task_mode_a(
    repo_ref: str,
    verb: str,
    spec: RunSpec,
    home: Path | None = None,
) -> dict[str, Any]:
    """Mode A loop: coordinator → work_item → worker → evaluate → repeat."""

    base_home = home or velora_home()

    # Use our own durable run_id/task_id so failures during preflight still get recorded.
    task_id = build_task_id()
    task_text = spec.task

    task_dir = ensure_dir(base_home / "tasks" / task_id)
    coord_output_path = task_dir / "coord-output.txt"
    agent_output_path = task_dir / "agent-output.txt"

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

    try:
        owner, repo = validate_repo_allowed(repo_ref)
        gh = GitHubClient.from_env()
        base_branch = gh.get_default_branch(owner, repo)
        repo_path = ensure_repo_checkout(owner, repo, home=home)
    except Exception as exc:  # noqa: BLE001
        detail = _format_preflight_error(exc)
        record["status"] = "failed"
        record["updated_at"] = now_iso()
        record["failure_reason"] = detail
        upsert_task(record, home=base_home)
        return {"task_id": task_id, "status": record["status"], "pr_url": None, "summary": detail}

    work_branch = f"velora/{task_id}"

    request: dict[str, Any] = {
        "protocol_version": 1,
        "run_id": task_id,
        "iteration": 1,
        "objective": task_text,
        "repo": {
            "owner": owner,
            "name": repo,
            "default_branch": base_branch,
            "work_branch": work_branch,
        },
        "policy": {
            "max_cost_usd": cfg.mode_a_max_cost_usd,
            "no_progress_max": cfg.mode_a_no_progress_max,
            "max_wall_seconds": cfg.mode_a_max_wall_seconds,
            "allow_self_merge": False,
            "required_gates": ["tests", "security"],
            "specialist_matrix": cfg.specialist_matrix,
        },
        "state": {
            "working_tree_clean": True,
            "last_commit": "",
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

    coord_session = coordinator_session_name(owner, repo)
    coord_runner = os.environ.get("VELORA_COORDINATOR_RUNNER", "claude").strip().lower() or "claude"

    max_attempts = spec.max_attempts if spec.max_attempts is not None else cfg.max_attempts
    max_attempts = max(1, min(int(max_attempts), 10))

    policy = request.get("policy") if isinstance(request, dict) else {}
    if not isinstance(policy, dict):
        policy = {}

    no_progress_max = int(policy.get("no_progress_max") or cfg.mode_a_no_progress_max)
    max_wall_seconds = int(policy.get("max_wall_seconds") or cfg.mode_a_max_wall_seconds)
    max_cost_usd = float(policy.get("max_cost_usd") or cfg.mode_a_max_cost_usd)

    loop_start = time.monotonic()
    last_failure_sig: str | None = None

    for attempt in range(1, max_attempts + 1):
        request["iteration"] = attempt

        # Breakers: wall clock and cost.
        hist = request.setdefault("history", {})
        elapsed = time.monotonic() - loop_start
        hist["elapsed_seconds"] = round(elapsed, 2)

        if max_wall_seconds and elapsed > max_wall_seconds:
            return _fail_task(
                record,
                home=base_home,
                task_dir=task_dir,
                detail=f"Wall-clock breaker tripped: elapsed={elapsed:.1f}s > max_wall_seconds={max_wall_seconds}",
            )

        cost_est = float(hist.get("cost_usd_estimate") or 0.0)
        if max_cost_usd and cost_est > max_cost_usd:
            return _fail_task(
                record,
                home=base_home,
                task_dir=task_dir,
                detail=f"Cost breaker tripped: cost_usd_estimate={cost_est:.2f} > max_cost_usd={max_cost_usd:.2f}",
            )

        iter_start = time.monotonic()

        try:
            coord_resp = run_coordinator_v1(
                session_name=coord_session,
                cwd=repo_path,
                request=request,
                runner=coord_runner,
            )
        except Exception as exc:  # noqa: BLE001
            detail = _format_preflight_error(exc)
            return _fail_task(
                record,
                home=base_home,
                task_dir=task_dir,
                detail=f"Coordinator failed on iteration {attempt}: {detail}",
            )

        _append_text(coord_output_path, f"---- iteration {attempt} decision={coord_resp.decision} ----\n{coord_resp.reason}")

        if coord_resp.decision != "execute_work_item":
            # In Mode A, finalize/stop must be explicit and we should surface it.
            record["status"] = _mode_a_status_for_terminal_decision(coord_resp.decision)
            if record["status"] == "failed":
                record["failure_reason"] = coord_resp.reason
            else:
                record.pop("failure_reason", None)

            hist = request.setdefault("history", {})
            hist["last_iteration_seconds"] = round(time.monotonic() - iter_start, 2)

            record["updated_at"] = now_iso()
            upsert_task(record, home=base_home)
            return {
                "task_id": task_id,
                "status": record["status"],
                "pr_url": record["pr_url"],
                "summary": coord_resp.reason,
            }

        if coord_resp.work_item is None:
            raise RuntimeError("CoordinatorResponse missing work_item")

        worker_runner = coord_resp.selected_specialist.runner
        if worker_runner not in {"codex", "claude"}:
            # Protocol should prevent this.
            raise RuntimeError(f"Unsupported worker runner: {worker_runner}")

        # One stable worker session per repo/runner.
        session_prefix = cfg.codex_session_prefix if worker_runner == "codex" else cfg.claude_session_prefix
        worker_session = f"{session_prefix}{repo_slug(owner, repo)}"

        prompt = build_worker_prompt_v1(
            repo_ref=repo_ref,
            verb=verb,
            objective=str(request["objective"]),
            run_id=task_id,
            iteration=attempt,
            work_branch=work_branch,
            work_item=coord_resp.work_item,
        )

        try:
            agent_result = (
                run_codex(session_name=worker_session, cwd=repo_path, prompt=prompt)
                if worker_runner == "codex"
                else run_claude(session_name=worker_session, cwd=repo_path, prompt=prompt)
            )
        except Exception as exc:  # noqa: BLE001
            detail = _format_preflight_error(exc)
            return _fail_task(
                record,
                home=base_home,
                task_dir=task_dir,
                detail=f"Worker runner '{worker_runner}' failed on iteration {attempt}: {detail}",
            )

        _append_text(
            agent_output_path,
            f"---- iteration {attempt} runner={worker_runner} rc={agent_result.returncode} ----\n{agent_result.stdout}\n{agent_result.stderr}",
        )
        if agent_result.returncode != 0:
            return _fail_task(
                record,
                home=base_home,
                task_dir=task_dir,
                detail=(
                    f"acpx {worker_runner} returned non-zero on iteration {attempt}: "
                    f"{(agent_result.stderr or agent_result.stdout).strip()}"
                ),
            )

        _cleanup_repo_detritus(repo_path)

        footer_raw = parse_codex_footer(agent_result.stdout)
        footer = footer_raw if isinstance(footer_raw, dict) else {}
        head_sha = _footer_head_sha(footer)
        record["branch"] = str(footer.get("branch") or "").strip() or None
        record["head_sha"] = head_sha or None
        record["summary"] = str(footer.get("summary") or "").strip()
        record["updated_at"] = now_iso()
        upsert_task(record, home=base_home)

        # Update coordinator state snapshot.
        request.setdefault("state", {})
        request["state"]["last_commit"] = head_sha

        if attempt == 1:
            try:
                pr = gh.create_pull_request(
                    owner=owner,
                    repo=repo,
                    title=_task_title(verb, task_text, spec.title),
                    body=_task_body(task_id, str(footer.get("summary") or ""), spec.body),
                    head=str(footer.get("branch") or ""),
                    base=base_branch,
                )
            except Exception as exc:  # noqa: BLE001
                detail = _format_preflight_error(exc)
                return _fail_task(
                    record,
                    home=base_home,
                    task_dir=task_dir,
                    detail=f"Failed to create PR on iteration {attempt}: {detail}",
                )

            record["pr_url"] = pr["html_url"]
            record["pr_number"] = pr["number"]
            record["updated_at"] = now_iso()
            upsert_task(record, home=base_home)

        ci_log = task_dir / f"ci-iter-{attempt}.log"
        _append_text(ci_log, f"[{now_iso()}] polling CI for {head_sha or '<missing-head-sha>'}")
        if not head_sha:
            ci_state, ci_detail = "failure", "missing-head-sha"
            _append_text(ci_log, f"[{now_iso()}] warning missing head_sha; skipping CI poll")
        else:
            try:
                ci_state, ci_detail = _poll_ci(gh, owner, repo, head_sha, ci_log)
            except Exception as exc:  # noqa: BLE001
                detail = _format_preflight_error(exc)
                return _fail_task(
                    record,
                    home=base_home,
                    task_dir=task_dir,
                    detail=f"CI polling failed on iteration {attempt}: {detail}",
                )
        _append_text(ci_log, f"[{now_iso()}] final {ci_state}: {ci_detail}")

        # Record history entry skeleton.
        hist = request.setdefault("history", {})
        work_items = hist.setdefault("work_items_executed", [])

        if ci_state != "success":
            failing_checks: list[dict[str, str]] = []
            ci_sig = ""
            if ci_state == "failure" and head_sha:
                try:
                    checks_payload = gh.get_check_runs(owner, repo, head_sha)
                    if isinstance(checks_payload, dict):
                        failing_checks, ci_sig = _parse_failing_check_runs_payload(checks_payload)
                except Exception as exc:  # noqa: BLE001
                    _append_text(ci_log, f"[{now_iso()}] warning unable to fetch check-runs for evidence: {exc}")

            if not failing_checks:
                failing_checks = [{"name": "ci", "kind": "ci", "url": record.get("pr_url"), "summary": _truncate_text(ci_detail, 420)}]
            logs_excerpt = _build_ci_logs_excerpt(failing_checks)
            if not logs_excerpt:
                logs_excerpt = _truncate_text(ci_detail, 600)

            failure_sig = f"ci:{ci_sig or _digest_failure_detail(ci_detail)}"
            no_prog = int(hist.get("no_progress_streak") or 0)
            no_prog = no_prog + 1 if last_failure_sig == failure_sig else 1
            last_failure_sig = failure_sig
            hist["no_progress_streak"] = no_prog
            hist["last_iteration_seconds"] = round(time.monotonic() - iter_start, 2)

            sigs = hist.setdefault("failure_signatures", [])
            sigs.append(failure_sig)
            del sigs[:-6]

            if _is_oscillating_failure_signatures(sigs):
                return _fail_task(
                    record,
                    home=base_home,
                    task_dir=task_dir,
                    detail=f"Oscillation breaker tripped: failure_signatures(last4)={sigs[-4:]}",
                )

            if no_prog >= no_progress_max:
                return _fail_task(
                    record,
                    home=base_home,
                    task_dir=task_dir,
                    detail=(
                        f"No-progress breaker tripped: no_progress_streak={no_prog} >= no_progress_max={no_progress_max} "
                        f"(failure_sig={failure_sig})"
                    ),
                )

            request["evaluation"] = {
                "status": "fail",
                "failing_checks": failing_checks,
                "logs_excerpt": logs_excerpt,
            }
            work_items.append(
                {
                    "id": coord_resp.work_item.id,
                    "kind": coord_resp.work_item.kind,
                    "result": "fail",
                    "patch_suggestion": {
                        "progress": "none" if no_prog > 1 else "some",
                        "evidence": [f"ci_state={ci_state}", f"ci_detail={ci_detail}"],
                        "next_guess": "repair failing CI checks",
                    },
                }
            )
            continue

        # CI success → review gate.
        diff_text = _read_diff_for_review(repo_path, base_branch, str(record["head_sha"]))
        gemini = run_gemini_review(diff_text)
        review_text = gemini.stdout.strip()
        if gemini.returncode != 0:
            review_text = f"BLOCKER: Review tool failed: {gemini.stderr.strip()}"

        review_attempt_path = task_dir / f"review-iter-{attempt}.txt"
        _write_text(review_attempt_path, review_text)

        if record["pr_number"] is None:
            raise RuntimeError("PR number missing; cannot post review comment")
        gh.post_issue_comment(owner, repo, int(record["pr_number"]), review_text)

        if gemini.returncode != 0 or "BLOCKER" in review_text:
            detail = "review-tool-failed" if gemini.returncode != 0 else "review-blocker"
            failure_sig = f"review:{detail}"
            no_prog = int(hist.get("no_progress_streak") or 0)
            no_prog = no_prog + 1 if last_failure_sig == failure_sig else 1
            last_failure_sig = failure_sig
            hist["no_progress_streak"] = no_prog
            hist["last_iteration_seconds"] = round(time.monotonic() - iter_start, 2)

            sigs = hist.setdefault("failure_signatures", [])
            sigs.append(failure_sig)
            del sigs[:-6]

            if _is_oscillating_failure_signatures(sigs):
                return _fail_task(
                    record,
                    home=base_home,
                    task_dir=task_dir,
                    detail=f"Oscillation breaker tripped: failure_signatures(last4)={sigs[-4:]}",
                )

            if no_prog >= no_progress_max:
                return _fail_task(
                    record,
                    home=base_home,
                    task_dir=task_dir,
                    detail=(
                        f"No-progress breaker tripped: no_progress_streak={no_prog} >= no_progress_max={no_progress_max} "
                        f"(failure_sig={failure_sig})"
                    ),
                )

            request["evaluation"] = {
                "status": "fail",
                "failing_checks": [{"name": "review", "kind": "review", "url": record.get("pr_url"), "summary": review_text[:2000]}],
                "logs_excerpt": review_text[:2000],
            }
            work_items.append(
                {
                    "id": coord_resp.work_item.id,
                    "kind": coord_resp.work_item.kind,
                    "result": "fail",
                    "patch_suggestion": {
                        "progress": "none" if no_prog > 1 else "some",
                        "evidence": [detail],
                        "next_guess": "address review blockers",
                    },
                }
            )
            continue

        # Success.
        request["evaluation"] = {"status": "success", "failing_checks": [], "logs_excerpt": ""}
        hist["no_progress_streak"] = 0
        hist["last_iteration_seconds"] = round(time.monotonic() - iter_start, 2)
        hist.pop("failure_signatures", None)
        last_failure_sig = None
        work_items.append(
            {
                "id": coord_resp.work_item.id,
                "kind": coord_resp.work_item.kind,
                "result": "pass",
                "patch_suggestion": {
                    "progress": "clear",
                    "evidence": ["ci_success", "review_clear"],
                    "next_guess": "",
                },
            }
        )

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

    record["status"] = "failed"
    record["updated_at"] = now_iso()
    record["failure_reason"] = f"Mode A loop exhausted after {max_attempts} iterations"
    upsert_task(record, home=base_home)
    return {"task_id": task_id, "status": record["status"], "pr_url": record["pr_url"], "summary": record["failure_reason"]}
