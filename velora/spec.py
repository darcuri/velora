from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class RunSpec:
    task: str
    title: str | None = None
    body: str | None = None
    max_attempts: int | None = None


def load_run_spec(spec_path: str) -> RunSpec:
    """Load a VELORA run spec from JSON.

    Use this to avoid exposing long prompts in the process list.

    Spec format (v0):
      {
        "task": "..."                   # required
        "title": "..."                  # optional (PR title override)
        "body": "..."                   # optional (PR body extra text)
        "max_attempts": 3                # optional (FIRE attempts override)
      }

    `spec_path` may be '-' to read from stdin.
    """

    if spec_path == "-":
        raw = sys.stdin.read()
    else:
        raw = Path(spec_path).expanduser().read_text(encoding="utf-8")

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON spec: {exc}") from exc

    if not isinstance(payload, dict):
        raise ValueError("JSON spec must be an object")

    task = payload.get("task")
    if not isinstance(task, str) or not task.strip():
        raise ValueError("JSON spec missing required non-empty 'task' string")

    title = payload.get("title")
    if title is not None and (not isinstance(title, str) or not title.strip()):
        raise ValueError("If provided, 'title' must be a non-empty string")

    body = payload.get("body")
    if body is not None and (not isinstance(body, str) or not body.strip()):
        raise ValueError("If provided, 'body' must be a non-empty string")

    max_attempts = payload.get("max_attempts")
    if max_attempts is not None:
        if not isinstance(max_attempts, int) or max_attempts < 1 or max_attempts > 10:
            raise ValueError("If provided, 'max_attempts' must be an int between 1 and 10")

    return RunSpec(task=task.strip(), title=title.strip() if isinstance(title, str) else None, body=body.strip() if isinstance(body, str) else None, max_attempts=max_attempts)
