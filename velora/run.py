from __future__ import annotations

import os
import json
import hashlib
import re
import shutil
import subprocess
import time
from dataclasses import asdict, is_dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .acpx import GEMINI_REVIEW_PROMPT_PREFIX, parse_codex_footer, run_claude, run_codex, run_gemini_review
from .config import get_config
from .constants import VALID_VERBS
from .coordinator import run_coordinator_v1_with_cmd
from .exchange import append_event, work_item_exchange_paths, write_json
from .github import GitHubClient
from .orchestrator import coordinator_session_name, worker_session_name
from .protocol import ProtocolError, WorkResult, validate_work_result
from .repo import ensure_repo_checkout, validate_repo_allowed
from .spec import RunSpec
from .state import get_task, upsert_task
from .util import build_task_id, ensure_dir, now_iso, repo_slug, velora_home
from .worker_prompt import build_worker_prompt_v1

_APPROVAL_TOKEN_RE = re.compile(r"^\s*ok(?:[.:])?(?:\s|$)", flags=re.IGNORECASE)
_FINDING_LINE_RE = re.compile(
    r"^(?:[-*+]\s+|\d+\.\s+)?(?:(?:\*\*(BLOCKER|NIT):\*\*)|(?:\*\*(BLOCKER|NIT)\*\*:)|((?:BLOCKER|NIT):))\s+\S",
    flags=re.IGNORECASE,
)
_REVIEW_DEBUG_MAX_DIFF_PREVIEW_CHARS = 1500
_REVIEW_DEBUG_MAX_REVIEW_PREVIEW_CHARS = 400

_INTERNAL_FAULT_ENABLE_ENV = "VELORA_INTERNAL_DANGEROUS_FAULT_INJECTION_ENABLE"
_INTERNAL_FAULT_CHECKPOINT_ENV = "VELORA_INTERNAL_DANGEROUS_FAULT_INJECTION_CHECKPOINT"
_INTERNAL_FAULT_ENABLE_VALUE = "I_UNDERSTAND_THIS_WILL_CRASH_VELORA"

CHECKPOINT_AFTER_PR_CREATED = "after_pr_created"
CHECKPOINT_AFTER_CI_SUCCESS_BEFORE_REVIEW = "after_ci_success_before_review"
CHECKPOINT_AFTER_REVIEW_RESOLUTION = "after_review_resolution"


class InternalFaultInjectionTriggered(RuntimeError):
    pass


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


def _configured_fault_checkpoints() -> set[str]:
    raw = os.environ.get(_INTERNAL_FAULT_CHECKPOINT_ENV, "")
    return {item.strip() for item in raw.split(",") if item.strip()}


def _maybe_inject_internal_fault(*, checkpoint: str, task_id: str) -> None:
    enabled = os.environ.get(_INTERNAL_FAULT_ENABLE_ENV, "").strip()
    if enabled != _INTERNAL_FAULT_ENABLE_VALUE:
        return

    checkpoints = _configured_fault_checkpoints()
    if checkpoint not in checkpoints:
        return

    raise InternalFaultInjectionTriggered(
        f"Internal fault injection triggered at checkpoint={checkpoint} for task_id={task_id}. "
        f"This is test-only and intentionally interrupts the run."
    )


def _persist_record_checkpoint(
    record: dict[str, Any],
    *,
    home: Path,
    checkpoint: str,
    updates: dict[str, Any] | None = None,
) -> None:
    if updates:
        record.update(updates)

    ts = now_iso()
    record["persisted_checkpoint"] = checkpoint
    record["persisted_checkpoint_at"] = ts
    record["updated_at"] = ts
    upsert_task(record, home=home)
    _maybe_inject_internal_fault(checkpoint=checkpoint, task_id=str(record.get("task_id") or ""))


def _usd_equiv_rate_per_1m_tokens() -> float:
    raw = os.environ.get("VELORA_USD_EQUIV_PER_1M_TOKENS", "").strip()
    if not raw:
        return 0.0
    try:
        return float(raw)
    except ValueError:
        return 0.0


def _ensure_hist_dict(hist: dict[str, Any], key: str) -> dict[str, Any]:
    val = hist.setdefault(key, {})
    if isinstance(val, dict):
        return val
    val = {}
    hist[key] = val
    return val


def _accumulate_acpx_usage(
    request: dict[str, Any],
    *,
    session_name: str,
    result: Any,
    actor: str,
    branch: str | None = None,
) -> int:
    """Accumulate best-effort token usage from an acpx CmdResult into Mode A history.

    This uses acpx's `usage_update.used` counter, which reflects context usage.
    It is useful for budget break/visibility, but it is not guaranteed billed-token truth.
    We only charge explicit deltas after a per-session baseline is established.
    """

    usage = getattr(result, "usage", None)
    if usage is None:
        return 0

    used = getattr(usage, "used", None)
    if not isinstance(used, int):
        return 0

    hist = request.setdefault("history", {})
    sess_usage = _ensure_hist_dict(hist, "session_usage")
    sess_baselines = _ensure_hist_dict(hist, "session_usage_baselines")
    sess_deltas = _ensure_hist_dict(hist, "session_usage_deltas")

    baseline = sess_baselines.get(session_name)
    has_baseline = isinstance(baseline, int)
    prev = sess_usage.get(session_name)
    prev_used = int(prev) if isinstance(prev, int) else int(baseline) if has_baseline else None

    # First observation for a session is baseline only; don't attribute unknown
    # prior cumulative usage to this run.
    if prev_used is None:
        sess_baselines[session_name] = used
        sess_usage[session_name] = used
        delta = 0
    else:
        delta = used - prev_used

    # If the session was reset/compacted and used goes backwards, re-baseline.
    # Treat this as unknown attribution instead of inventing a charge.
    if delta < 0:
        sess_baselines[session_name] = used
        delta = 0

    sess_usage[session_name] = used
    sess_deltas[session_name] = int(sess_deltas.get(session_name) or 0) + int(delta)

    hist["tokens_used_estimate"] = int(hist.get("tokens_used_estimate") or 0) + int(delta)

    # USD-equivalent is informational only. Disabled unless user configures a rate.
    rate = _usd_equiv_rate_per_1m_tokens()
    hist["usd_equiv_per_1m_tokens"] = rate
    hist["cost_usd_estimate"] = float(hist.get("cost_usd_estimate") or 0.0) + (float(delta) / 1_000_000.0) * rate

    if actor == "coordinator":
        hist["coordinator_tokens_used_estimate"] = int(hist.get("coordinator_tokens_used_estimate") or 0) + int(delta)
    elif actor == "worker":
        hist["worker_tokens_used_estimate"] = int(hist.get("worker_tokens_used_estimate") or 0) + int(delta)
        if isinstance(branch, str) and branch.strip():
            by_branch = _ensure_hist_dict(hist, "worker_tokens_by_branch_estimate")
            b = branch.strip()
            by_branch[b] = int(by_branch.get(b) or 0) + int(delta)

    model_id = getattr(usage, "model_id", None)
    if isinstance(model_id, str) and model_id.strip():
        models = hist.setdefault("models_seen", [])
        if isinstance(models, list) and model_id not in models:
            models.append(model_id)
            del models[:-10]
    return int(delta)


