from __future__ import annotations

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

