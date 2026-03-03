from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path
from typing import Any

from .util import ensure_dir, velora_home


def tasks_file(home: Path | None = None) -> Path:
    base = home or velora_home()
    return base / "tasks.json"


def load_tasks(home: Path | None = None) -> dict[str, Any]:
    path = tasks_file(home)
    if not path.exists():
        return {"version": 1, "tasks": []}
    with path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    if "tasks" not in data or not isinstance(data["tasks"], list):
        raise ValueError(f"Invalid task registry format in {path}")
    if "version" not in data:
        data["version"] = 1
    return data


def save_tasks(registry: dict[str, Any], home: Path | None = None) -> None:
    path = tasks_file(home)
    ensure_dir(path.parent)
    with path.open("w", encoding="utf-8") as fh:
        json.dump(registry, fh, indent=2, sort_keys=True)
        fh.write("\n")


def upsert_task(task_record: dict[str, Any], home: Path | None = None) -> dict[str, Any]:
    registry = load_tasks(home)
    tasks = registry["tasks"]
    for idx, item in enumerate(tasks):
        if item.get("task_id") == task_record.get("task_id"):
            tasks[idx] = task_record
            break
    else:
        tasks.append(task_record)
    save_tasks(registry, home)
    return task_record


def get_status_view(home: Path | None = None, recent_limit: int = 10) -> dict[str, list[dict[str, Any]]]:
    registry = load_tasks(home)
    sorted_tasks = sorted(registry["tasks"], key=lambda x: x.get("updated_at", ""), reverse=True)
    active = [t for t in sorted_tasks if t.get("status") in {"running", "queued", "reviewing"}]
    recent = sorted_tasks[:recent_limit]
    return {"active": active, "recent": recent}


def _parse_iso(ts: str) -> _dt.datetime | None:
    """Parse ISO8601 timestamps produced by velora.util.now_iso()."""

    try:
        dt = _dt.datetime.fromisoformat(ts)
    except Exception:  # noqa: BLE001
        return None
    if dt.tzinfo is None:
        # Treat naive as UTC.
        dt = dt.replace(tzinfo=_dt.timezone.utc)
    return dt


def prune_stale_tasks(
    *,
    older_than_hours: int = 24,
    dry_run: bool = False,
    home: Path | None = None,
) -> dict[str, Any]:
    """Mark old "running"-like tasks as stale.

    This is a non-destructive cleanup that only changes task metadata in tasks.json.
    """

    if older_than_hours < 1:
        raise ValueError("older_than_hours must be >= 1")

    registry = load_tasks(home)
    now = _dt.datetime.now(tz=_dt.timezone.utc)
    cutoff = now - _dt.timedelta(hours=older_than_hours)

    changed: list[str] = []
    active_statuses = {"running", "queued", "reviewing"}

    for task in registry.get("tasks", []):
        if task.get("status") not in active_statuses:
            continue
        updated_at = task.get("updated_at") or task.get("created_at")
        if not isinstance(updated_at, str):
            continue
        dt = _parse_iso(updated_at)
        if dt is None:
            continue
        if dt > cutoff:
            continue

        task_id = str(task.get("task_id") or "")
        if task_id:
            changed.append(task_id)
        if not dry_run:
            task["status"] = "stale"
            task["stale_at"] = now.replace(microsecond=0).isoformat()
            task["updated_at"] = now.replace(microsecond=0).isoformat()

    if changed and not dry_run:
        save_tasks(registry, home)

    return {
        "status": "ok",
        "dry_run": dry_run,
        "older_than_hours": older_than_hours,
        "stale_marked": changed,
        "count": len(changed),
    }


def get_task(task_id: str, home: Path | None = None) -> dict[str, Any] | None:
    """Fetch a task record by id from tasks.json."""

    tid = str(task_id or "").strip()
    if not tid:
        raise ValueError("task_id must be a non-empty string")

    reg = load_tasks(home)
    for task in reg.get("tasks", []):
        if task.get("task_id") == tid:
            return task
    return None

