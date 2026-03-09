from __future__ import annotations

"""Run-scoped coordinator replay artifacts for the Velora ACP rewrite.

Phase 1 starts with a disciplined, explicit replay bundle that lives under the
existing repo-local run exchange directory. The coordinator will eventually read
from this bundle when using a direct runner; for now we seed it at run start and
refresh it across loop transitions so state is durable, inspectable, and ready
for later wiring.
"""

import json
from pathlib import Path
from typing import Any

from .exchange import run_exchange_dir
from .util import ensure_dir, now_iso


def coordinator_replay_paths(repo_path: Path, run_id: str) -> dict[str, Path]:
    base = run_exchange_dir(repo_path, run_id)
    return {
        "dir": base,
        "brief": base / "coordinator-brief.json",
        "memory": base / "coordinator-memory.md",
        "history": base / "iteration-history.jsonl",
    }


def append_run_replay_event(
    repo_path: Path,
    run_id: str,
    *,
    iteration: int,
    event: str,
    data: dict[str, Any] | None = None,
) -> None:
    paths = coordinator_replay_paths(repo_path, run_id)
    payload: dict[str, Any] = {
        "schema_version": 1,
        "ts": now_iso(),
        "run_id": run_id,
        "iteration": int(iteration),
        "event": event,
    }
    if data:
        payload["data"] = data
    with paths["history"].open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, sort_keys=True))
        fh.write("\n")


