from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .util import ensure_dir, now_iso


_EXCHANGE_DIRNAME = ".velora"


def repo_exchange_root(repo_path: Path) -> Path:
    return repo_path / _EXCHANGE_DIRNAME / "exchange"


def run_exchange_dir(repo_path: Path, run_id: str) -> Path:
    return ensure_dir(repo_exchange_root(repo_path) / "runs" / run_id)


def work_item_exchange_dir(repo_path: Path, run_id: str, work_item_id: str) -> Path:
    return ensure_dir(run_exchange_dir(repo_path, run_id) / work_item_id)


def work_item_exchange_paths(repo_path: Path, run_id: str, work_item_id: str) -> dict[str, Path]:
    base = work_item_exchange_dir(repo_path, run_id, work_item_id)
    return {
        "dir": base,
        "work_item": base / "work-item.json",
        "status": base / "status.json",
        "result": base / "result.json",
        "handoff": base / "handoff.json",
        "block": base / "block.json",
        "events": base / "events.jsonl",
        "raw_output": base / "raw-output.txt",
        "error": base / "error.txt",
    }


def write_json(path: Path, payload: Any) -> None:
    ensure_dir(path.parent)
    path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def append_event(path: Path, event: str, data: dict[str, Any] | None = None) -> None:
    ensure_dir(path.parent)
    payload: dict[str, Any] = {"ts": now_iso(), "event": event}
    if data:
        payload.update(data)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, sort_keys=True))
        fh.write("\n")
