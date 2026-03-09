from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


RUN_STARTED = "run_started"
COORDINATOR_DECISION = "coordinator_decision"
WORK_ITEM_DISPATCHED = "work_item_dispatched"
WORKER_COMPLETED = "worker_completed"
WORKER_BLOCKED = "worker_blocked"
WORKER_FAILED = "worker_failed"
CI_RESULT = "ci_result"
REVIEW_RESULT = "review_result"
RUN_TERMINAL = "run_terminal"


@dataclass(frozen=True)
class AuditEvent:
    run_id: str
    iteration: int
    event_type: str
    timestamp: str
    payload: dict[str, Any]


def _audit_run_dir(run_id: str, base_dir: Path | None = None) -> Path:
    root = base_dir if base_dir is not None else Path.cwd()
    return root / ".velora" / "audit" / run_id


def append_event(run_id: str, event: AuditEvent, base_dir: Path | None = None) -> Path:
    path = _audit_run_dir(run_id, base_dir=base_dir) / "events.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(asdict(event), sort_keys=True))
        fh.write("\n")
        fh.flush()
    return path


def load_events(run_id: str, base_dir: Path | None = None) -> list[AuditEvent]:
    path = _audit_run_dir(run_id, base_dir=base_dir) / "events.jsonl"
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


def generate_summary(events: list[AuditEvent]) -> str:
    if not events:
        return "No audit events recorded."

    run_id = events[0].run_id
    run_started = next((event for event in events if event.event_type == RUN_STARTED), None)
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
        elif event.event_type in {WORKER_COMPLETED, WORKER_BLOCKED, WORKER_FAILED}:
            work_item_id = str(payload.get("work_item_id") or payload.get("id") or "unknown")
            outcome = event.event_type.removeprefix("worker_")
            if work_item_id not in dispatched:
                dispatched[work_item_id] = {"kind": "unknown", "runner": "unknown", "backend": "unknown", "outcome": outcome}
            else:
                dispatched[work_item_id]["outcome"] = outcome
        elif event.event_type == CI_RESULT:
            ci_status = str(payload.get("status") or payload.get("outcome") or ci_status)
        elif event.event_type == REVIEW_RESULT:
            review_status = str(payload.get("status") or payload.get("outcome") or review_status)
        elif event.event_type == RUN_TERMINAL:
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