def _as_dict(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _truncate(text: object, limit: int = 200) -> str:
    s = str(text or "").strip()
    if len(s) <= limit:
        return s
    return s[: limit - 1].rstrip() + "…"


def _quality_gate_from_tests(latest_worker: dict[str, Any]) -> str:
    tests = latest_worker.get("tests_run")
    if not isinstance(tests, list) or not tests:
        return "unknown"
    statuses = {str(_as_dict(t).get("status") or "") for t in tests}
    if "fail" in statuses:
        return "fail"
    if "pass" in statuses:
        return "pass"
    if "not_run" in statuses:
        return "not_run"
    return "unknown"


def _quality_gate_from_ci(latest_ci: dict[str, Any]) -> str:
    state = str(latest_ci.get("state") or "").lower()
    if not state:
        return "unknown"
    if state == "success":
        return "pass"
    if state in {"failure", "failed", "error", "cancelled", "timed_out"}:
        return "fail"
    return "unknown"


def _extract_latest_work_item(latest_coord: dict[str, Any]) -> dict[str, Any] | None:
    work_item = _as_dict(latest_coord.get("work_item"))
    selected = _as_dict(latest_coord.get("selected_specialist"))
    if not work_item:
        return None
    scope_hints = _as_dict(work_item.get("scope_hints"))
    likely_files = scope_hints.get("likely_files")
    files = likely_files if isinstance(likely_files, list) else []
    return {
        "id": work_item.get("id"),
        "kind": work_item.get("kind"),
        "runner": selected.get("runner"),
        "role": selected.get("role"),
        "summary": _truncate(work_item.get("rationale") or ""),
        "files": [str(x) for x in files[:5]],
    }


def _extract_latest_outcome(state: dict[str, Any]) -> dict[str, Any] | None:
    latest_review = _as_dict(state.get("latest_review"))
    if latest_review:
        return {
            "kind": "review_result",
            "summary": _truncate(f"{latest_review.get('result')}: {latest_review.get('summary')}", limit=240),
            "head_sha": None,
        }

    latest_ci = _as_dict(state.get("latest_ci"))
    if latest_ci:
        return {
            "kind": "ci_result",
            "summary": _truncate(f"{latest_ci.get('state')}: {latest_ci.get('detail')}", limit=240),
            "head_sha": None,
        }

    latest_worker = _as_dict(state.get("latest_worker_result"))
    if latest_worker:
        return {
            "kind": f"worker_{latest_worker.get('status')}",
            "summary": _truncate(latest_worker.get("summary") or "", limit=240),
            "head_sha": latest_worker.get("head_sha"),
        }

    return None


def _extract_open_loops(state: dict[str, Any]) -> list[str]:
    loops: list[str] = []
    latest_coord = _as_dict(state.get("latest_coordinator_decision"))
    latest_worker = _as_dict(state.get("latest_worker_result"))
    latest_ci = _as_dict(state.get("latest_ci"))
    latest_review = _as_dict(state.get("latest_review"))

    work_item = _as_dict(latest_coord.get("work_item"))
    work_item_id = str(work_item.get("id") or "").strip()
    latest_worker_id = str(latest_worker.get("work_item_id") or "").strip()
    if latest_coord.get("decision") == "execute_work_item" and work_item_id and latest_worker_id != work_item_id:
        loops.append(f"Await worker result for {work_item_id}")

    if latest_ci and str(latest_ci.get("state") or "").lower() != "success":
        loops.append(_truncate(f"Resolve CI: {latest_ci.get('detail')}", limit=160))

    review_result = str(latest_review.get("result") or "")
    if review_result in {"blocker", "tool-error", "malformed"}:
        loops.append(_truncate(f"Resolve review: {latest_review.get('summary')}", limit=160))

    follow_up = latest_worker.get("follow_up")
    if isinstance(follow_up, list):
        for item in follow_up[:3]:
            if item:
                loops.append(_truncate(item, limit=160))

    deduped: list[str] = []
    seen: set[str] = set()
    for loop in loops:
        key = loop.strip()
        if key and key not in seen:
            deduped.append(key)
            seen.add(key)
    return deduped[:5]


def _extract_blockers(state: dict[str, Any]) -> list[str]:
    latest_worker = _as_dict(state.get("latest_worker_result"))
    blockers = latest_worker.get("blockers")
    if not isinstance(blockers, list):
        return []
    return [_truncate(x, limit=160) for x in blockers[:5] if str(x).strip()]


def build_coordinator_brief(
    *,
    request: dict[str, Any],
    max_attempts: int,
    verb: str | None = None,
) -> dict[str, Any]:
    repo = _as_dict(request.get("repo") if isinstance(request, dict) else {})
    history = _as_dict(request.get("history") if isinstance(request, dict) else {})
    state = _as_dict(request.get("state") if isinstance(request, dict) else {})
    objective = request.get("objective") if isinstance(request, dict) else None
    current_iteration = int(request.get("iteration") or 0) if isinstance(request, dict) else 0

    latest_coord = _as_dict(state.get("latest_coordinator_decision"))
    latest_terminal = _as_dict(state.get("run_terminal"))
    latest_ci = _as_dict(state.get("latest_ci"))
    latest_worker = _as_dict(state.get("latest_worker_result"))

    status_state = "starting"
    if latest_terminal:
        decision = str(latest_terminal.get("decision") or "")
        status_state = "ready" if decision == "finalize_success" else "failed"
    elif latest_coord:
        status_state = "running"
    elif current_iteration > 1:
        status_state = "running"

    last_decision = latest_terminal.get("decision") or latest_coord.get("decision") or None
    last_reason = latest_terminal.get("reason") or latest_coord.get("reason") or ""

    return {
        "schema_version": 1,
        "run_id": request.get("run_id"),
        "repo": {
            "owner": repo.get("owner"),
            "name": repo.get("name"),
            "base_branch": repo.get("default_branch"),
            "work_branch": repo.get("work_branch"),
        },
        "objective": {
            "verb": verb,
            "summary": str(objective or ""),
        },
        "iteration": {
            "current": current_iteration,
            "max": int(max_attempts),
            "no_progress_streak": int(history.get("no_progress_streak") or 0),
        },
        "status": {
            "state": status_state,
            "last_decision": last_decision,
            "last_reason": str(last_reason),
        },
        "latest_work_item": _extract_latest_work_item(latest_coord),
        "latest_outcome": _extract_latest_outcome(state),
        "quality_gates": {
            "tests": _quality_gate_from_tests(latest_worker),
            "lint": "unknown",
            "security": "unknown",
            "ci": _quality_gate_from_ci(latest_ci),
            "docs": "unknown",
        },
        "open_loops": _extract_open_loops(state),
        "blockers": _extract_blockers(state),
        "updated_at": now_iso(),
    }


def render_coordinator_memory(brief: dict[str, Any]) -> str:
    repo = _as_dict(brief.get("repo"))
    objective = _as_dict(brief.get("objective"))
    iteration = _as_dict(brief.get("iteration"))
    status = _as_dict(brief.get("status"))
    latest_work_item = _as_dict(brief.get("latest_work_item"))
    latest_outcome = _as_dict(brief.get("latest_outcome"))

    current = int(iteration.get("current") or 0)
    max_attempts = int(iteration.get("max") or 0)
    run_id = brief.get("run_id") or ""
    repo_ref = f"{repo.get('owner')}/{repo.get('name')}" if repo.get("owner") and repo.get("name") else "unknown"

    lines = [
        "# Coordinator Replay",
        "",
        f"Run: {run_id}",
        f"Repo: {repo_ref}",
        f"Iteration: {current} of {max_attempts}",
        "",
        "## Objective",
        str(objective.get("summary") or ""),
        "",
        "## Current state",
        f"- Status: {status.get('state') or 'unknown'}",
    ]

    last_decision = status.get("last_decision")
    if last_decision:
        lines.append(f"- Last decision: {last_decision}")
    else:
        lines.append("- No coordinator decisions recorded yet")

    if latest_work_item:
        work_item_id = latest_work_item.get("id") or "unknown"
        kind = latest_work_item.get("kind") or "unknown"
        runner = latest_work_item.get("runner") or "unknown"
        role = latest_work_item.get("role") or "unknown"
        lines.append(f"- Last work item: {work_item_id} {kind} via {runner}/{role}")
    else:
        lines.append("- No work items dispatched yet")

    if latest_outcome:
        lines.append(f"- Latest outcome: {latest_outcome.get('summary') or 'none'}")

    open_loops = brief.get("open_loops")
    if isinstance(open_loops, list) and open_loops:
        lines.extend(["", "## Open loops"])
        for loop in open_loops:
            lines.append(f"- {loop}")

    blockers = brief.get("blockers")
    if isinstance(blockers, list) and blockers:
        lines.extend(["", "## Active blockers"])
        for blocker in blockers:
            lines.append(f"- {blocker}")

    lines.extend(
        [
            "",
            "## Important cautions",
            "- CoordinatorRequest is authoritative if anything here conflicts.",
            "- Keep worker instructions succinct, bounded, and acceptance-driven.",
            "",
        ]
    )
    return "\n".join(lines)


def write_coordinator_brief(repo_path: Path, run_id: str, brief: dict[str, Any]) -> Path:
    paths = coordinator_replay_paths(repo_path, run_id)
    ensure_dir(paths["brief"].parent)
    paths["brief"].write_text(json.dumps(brief, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return paths["brief"]


def write_coordinator_memory(repo_path: Path, run_id: str, memory_text: str) -> Path:
    paths = coordinator_replay_paths(repo_path, run_id)
    ensure_dir(paths["memory"].parent)
    paths["memory"].write_text(memory_text, encoding="utf-8")
    return paths["memory"]


def sync_run_replay(
    repo_path: Path,
    *,
    request: dict[str, Any],
    max_attempts: int,
    verb: str | None = None,
) -> dict[str, Path]:
    run_id = str(request.get("run_id") or "")
    if not run_id:
        raise ValueError("request.run_id is required to sync replay artifacts")

    paths = coordinator_replay_paths(repo_path, run_id)
    brief = build_coordinator_brief(request=request, max_attempts=max_attempts, verb=verb)
    memory_text = render_coordinator_memory(brief)
    write_coordinator_brief(repo_path, run_id, brief)
    write_coordinator_memory(repo_path, run_id, memory_text)
    return paths


def seed_run_replay(
    repo_path: Path,
    *,
    request: dict[str, Any],
    max_attempts: int,
    verb: str | None = None,
) -> dict[str, Path]:
    run_id = str(request.get("run_id") or "")
    if not run_id:
        raise ValueError("request.run_id is required to seed replay artifacts")

    brief = build_coordinator_brief(request=request, max_attempts=max_attempts, verb=verb)

    append_run_replay_event(
        repo_path,
        run_id,
        iteration=0,
        event="run_started",
        data={
            "repo": {
                "owner": brief["repo"].get("owner"),
                "name": brief["repo"].get("name"),
            },
            "objective": brief["objective"].get("summary"),
            "verb": brief["objective"].get("verb"),
        },
    )
    return sync_run_replay(repo_path, request=request, max_attempts=max_attempts, verb=verb)