def _sync_budget_to_record(record: dict[str, Any], request: dict[str, Any]) -> None:
    hist = request.get("history") if isinstance(request, dict) else None
    if not isinstance(hist, dict):
        return

    for key in (
        "tokens_used_estimate",
        "cost_usd_estimate",
        "usd_equiv_per_1m_tokens",
        "models_seen",
        "coordinator_tokens_used_estimate",
        "worker_tokens_used_estimate",
        "worker_tokens_by_branch_estimate",
    ):
        if key in hist:
            record[key] = hist.get(key)


def _compact_title_fragment(text: str, max_len: int) -> str:
    # Collapse whitespace and strip common “loud” prefixes.
    t = " ".join((text or "").split()).strip()
    for prefix in (
        "IMPORTANT:",
        "IMPORTANT",
        "Mode A complex dogfood:",
    ):
        if t.lower().startswith(prefix.lower()):
            t = t[len(prefix) :].strip()

    # Prefer something sentence-like.
    for sep in (". ", "; "):
        if sep in t:
            t = t.split(sep, 1)[0].strip()
            break

    if max_len < 1:
        return ""
    if len(t) <= max_len:
        return t

    # Ellipsis truncation.
    cut = max(1, max_len - 1)
    return t[:cut].rstrip() + "…"


def _task_title(verb: str, task: str, title_override: str | None = None) -> str:
    # Keep PR titles socially acceptable (and avoid leaking full prompts).
    max_total = 96
    prefix = f"[{verb}] "

    if title_override and title_override.strip():
        frag = " ".join(title_override.split()).strip()
    else:
        frag = _compact_title_fragment(task, max_total - len(prefix))

    title = (prefix + frag).strip()
    if len(title) > max_total:
        title = title[: max_total - 1].rstrip() + "…"
    return title


def _task_body(task_id: str, summary: str, extra_body: str | None) -> str:
    body = f"VELORA task_id: {task_id}\n\n{summary}".strip()
    if extra_body and extra_body.strip():
        body += "\n\n" + extra_body.strip()
    return body + "\n"


_DEFAULT_PR_TEMPLATE = """## Summary

## Testing
- [ ] Unit tests pass

## Notes
"""


def _load_repo_pr_template(repo_path: Path) -> str | None:
    """Load a PR template from the target repo checkout if present.

    GitHub-supported locations include (common cases):
    - PULL_REQUEST_TEMPLATE.md (repo root)
    - .github/PULL_REQUEST_TEMPLATE.md
    - docs/PULL_REQUEST_TEMPLATE.md

    Also support the multi-template directory style:
    - .github/PULL_REQUEST_TEMPLATE/*.md

    If multiple templates exist, prefer default.md, else pick the first .md alphabetically.
    """

    candidates = [
        repo_path / "PULL_REQUEST_TEMPLATE.md",
        repo_path / ".github" / "PULL_REQUEST_TEMPLATE.md",
        repo_path / ".github" / "pull_request_template.md",
        repo_path / "docs" / "PULL_REQUEST_TEMPLATE.md",
    ]
    for path in candidates:
        if path.exists() and path.is_file():
            return path.read_text(encoding="utf-8")

    for dir_path in (
        repo_path / ".github" / "PULL_REQUEST_TEMPLATE",
        repo_path / ".github" / "pull_request_template",
    ):
        if not dir_path.exists() or not dir_path.is_dir():
            continue

        default_md = dir_path / "default.md"
        if default_md.exists() and default_md.is_file():
            return default_md.read_text(encoding="utf-8")

        md_files = sorted(p for p in dir_path.glob("*.md") if p.is_file())
        if md_files:
            return md_files[0].read_text(encoding="utf-8")

    return None


def _build_pr_body(
    *,
    repo_path: Path,
    task_id: str,
    summary: str,
    extra_body: str | None,
) -> str:
    base = _task_body(task_id, summary, extra_body).strip()
    template = _load_repo_pr_template(repo_path)
    if template and template.strip():
        # Respect the repo's template first; append Velora metadata at the end.
        return template.strip() + "\n\n---\n\n" + base + "\n"

    # Default template (kept lightweight).
    return base + "\n\n---\n\n" + _DEFAULT_PR_TEMPLATE


def _mode_a_status_for_terminal_decision(decision: str) -> str:
    if decision == "finalize_success":
        return "ready"
    if decision == "stop_failure":
        return "failed"
    raise ValueError(f"Unsupported terminal decision: {decision}")


def _extract_json_object_from_text(output: str) -> str:
    """Extract a JSON object payload from worker output.

    Primary contract: output must be a single JSON object.
    Compatibility bridge: allow a single fenced ```json block for debugging/recovery.
    """

    text = (output or "").strip()
    if not text:
        raise ProtocolError("Worker output is empty; expected WorkResult JSON object")

    if text.startswith("{") and text.endswith("}"):
        return text

    fence_match = re.search(r"```json\s*(\{.*?\})\s*```", text, flags=re.IGNORECASE | re.DOTALL)
    if fence_match:
        return fence_match.group(1).strip()

    raise ProtocolError("Worker output must be a JSON object (or a single fenced ```json block)")


def _parse_worker_work_result(
    output: str,
    *,
    expected_work_item_id: str,
    expected_branch: str | None = None,
) -> WorkResult:
    payload_raw = _extract_json_object_from_text(output)
    try:
        payload = json.loads(payload_raw)
    except json.JSONDecodeError as exc:
        raise ProtocolError(f"Worker output is not valid JSON: {exc}") from exc

    result = validate_work_result(payload)
    if result.work_item_id != expected_work_item_id:
        raise ProtocolError(
            f"WorkResult.work_item_id mismatch: expected {expected_work_item_id}, got {result.work_item_id}"
        )
    if expected_branch is not None and result.status == "completed" and result.branch != expected_branch:
        raise ProtocolError(
            f"WorkResult.branch mismatch: expected {expected_branch}, got {result.branch}"
        )
    return result


