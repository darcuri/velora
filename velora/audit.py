from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


RUN_START = "run_start"
ITERATION_START = "iteration_start"
DECISION_MADE = "decision_made"
ITERATION_END = "iteration_end"
RUN_END = "run_end"
WORK_ITEM_DISPATCHED = "work_item_dispatched"
WORK_ITEM_COMPLETED = "work_item_completed"
WORKER_BLOCKED = "worker_blocked"
WORKER_FAILED = "worker_failed"
CI_RESULT = "ci_result"
REVIEW_RESULT = "review_result"
REVIEW_STARTED = "review_started"
REVIEW_COMPLETED = "review_completed"
RUN_TERMINAL = "run_terminal"
REVIEW_REQUESTED = "review_requested"
FINDING_DISMISSED = "finding_dismissed"

# Back-compat aliases from the initial audit helper naming.
RUN_STARTED = RUN_START
COORDINATOR_DECISION = DECISION_MADE
WORKER_COMPLETED = WORK_ITEM_COMPLETED


@dataclass(frozen=True)
class AuditEvent:
    run_id: str
    iteration: int
    event_type: str
    timestamp: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class AuditLog:
    run_id: str
    events: list[AuditEvent]


@dataclass(frozen=True)
class AuditSummary:
    run_id: str
    objective_snippet: str
    iterations: list[int]
    decisions: list[str]
    final_status: str
    event_count: int


def _audit_run_dir(run_id: str, base_dir: Path | None = None) -> Path:
    root = base_dir if base_dir is not None else Path.cwd()
    return root / ".velora" / "runs" / run_id


def audit_log_path(run_id: str, base_dir: Path | None = None) -> Path:
    return _audit_run_dir(run_id, base_dir=base_dir) / "audit.jsonl"


def _sanitize_payload(payload: dict[str, Any]) -> dict[str, Any]:
    sensitive_parts = ("token", "secret", "password", "auth", "api_key", "apikey", "credential", "header", "cookie", "env")
    out: dict[str, Any] = {}
    for key, value in payload.items():
        lowered = str(key).strip().lower()
        out[str(key)] = "[REDACTED]" if any(part in lowered for part in sensitive_parts) else value
    return out


def append_event(run_id: str, event: AuditEvent, base_dir: Path | None = None) -> Path:
    path = audit_log_path(run_id, base_dir=base_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    row = asdict(event)
    row["payload"] = _sanitize_payload(dict(row.get("payload") or {}))
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, sort_keys=True))
        fh.write("\n")
        fh.flush()
    return path


def load_events(run_id: str, base_dir: Path | None = None) -> list[AuditEvent]:
    path = audit_log_path(run_id, base_dir=base_dir)
    if not path.exists():
        return []
    events: list[AuditEvent] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        if not raw_line.strip():
            continue
        row = json.loads(raw_line)
        events.append(
            AuditEvent(
                run_id=str(row["run_id"]),
                iteration=int(row["iteration"]),
                event_type=str(row["event_type"]),
                timestamp=str(row["timestamp"]),
                payload=dict(row.get("payload") or {}),
            )
        )
    return events


def latest_run_id(base_dir: Path | None = None) -> str | None:
    root = (base_dir if base_dir is not None else Path.cwd()) / ".velora" / "runs"
    if not root.exists():
        return None
    candidates: list[tuple[float, str]] = []
    for child in root.iterdir():
        path = child / "audit.jsonl"
        if not child.is_dir() or not path.exists():
            continue
        try:
            candidates.append((path.stat().st_mtime, child.name))
        except OSError:
            continue
    if not candidates:
        return None
    candidates.sort()
    return candidates[-1][1]