def _load_worker_work_result_from_file(
    path: Path,
    *,
    expected_work_item_id: str,
    expected_branch: str | None = None,
) -> WorkResult:
    if not path.exists():
        raise ProtocolError(f"Worker result file missing: {path}")
    return _parse_worker_work_result(
        path.read_text(encoding="utf-8"),
        expected_work_item_id=expected_work_item_id,
        expected_branch=expected_branch,
    )


def _load_worker_outcome(
    exchange_paths: dict[str, Path],
    *,
    expected_work_item_id: str,
    expected_branch: str,
) -> tuple[str, WorkResult]:
    kinds = [kind for kind in ("result", "handoff", "block") if exchange_paths[kind].exists()]
    if len(kinds) != 1:
        present = ", ".join(kinds) if kinds else "none"
        raise ProtocolError(
            "Worker must write exactly one outcome file among result.json, handoff.json, block.json "
            f"(present: {present})"
        )

    kind = kinds[0]
    result = _load_worker_work_result_from_file(
        exchange_paths[kind],
        expected_work_item_id=expected_work_item_id,
        expected_branch=(expected_branch if kind == "result" else None),
    )

    if kind == "result" and result.status != "completed":
        raise ProtocolError("result.json requires status=completed")
    if kind == "handoff" and result.status != "completed":
        raise ProtocolError("handoff.json requires status=completed")
    if kind == "block" and result.status == "completed":
        raise ProtocolError("block.json requires status=blocked or status=failed")

    return kind, result


def _work_result_artifact(work_result: WorkResult) -> dict[str, Any]:
    return {
        "protocol_version": work_result.protocol_version,
        "work_item_id": work_result.work_item_id,
        "status": work_result.status,
        "summary": work_result.summary,
        "branch": work_result.branch,
        "head_sha": work_result.head_sha,
        "files_touched": list(work_result.files_touched),
        "tests_run": [
            {"command": t.command, "status": t.status, "details": t.details}
            for t in work_result.tests_run
        ],
        "blockers": list(work_result.blockers),
        "follow_up": list(work_result.follow_up),
        "evidence": list(work_result.evidence),
    }


def _json_compatible(value: Any) -> Any:
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, dict):
        return {str(k): _json_compatible(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_compatible(v) for v in value]
    if hasattr(value, "__dict__"):
        return {str(k): _json_compatible(v) for k, v in vars(value).items()}
    return value


def _set_evaluation_state(
    request: dict[str, Any],
    *,
    status: str,
    outcome: str,
    worker_result: WorkResult | None,
    ci_state: str | None = None,
    ci_detail: str | None = None,
    review_result: str | None = None,
    failing_checks: list[dict[str, Any]] | None = None,
    logs_excerpt: str = "",
) -> None:
    request["evaluation"] = {
        "status": status,
        "outcome": outcome,
        "worker_result_status": (worker_result.status if worker_result is not None else None),
        "ci_state": ci_state,
        "ci_detail": ci_detail or "",
        "review_result": review_result,
        "failing_checks": failing_checks or [],
        "logs_excerpt": logs_excerpt,
    }


def _append_iteration_history_entry(
    request: dict[str, Any],
    *,
    iteration: int,
    work_item: Any,
    selected_specialist: Any,
    worker_result: WorkResult,
    outcome: str,
    ci: dict[str, Any] | None = None,
    review: dict[str, Any] | None = None,
) -> None:
    hist = request.setdefault("history", {})
    work_items = hist.setdefault("work_items_executed", [])
    acceptance = getattr(work_item, "acceptance", None)
    gates = getattr(acceptance, "gates", []) if acceptance is not None else []
    work_items.append(
        {
            "iteration": iteration,
            "work_item": {
                "id": work_item.id,
                "kind": work_item.kind,
                "rationale": getattr(work_item, "rationale", ""),
                "acceptance_gates": list(gates),
            },
            "selected_specialist": {
                "role": selected_specialist.role,
                "runner": selected_specialist.runner,
                "model": getattr(selected_specialist, "model", None),
            },
            "artifacts": {
                "worker_result": _work_result_artifact(worker_result),
                "ci": ci,
                "review": review,
            },
            "outcome": outcome,
        }
    )


def _is_oscillating_failure_signatures(sigs: list[str]) -> bool:
    """Detect a simple ABAB oscillation in the last 4 failure signatures."""

    if len(sigs) < 4:
        return False
    a, b, c, d = sigs[-4:]
    return a == c and b == d and a != b


def _parse_iso8601(ts: object) -> datetime | None:
    if not isinstance(ts, str) or not ts.strip():
        return None
    s = ts.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def _classify_ci_failure(
    ci_state: str,
    ci_detail: str,
    checks_payload: dict[str, Any] | None,
) -> dict[str, Any]:
    checks = checks_payload.get("check_runs", []) if isinstance(checks_payload, dict) else []
    runs = checks if isinstance(checks, list) else []
    infra_conclusions = {"cancelled", "timed_out", "neutral", "stale", "startup_failure", "action_required"}
    reasons: set[str] = set()
    total = len(runs)
    started = 0
    queued = 0
    real_fail = 0
    infra_fail = 0
    output_evidence = 0
    nonzero_runtime = 0
    for run in runs:
        if not isinstance(run, dict):
            continue
        st = str(run.get("status") or "").lower()
        if st in {"queued", "requested", "pending", "waiting"}:
            queued += 1
        if run.get("started_at"):
            started += 1
        c = str(run.get("conclusion") or "").lower()
        if c == "failure":
            real_fail += 1
        elif c in infra_conclusions:
            infra_fail += 1
        out = run.get("output")
        if isinstance(out, dict) and (str(out.get("summary") or "").strip() or str(out.get("title") or "").strip()):
            output_evidence += 1
        a = _parse_iso8601(run.get("started_at"))
        b = _parse_iso8601(run.get("completed_at"))
        if a and b and (b - a).total_seconds() >= 5:
            nonzero_runtime += 1
    if total > 0 and started == 0:
        reasons.add("queued_never_started")
    if ci_detail == "stuck-no-progress":
        reasons.add("poll_stuck_no_progress")
    if real_fail > 0:
        reasons.add("explicit_failure_conclusion")
    if output_evidence > 0:
        reasons.add("failure_output_present")
    if infra_fail > 0 and real_fail == 0:
        reasons.add("infra_like_conclusions")

    classification = "unknown"
    confidence = "low"
    if real_fail > 0 or output_evidence > 0:
        classification, confidence = "code_failure", "high"
    elif ("queued_never_started" in reasons and "poll_stuck_no_progress" in reasons) or (
        infra_fail > 0 and real_fail == 0 and nonzero_runtime == 0 and output_evidence == 0
    ):
        classification, confidence = "infra_outage", "high"
    elif infra_fail > 0 and real_fail == 0:
        classification, confidence = "infra_outage", "medium"
    return {
        "classification": classification,
        "confidence": confidence,
        "reason_codes": sorted(reasons),
        "evidence": {
            "check_runs_total": total,
            "started_runs": started,
            "queued_runs": queued,
            "real_failure_runs": real_fail,
            "infra_like_runs": infra_fail,
            "output_evidence_runs": output_evidence,
            "runtime_ge_5s_runs": nonzero_runtime,
            "ci_state": ci_state,
        },
    }


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


def _write_worker_raw_output(path: Path, *, iteration: int, runner: str, rc: int, stdout: str, stderr: str) -> None:
    _write_text(
        path,
        f"---- iteration {iteration} runner={runner} rc={rc} ----\n{stdout}\n{stderr}",
    )


def _dbg(task_dir: Path | None, event: str, data: dict[str, Any] | None = None) -> None:
    """Best-effort structured debug logging to task_dir/debug.jsonl."""

    if task_dir is None:
        return

    payload: dict[str, Any] = {"ts": now_iso(), "event": event}
    if data:
        payload.update(data)

    try:
        _append_text(task_dir / "debug.jsonl", json.dumps(payload, sort_keys=True))
    except Exception:
        # Never fail the run due to debug logging.
        pass


def _truncate_for_debug(text: str, max_chars: int) -> str:
    cleaned = re.sub(r"[^\x09\x0A\x0D\x20-\x7E]", "?", text or "")
    cleaned = re.sub(r"(?i)\b(token|api[_-]?key|secret|password)\b\s*[:=]\s*\S+", r"\1=<redacted>", cleaned)
    cleaned = re.sub(r"(?i)\bauthorization:\s*\S+", "authorization: <redacted>", cleaned)
    if len(cleaned) <= max_chars:
        return cleaned
    return cleaned[:max_chars] + "\n[truncated]"


def _write_review_forensics(
    task_dir: Path | None,
    *,
    review_try: int,
    review_result: str,
    review_text: str,
    diff_text: str,
) -> None:
    if task_dir is None:
        return

    payload = {
        "artifact_version": 1,
        "review_try": review_try + 1,
        "review_result": review_result,
        "prompt_prefix": GEMINI_REVIEW_PROMPT_PREFIX.strip(),
        "diff_chars": len(diff_text or ""),
        "diff_fingerprint_sha256": hashlib.sha256((diff_text or "").encode("utf-8")).hexdigest(),
        "diff_preview": _truncate_for_debug(diff_text, _REVIEW_DEBUG_MAX_DIFF_PREVIEW_CHARS),
        "review_preview": _truncate_for_debug(review_text, _REVIEW_DEBUG_MAX_REVIEW_PREVIEW_CHARS),
        "ts": now_iso(),
    }
    _write_text(task_dir / f"review-forensics-try-{review_try + 1}.json", json.dumps(payload, sort_keys=True))


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


def _classify_review_text(review_text: str) -> str:
    text = review_text.strip()
    if not text:
        return "malformed"

    # Approval tokens are only valid at the beginning of the review output.
    if _APPROVAL_TOKEN_RE.match(text):
        return "approved"

    lines = [line.rstrip() for line in text.splitlines() if line.strip()]
    if not lines:
        return "malformed"

    saw_blocker = False
    saw_finding = False
    for line in lines:
        match = _FINDING_LINE_RE.match(line)
        if match:
            saw_finding = True
            label = next((group for group in match.groups() if group), "")
            if label.upper().startswith("BLOCKER"):
                saw_blocker = True
            continue

        # Allow light prose before the first structured finding line.
        if not saw_finding:
            continue

        # Allow indented continuation lines for finding details.
        if line[:1].isspace():
            continue

        return "malformed"

    if not saw_finding:
        return "malformed"

    return "blocker" if saw_blocker else "nits"


def _run_review_with_retry(diff_text: str, *, debug_task_dir: Path | None = None) -> tuple[str, str]:
    review_text = ""
    for review_try in range(2):
        gemini = run_gemini_review(diff_text)
        review_text = gemini.stdout.strip()
        if gemini.returncode != 0:
            _write_review_forensics(
                debug_task_dir,
                review_try=review_try,
                review_result="tool-error",
                review_text=gemini.stderr.strip(),
                diff_text=diff_text,
            )
            return "tool-error", f"REVIEW_TOOL_ERROR: {gemini.stderr.strip()}"

        review_result = _classify_review_text(review_text)
        if review_result != "malformed":
            return review_result, review_text
        _write_review_forensics(
            debug_task_dir,
            review_try=review_try,
            review_result="malformed",
            review_text=review_text,
            diff_text=diff_text,
        )
        if review_try == 1:
            return "malformed", f"REVIEW_MALFORMED: {review_text}"

    return "malformed", f"REVIEW_MALFORMED: {review_text}"


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
    base_branch: str | None = None,
    *,
    use_coordinator: bool = False,
    debug: bool = False,
) -> dict[str, Any]:
    """Run a VELORA task.

    - Legacy mode (default): direct worker prompt (Codex/Claude) + FIRE loop.
    - Mode A (use_coordinator=True): Coordinator (control-plane) emits WorkItems;
      workers execute; CI + review feed back into coordinator.
    """

    if use_coordinator:
        return run_task_mode_a(repo_ref, verb, spec, home=home, base_branch=base_branch, debug=debug)
    return run_task_legacy(repo_ref, verb, spec, home=home, runner=runner, base_branch=base_branch, debug=debug)


def resume_task(task_id: str, home: Path | None = None, *, debug: bool = False) -> dict[str, Any]:
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

    repo_path = ensure_repo_checkout(owner, repo, home=home, base_branch=base_branch)

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
            body=_build_pr_body(repo_path=repo_path, task_id=task_id, summary=summary, extra_body=None),
            head=branch,
            base=base_branch,
        )
        task["pr_url"] = pr["html_url"]
        task["pr_number"] = pr["number"]
        _persist_record_checkpoint(task, home=base_home, checkpoint=CHECKPOINT_AFTER_PR_CREATED)

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

    _persist_record_checkpoint(
        task,
        home=base_home,
        checkpoint=CHECKPOINT_AFTER_CI_SUCCESS_BEFORE_REVIEW,
        updates={"ci_state": ci_state, "ci_detail": ci_detail},
    )

    # Review gate.
    diff_text = _read_diff_for_review(repo_path, base_branch, head_sha)
    review_result, review_text = _run_review_with_retry(diff_text, debug_task_dir=task_dir if debug else None)

    review_path = task_dir / "review-resume.txt"
    _write_text(review_path, review_text)

    pr_number = int(task["pr_number"])
    gh.post_issue_comment(owner, repo, pr_number, review_text)

    if review_result in {"tool-error", "malformed", "blocker"}:
        task["status"] = "not-ready"
    else:
        task["status"] = "ready"

    _persist_record_checkpoint(
        task,
        home=base_home,
        checkpoint=CHECKPOINT_AFTER_REVIEW_RESOLUTION,
        updates={"review_result": review_result},
    )

    return {
        "task_id": task_id,
        "status": task["status"],
        "pr_url": task.get("pr_url"),
        "summary": task.get("summary"),
        "ci_state": ci_state,
        "ci_detail": ci_detail,
        "review": review_text,
        "review_result": review_result,
    }