def summarize(events: list[AuditEvent]) -> AuditSummary:
    if not events:
        return AuditSummary(
            run_id="unknown",
            objective_snippet="",
            iterations=[],
            decisions=[],
            final_status="unknown",
            event_count=0,
        )

    run_id = events[0].run_id
    objective_snippet = ""
    iterations = sorted({event.iteration for event in events if event.iteration > 0})
    decisions: list[str] = []
    final_status = "unknown"

    for event in events:
        payload = event.payload
        if event.event_type in {RUN_START, RUN_STARTED} and not objective_snippet:
            objective_snippet = str(payload.get("objective_snippet") or payload.get("objective") or "")
        elif event.event_type in {DECISION_MADE, COORDINATOR_DECISION}:
            decision = str(payload.get("decision") or "").strip()
            reason = str(payload.get("reason") or "").strip()
            if decision:
                decisions.append(f"{decision}: {reason}" if reason else decision)
        elif event.event_type in {RUN_END, RUN_TERMINAL}:
            final_status = str(payload.get("status") or payload.get("outcome") or payload.get("decision") or final_status)

    return AuditSummary(
        run_id=run_id,
        objective_snippet=objective_snippet,
        iterations=iterations,
        decisions=decisions,
        final_status=final_status,
        event_count=len(events),
    )


def generate_summary(events: list[AuditEvent]) -> str:
    if not events:
        return "No audit events recorded."

    run_id = events[0].run_id
    run_started = next((event for event in events if event.event_type in {RUN_START, RUN_STARTED}), None)
    repo = ""
    branch = ""
    if run_started is not None:
        repo = str(run_started.payload.get("repo") or "")
        branch = str(run_started.payload.get("branch") or run_started.payload.get("work_branch") or "")

    iterations = sorted({event.iteration for event in events})
    dispatched: dict[str, dict[str, str]] = {}
    ci_status = "unknown"
    review_status = "unknown"
    terminal_status = "unknown"

    for event in events:
        payload = event.payload
        if event.event_type == WORK_ITEM_DISPATCHED:
            work_item_id = str(payload.get("work_item_id") or payload.get("id") or "unknown")
            dispatched[work_item_id] = {
                "kind": str(payload.get("kind") or "unknown"),
                "runner": str(payload.get("runner") or "unknown"),
                "backend": str(payload.get("backend") or "unknown"),
                "outcome": "pending",
            }
        elif event.event_type in {WORK_ITEM_COMPLETED, WORKER_COMPLETED, WORKER_BLOCKED, WORKER_FAILED}:
            work_item_id = str(payload.get("work_item_id") or payload.get("id") or "unknown")
            outcome = str(payload.get("status") or payload.get("outcome") or "")
            if not outcome:
                outcome = event.event_type.removeprefix("worker_").removeprefix("work_item_")
            if work_item_id not in dispatched:
                dispatched[work_item_id] = {"kind": "unknown", "runner": "unknown", "backend": "unknown", "outcome": outcome}
            else:
                dispatched[work_item_id]["outcome"] = outcome
        elif event.event_type == CI_RESULT:
            ci_status = str(payload.get("status") or payload.get("outcome") or ci_status)
        elif event.event_type in {REVIEW_RESULT, REVIEW_COMPLETED}:
            review_status = str(payload.get("status") or payload.get("outcome") or review_status)
        elif event.event_type in {RUN_END, RUN_TERMINAL}:
            terminal_status = str(payload.get("status") or payload.get("outcome") or terminal_status)

    lines = [
        f"# Run Audit Summary: {run_id}",
        "",
        "## Context",
        f"- Run ID: `{run_id}`",
        f"- Repo: `{repo or 'unknown'}`",
        f"- Branch: `{branch or 'unknown'}`",
        f"- Iterations touched: `{', '.join(str(i) for i in iterations)}`",
        "",
        "## Work Items",
    ]

    if dispatched:
        for work_item_id in sorted(dispatched):
            item = dispatched[work_item_id]
            lines.append(
                "- "
                f"`{work_item_id}` kind=`{item['kind']}` runner=`{item['runner']}` "
                f"backend=`{item['backend']}` outcome=`{item['outcome']}`"
            )
    else:
        lines.append("- none")

    lines.extend(
        [
            "",
            "## Outcomes",
            f"- CI result: `{ci_status}`",
            f"- Review result: `{review_status}`",
            f"- Terminal status: `{terminal_status}`",
            "",
            "## Timeline",
        ]
    )

    for event in events:
        lines.append(f"- `{event.timestamp}` iteration=`{event.iteration}` event=`{event.event_type}`")

    return "\n".join(lines) + "\n"


def write_summary(run_id: str, events: list[AuditEvent], base_dir: Path | None = None) -> Path:
    path = _audit_run_dir(run_id, base_dir=base_dir) / "summary.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(generate_summary(events), encoding="utf-8")
    return path