def run_task_legacy(
    repo_ref: str,
    verb: str,
    spec: RunSpec,
    home: Path | None = None,
    runner: str | None = None,
    base_branch: str | None = None,
    debug: bool = False,
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
        base_branch = (base_branch or "").strip() or gh.get_default_branch(owner, repo)
        repo_path = ensure_repo_checkout(owner, repo, home=home, base_branch=base_branch)
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
                body=_build_pr_body(repo_path=repo_path, task_id=task_id, summary=footer["summary"], extra_body=spec.body),
                head=footer["branch"],
                base=base_branch,
            )
            record["pr_url"] = pr["html_url"]
            record["pr_number"] = pr["number"]
            _persist_record_checkpoint(record, home=base_home, checkpoint=CHECKPOINT_AFTER_PR_CREATED)

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

        _persist_record_checkpoint(
            record,
            home=base_home,
            checkpoint=CHECKPOINT_AFTER_CI_SUCCESS_BEFORE_REVIEW,
            updates={"ci_state": ci_state, "ci_detail": ci_detail},
        )

        diff_text = _read_diff_for_review(repo_path, base_branch, str(record["head_sha"]))
        review_result, review_text = _run_review_with_retry(diff_text, debug_task_dir=task_dir if debug else None)

        review_attempt_path = task_dir / f"review-attempt-{attempt}.txt"
        _write_text(review_attempt_path, review_text)

        if record["pr_number"] is None:
            raise RuntimeError("PR number missing; cannot post review comment")
        gh.post_issue_comment(owner, repo, int(record["pr_number"]), review_text)

        if review_result in {"tool-error", "malformed"}:
            record["status"] = "not-ready"
            _persist_record_checkpoint(
                record,
                home=base_home,
                checkpoint=CHECKPOINT_AFTER_REVIEW_RESOLUTION,
                updates={"review_result": review_result},
            )
            return {
                "task_id": task_id,
                "status": record["status"],
                "pr_url": record["pr_url"],
                "summary": record["summary"],
                "ci_state": ci_state,
                "ci_detail": ci_detail,
                "review": review_text,
                "review_result": review_result,
            }

        if review_result == "blocker":
            if attempt == max_attempts:
                record["status"] = "not-ready"
                _persist_record_checkpoint(
                    record,
                    home=base_home,
                    checkpoint=CHECKPOINT_AFTER_REVIEW_RESOLUTION,
                    updates={"review_result": review_result},
                )
                return {
                    "task_id": task_id,
                    "status": record["status"],
                    "pr_url": record["pr_url"],
                    "summary": record["summary"],
                    "ci_state": ci_state,
                    "ci_detail": ci_detail,
                    "review": review_text,
                    "review_result": review_result,
                }

            fix_context = f"Attempt {attempt} review blockers to address:\n{review_text}"
            continue

        record["status"] = "ready"
        _persist_record_checkpoint(
            record,
            home=base_home,
            checkpoint=CHECKPOINT_AFTER_REVIEW_RESOLUTION,
            updates={"review_result": review_result},
        )
        return {
            "task_id": task_id,
            "status": record["status"],
            "pr_url": record["pr_url"],
            "summary": record["summary"],
            "ci_state": ci_state,
            "ci_detail": ci_detail,
            "review": review_text,
            "review_result": review_result,
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
    base_branch: str | None = None,
    debug: bool = False,
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

    dbg_dir = task_dir if debug else None
    _dbg(
        dbg_dir,
        "run_start",
        {
            "task_id": task_id,
            "repo": repo_ref,
            "verb": verb,
            "base_branch_override": (base_branch or "").strip() or None,
            "max_attempts": spec.max_attempts,
            "max_tokens": os.environ.get("VELORA_MODE_A_MAX_TOKENS"),
        },
    )

    cfg = get_config()

    try:
        owner, repo = validate_repo_allowed(repo_ref)
        gh = GitHubClient.from_env()
        base_branch = (base_branch or "").strip() or gh.get_default_branch(owner, repo)
        repo_path = ensure_repo_checkout(owner, repo, home=home, base_branch=base_branch)
    except Exception as exc:  # noqa: BLE001
        detail = _format_preflight_error(exc)
        record["status"] = "failed"
        record["updated_at"] = now_iso()
        record["failure_reason"] = detail
        upsert_task(record, home=base_home)
        return {"task_id": task_id, "status": record["status"], "pr_url": None, "summary": detail}

    _dbg(
        dbg_dir,
        "preflight_ok",
        {
            "owner": owner,
            "repo": repo,
            "base_branch": base_branch,
            "repo_path": str(repo_path),
        },
    )

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
            "max_tokens": cfg.mode_a_max_tokens,
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
            "latest_worker_result": None,
            "latest_handoff": None,
            "latest_ci": None,
            "latest_review": None,
        },
        "evaluation": {
            "status": "none",
            "outcome": "none",
            "worker_result_status": None,
            "ci_state": None,
            "ci_detail": "",
            "review_result": None,
            "failing_checks": [],
            "logs_excerpt": "",
        },
        "history": {
            "work_items_executed": [],
            "no_progress_streak": 0,
            "tokens_used_estimate": 0,
            "cost_usd_estimate": 0.0,
            "session_usage": {},
            "session_usage_baselines": {},
            "session_usage_deltas": {},
            "coordinator_tokens_used_estimate": 0,
            "worker_tokens_used_estimate": 0,
            "worker_tokens_by_branch_estimate": {},
        },
    }

    coord_session = coordinator_session_name(owner, repo, task_id)
    coord_runner = os.environ.get("VELORA_COORDINATOR_RUNNER", "claude").strip().lower() or "claude"

    max_attempts = spec.max_attempts if spec.max_attempts is not None else cfg.max_attempts
    max_attempts = max(1, min(int(max_attempts), 10))

    policy = request.get("policy") if isinstance(request, dict) else {}
    if not isinstance(policy, dict):
        policy = {}

    no_progress_max = int(policy.get("no_progress_max") or cfg.mode_a_no_progress_max)
    max_wall_seconds = int(policy.get("max_wall_seconds") or cfg.mode_a_max_wall_seconds)
    max_tokens = int(policy.get("max_tokens") or cfg.mode_a_max_tokens)

    loop_start = time.monotonic()
    last_failure_sig: str | None = None

    for attempt in range(1, max_attempts + 1):
        request["iteration"] = attempt

        # Breakers: wall clock and token budget.
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

        tokens_used = int(hist.get("tokens_used_estimate") or 0)
        if max_tokens and tokens_used > max_tokens:
            return _fail_task(
                record,
                home=base_home,
                task_dir=task_dir,
                detail=f"Token breaker tripped: tokens_used_estimate={tokens_used} > max_tokens={max_tokens}",
            )

        iter_start = time.monotonic()

        try:
            _dbg(
                dbg_dir,
                "coordinator_start",
                {
                    "iteration": attempt,
                    "runner": coord_runner,
                    "session": coord_session,
                    "tokens_total": int(hist.get("tokens_used_estimate") or 0),
                },
            )
            coord_t0 = time.monotonic()
            coord_run = run_coordinator_v1_with_cmd(
                session_name=coord_session,
                cwd=repo_path,
                request=request,
                runner=coord_runner,
            )
            coord_dt = round(time.monotonic() - coord_t0, 2)
            coord_resp = coord_run.response
            _accumulate_acpx_usage(request, session_name=coord_session, result=coord_run.cmd, actor="coordinator")
            _sync_budget_to_record(record, request)
            _dbg(
                dbg_dir,
                "coordinator_done",
                {
                    "iteration": attempt,
                    "duration_s": coord_dt,
                    "decision": coord_resp.decision,
                    "selected_role": coord_resp.selected_specialist.role,
                    "selected_runner": coord_resp.selected_specialist.runner,
                    "work_item_id": (coord_resp.work_item.id if coord_resp.work_item else None),
                    "work_item_kind": (coord_resp.work_item.kind if coord_resp.work_item else None),
                    "model_id": getattr(getattr(coord_run.cmd, "usage", None), "model_id", None),
                    "tokens_total": record.get("tokens_used_estimate"),
                },
            )

            # Trip immediately if the coordinator itself blew the token budget.
            hist = request.setdefault("history", {})
            tokens_used = int(hist.get("tokens_used_estimate") or 0)
            if max_tokens and tokens_used > max_tokens:
                return _fail_task(
                    record,
                    home=base_home,
                    task_dir=task_dir,
                    detail=f"Token breaker tripped after coordinator: tokens_used_estimate={tokens_used} > max_tokens={max_tokens}",
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

        # One stable worker session per run/runner.
        worker_session = worker_session_name(owner, repo, task_id, worker_runner)

        exchange_paths = work_item_exchange_paths(repo_path, task_id, coord_resp.work_item.id)
        for key in ("result", "handoff", "block", "error"):
            if exchange_paths[key].exists():
                exchange_paths[key].unlink()
        write_json(exchange_paths["work_item"], _json_compatible(coord_resp.work_item))
        write_json(
            exchange_paths["status"],
            {
                "run_id": task_id,
                "work_item_id": coord_resp.work_item.id,
                "iteration": attempt,
                "runner": worker_runner,
                "status": "running",
                "updated_at": now_iso(),
            },
        )
        append_event(
            exchange_paths["events"],
            "work_item_dispatched",
            {
                "run_id": task_id,
                "work_item_id": coord_resp.work_item.id,
                "iteration": attempt,
                "runner": worker_runner,
            },
        )

        prompt = build_worker_prompt_v1(
            repo_ref=repo_ref,
            verb=verb,
            objective=str(request["objective"]),
            run_id=task_id,
            iteration=attempt,
            work_branch=work_branch,
            work_item_path=str(exchange_paths["work_item"]),
            result_path=str(exchange_paths["result"]),
            work_item=coord_resp.work_item,
        )

        _dbg(
            dbg_dir,
            "worker_start",
            {
                "iteration": attempt,
                "runner": worker_runner,
                "session": worker_session,
                "work_item_id": coord_resp.work_item.id,
                "work_item_kind": coord_resp.work_item.kind,
                "tokens_total": record.get("tokens_used_estimate"),
            },
        )
        worker_t0 = time.monotonic()

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

        worker_dt = round(time.monotonic() - worker_t0, 2)
        _dbg(
            dbg_dir,
            "worker_done",
            {
                "iteration": attempt,
                "duration_s": worker_dt,
                "rc": agent_result.returncode,
                "model_id": getattr(getattr(agent_result, "usage", None), "model_id", None),
                "stdout_chars": len(agent_result.stdout or ""),
                "stderr_chars": len(agent_result.stderr or ""),
            },
        )

        _accumulate_acpx_usage(
            request,
            session_name=worker_session,
            result=agent_result,
            actor="worker",
            branch=work_branch,
        )
        _sync_budget_to_record(record, request)
        hist = request.setdefault("history", {})
        tokens_used = int(hist.get("tokens_used_estimate") or 0)
        if max_tokens and tokens_used > max_tokens:
            return _fail_task(
                record,
                home=base_home,
                task_dir=task_dir,
                detail=f"Token breaker tripped after worker: tokens_used_estimate={tokens_used} > max_tokens={max_tokens}",
            )

        if agent_result.returncode != 0:
            _write_worker_raw_output(
                exchange_paths["raw_output"],
                iteration=attempt,
                runner=worker_runner,
                rc=agent_result.returncode,
                stdout=agent_result.stdout,
                stderr=agent_result.stderr,
            )
            _write_text(exchange_paths["error"], (agent_result.stderr or agent_result.stdout).strip())
            write_json(
                exchange_paths["status"],
                {
                    "run_id": task_id,
                    "work_item_id": coord_resp.work_item.id,
                    "iteration": attempt,
                    "runner": worker_runner,
                    "status": "failed",
                    "updated_at": now_iso(),
                },
            )
            append_event(
                exchange_paths["events"],
                "worker_nonzero_exit",
                {"iteration": attempt, "runner": worker_runner, "rc": agent_result.returncode},
            )
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

        try:
            outcome_kind, work_result = _load_worker_outcome(
                exchange_paths,
                expected_work_item_id=coord_resp.work_item.id,
                expected_branch=work_branch,
            )
        except ProtocolError as exc:
            _write_worker_raw_output(
                exchange_paths["raw_output"],
                iteration=attempt,
                runner=worker_runner,
                rc=agent_result.returncode,
                stdout=agent_result.stdout,
                stderr=agent_result.stderr,
            )
            _write_text(exchange_paths["error"], str(exc))
            write_json(
                exchange_paths["status"],
                {
                    "run_id": task_id,
                    "work_item_id": coord_resp.work_item.id,
                    "iteration": attempt,
                    "runner": worker_runner,
                    "status": "failed",
                    "updated_at": now_iso(),
                },
            )
            append_event(
                exchange_paths["events"],
                "worker_protocol_failure",
                {"iteration": attempt, "runner": worker_runner, "detail": str(exc)},
            )
            return _fail_task(
                record,
                home=base_home,
                task_dir=task_dir,
                detail=f"Worker protocol failure on iteration {attempt}: {exc}",
            )

        if debug:
            _write_worker_raw_output(
                exchange_paths["raw_output"],
                iteration=attempt,
                runner=worker_runner,
                rc=agent_result.returncode,
                stdout=agent_result.stdout,
                stderr=agent_result.stderr,
            )
        write_json(
            exchange_paths["status"],
            {
                "run_id": task_id,
                "work_item_id": coord_resp.work_item.id,
                "iteration": attempt,
                "runner": worker_runner,
                "status": ("handoff" if outcome_kind == "handoff" else work_result.status),
                "updated_at": now_iso(),
            },
        )
        append_event(
            exchange_paths["events"],
            "worker_outcome_loaded",
            {
                "iteration": attempt,
                "runner": worker_runner,
                "work_item_id": coord_resp.work_item.id,
                "outcome_kind": outcome_kind,
                "result_status": work_result.status,
            },
        )

        record["branch"] = work_result.branch
        record["head_sha"] = work_result.head_sha
        record["summary"] = work_result.summary
        record["worker_status"] = work_result.status
        record["tests_run"] = [
            {"command": t.command, "status": t.status, "details": t.details} for t in work_result.tests_run
        ]
        record["files_touched"] = list(work_result.files_touched)
        record["evidence"] = list(work_result.evidence)
        record["blockers"] = list(work_result.blockers)
        record["follow_up"] = list(work_result.follow_up)
        record["updated_at"] = now_iso()
        upsert_task(record, home=base_home)
        _dbg(
            dbg_dir,
            "worker_work_result",
            {
                "iteration": attempt,
                "branch": record.get("branch"),
                "head_sha": record.get("head_sha"),
                "summary": record.get("summary"),
                "work_result_status": work_result.status,
                "tests_run_count": len(work_result.tests_run),
                "tokens_total": record.get("tokens_used_estimate"),
            },
        )

        # Update coordinator state snapshot.
        request.setdefault("state", {})
        request["state"]["last_commit"] = work_result.head_sha
        request["state"]["diff_summary"] = work_result.summary
        request["state"]["latest_worker_result"] = _work_result_artifact(work_result)
        hist = request.setdefault("history", {})

        if outcome_kind == "handoff":
            request["state"]["latest_handoff"] = _work_result_artifact(work_result)
            notes = request["state"].setdefault("notes", [])
            if isinstance(notes, list):
                notes.append(f"handoff[{coord_resp.work_item.id}]={work_result.summary}")
                del notes[:-12]
            hist["no_progress_streak"] = 0
            last_failure_sig = None
            hist["last_iteration_seconds"] = round(time.monotonic() - iter_start, 2)
            _append_iteration_history_entry(
                request,
                iteration=attempt,
                work_item=coord_resp.work_item,
                selected_specialist=coord_resp.selected_specialist,
                worker_result=work_result,
                outcome="worker_handoff",
            )
            continue

        if work_result.status != "completed":
            failure_sig = f"worker:{work_result.status}:{'|'.join(work_result.blockers)}"
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

            _set_evaluation_state(
                request,
                status="fail",
                outcome=f"worker_{work_result.status}",
                worker_result=work_result,
                failing_checks=[
                    {
                        "name": "worker",
                        "kind": "worker",
                        "url": record.get("pr_url"),
                        "summary": "; ".join(work_result.blockers),
                    }
                ],
                logs_excerpt="; ".join(work_result.blockers),
            )
            _append_iteration_history_entry(
                request,
                iteration=attempt,
                work_item=coord_resp.work_item,
                selected_specialist=coord_resp.selected_specialist,
                worker_result=work_result,
                outcome=f"worker_{work_result.status}",
            )
            continue

        if attempt == 1:
            try:
                pr = gh.create_pull_request(
                    owner=owner,
                    repo=repo,
                    title=_task_title(verb, task_text, spec.title),
                    body=_build_pr_body(
                        repo_path=repo_path,
                        task_id=task_id,
                        summary=work_result.summary,
                        extra_body=spec.body,
                    ),
                    head=work_result.branch,
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
            _persist_record_checkpoint(record, home=base_home, checkpoint=CHECKPOINT_AFTER_PR_CREATED)
            _dbg(
                dbg_dir,
                "pr_created",
                {
                    "iteration": attempt,
                    "pr_url": record.get("pr_url"),
                    "pr_number": record.get("pr_number"),
                    "base_branch": base_branch,
                },
            )

        ci_log = task_dir / f"ci-iter-{attempt}.log"
        _dbg(
            dbg_dir,
            "ci_start",
            {
                "iteration": attempt,
                "head_sha": work_result.head_sha,
                "pr_url": record.get("pr_url"),
            },
        )
        ci_t0 = time.monotonic()
        _append_text(ci_log, f"[{now_iso()}] polling CI for {work_result.head_sha}")
        try:
            ci_state, ci_detail = _poll_ci(gh, owner, repo, work_result.head_sha, ci_log)
        except Exception as exc:  # noqa: BLE001
            detail = _format_preflight_error(exc)
            return _fail_task(
                record,
                home=base_home,
                task_dir=task_dir,
                detail=f"CI polling failed on iteration {attempt}: {detail}",
            )
        ci_dt = round(time.monotonic() - ci_t0, 2)
        _append_text(ci_log, f"[{now_iso()}] final {ci_state}: {ci_detail}")
        _dbg(
            dbg_dir,
            "ci_done",
            {
                "iteration": attempt,
                "duration_s": ci_dt,
                "ci_state": ci_state,
                "ci_detail": ci_detail,
            },
        )

        # Record history entry skeleton.
        if ci_state != "success":
            ci_checks: dict[str, Any] = {}
            ci_class = _classify_ci_failure(ci_state, ci_detail, None)
            infra_retries = 0
            while True:
                if str(record.get("head_sha") or "").strip():
                    try:
                        payload = gh.get_check_runs(owner, repo, str(record.get("head_sha")))
                        if isinstance(payload, dict):
                            ci_checks = payload
                    except Exception as exc:  # noqa: BLE001
                        _dbg(dbg_dir, "ci_check_runs_error", {"iteration": attempt, "detail": str(exc)})
                ci_class = _classify_ci_failure(ci_state, ci_detail, ci_checks)
                _dbg(
                    dbg_dir,
                    "ci_classification",
                    {"iteration": attempt, "ci_detail": ci_detail, **ci_class},
                )
                if ci_state == "success" or ci_class["classification"] != "infra_outage" or infra_retries >= 2:
                    break
                infra_retries += 1
                backoff = 30 * infra_retries
                _append_text(ci_log, f"[{now_iso()}] infra-outage suspected; backoff {backoff}s before retry {infra_retries}/2")
                time.sleep(backoff)
                ci_state, ci_detail = _poll_ci(
                    gh,
                    owner,
                    repo,
                    str(record["head_sha"]),
                    ci_log,
                    poll_seconds=45,
                    stuck_warn_seconds=20 * 60,
                    stuck_fail_seconds=45 * 60,
                )
                _append_text(ci_log, f"[{now_iso()}] infra-retry result {ci_state}: {ci_detail}")

            if ci_state != "success" and ci_class["classification"] == "infra_outage":
                return _fail_task(
                    record,
                    home=base_home,
                    task_dir=task_dir,
                    detail=(
                        "CI outage suspected after retries; no code/workflow FIRE attempted "
                        f"(reasons={','.join(ci_class['reason_codes']) or 'none'})"
                    ),
                )

            failure_sig = f"ci:{ci_detail}"
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

            ci_artifact = {
                "state": ci_state,
                "detail": ci_detail,
                "classification": ci_class,
            }
            request.setdefault("state", {})
            request["state"]["latest_ci"] = ci_artifact
            _set_evaluation_state(
                request,
                status="fail",
                outcome="ci_failure",
                worker_result=work_result,
                ci_state=ci_state,
                ci_detail=ci_detail,
                failing_checks=[{"name": "ci", "kind": "ci", "url": record.get("pr_url"), "summary": ci_detail}],
                logs_excerpt=ci_detail,
            )
            _append_iteration_history_entry(
                request,
                iteration=attempt,
                work_item=coord_resp.work_item,
                selected_specialist=coord_resp.selected_specialist,
                worker_result=work_result,
                outcome="ci_failure",
                ci=ci_artifact,
            )
            continue

        # CI success → review gate.
        request.setdefault("state", {})
        request["state"]["latest_ci"] = {"state": ci_state, "detail": ci_detail, "classification": None}
        _persist_record_checkpoint(
            record,
            home=base_home,
            checkpoint=CHECKPOINT_AFTER_CI_SUCCESS_BEFORE_REVIEW,
            updates={"ci_state": ci_state, "ci_detail": ci_detail},
        )

        diff_text = _read_diff_for_review(repo_path, base_branch, str(record["head_sha"]))
        _dbg(
            dbg_dir,
            "review_start",
            {
                "iteration": attempt,
                "head_sha": record.get("head_sha"),
                "diff_chars": len(diff_text or ""),
            },
        )
        review_t0 = time.monotonic()
        review_result, review_text = _run_review_with_retry(diff_text, debug_task_dir=dbg_dir)
        review_dt = round(time.monotonic() - review_t0, 2)

        _dbg(
            dbg_dir,
            "review_done",
            {
                "iteration": attempt,
                "duration_s": review_dt,
                "review_result": review_result,
                "has_blocker": review_result == "blocker",
                "review_first_line": (review_text.splitlines()[0][:200] if review_text else ""),
            },
        )

        review_attempt_path = task_dir / f"review-iter-{attempt}.txt"
        _write_text(review_attempt_path, review_text)

        if record["pr_number"] is None:
            raise RuntimeError("PR number missing; cannot post review comment")
        gh.post_issue_comment(owner, repo, int(record["pr_number"]), review_text)

        if review_result in {"tool-error", "malformed", "blocker"}:
            if review_result == "tool-error":
                detail = "review-tool-error"
            elif review_result == "malformed":
                detail = "review-malformed"
            else:
                detail = "review-blocker"
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

            review_artifact = {"result": review_result, "summary": review_text[:2000]}
            request.setdefault("state", {})
            request["state"]["latest_review"] = review_artifact
            _set_evaluation_state(
                request,
                status="fail",
                outcome=detail,
                worker_result=work_result,
                ci_state=ci_state,
                ci_detail=ci_detail,
                review_result=review_result,
                failing_checks=[{"name": "review", "kind": "review", "url": record.get("pr_url"), "summary": review_text[:2000]}],
                logs_excerpt=review_text[:2000],
            )
            _append_iteration_history_entry(
                request,
                iteration=attempt,
                work_item=coord_resp.work_item,
                selected_specialist=coord_resp.selected_specialist,
                worker_result=work_result,
                outcome=detail,
                ci={"state": ci_state, "detail": ci_detail, "classification": None},
                review=review_artifact,
            )
            continue

        # Success.
        request.setdefault("state", {})
        request["state"]["latest_review"] = {"result": review_result, "summary": review_text[:2000]}
        _set_evaluation_state(
            request,
            status="success",
            outcome="accepted",
            worker_result=work_result,
            ci_state=ci_state,
            ci_detail=ci_detail,
            review_result=review_result,
            failing_checks=[],
            logs_excerpt="",
        )
        hist["no_progress_streak"] = 0
        hist["last_iteration_seconds"] = round(time.monotonic() - iter_start, 2)
        hist.pop("failure_signatures", None)
        last_failure_sig = None
        _append_iteration_history_entry(
            request,
            iteration=attempt,
            work_item=coord_resp.work_item,
            selected_specialist=coord_resp.selected_specialist,
            worker_result=work_result,
            outcome="accepted",
            ci={"state": ci_state, "detail": ci_detail, "classification": None},
            review={"result": review_result, "summary": review_text[:2000]},
        )

        record["status"] = "ready"
        _persist_record_checkpoint(
            record,
            home=base_home,
            checkpoint=CHECKPOINT_AFTER_REVIEW_RESOLUTION,
            updates={"review_result": review_result},
        )
        _dbg(
            dbg_dir,
            "run_done",
            {
                "status": "ready",
                "pr_url": record.get("pr_url"),
                "tokens_total": record.get("tokens_used_estimate"),
                "duration_s": round(time.monotonic() - loop_start, 2),
            },
        )
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
