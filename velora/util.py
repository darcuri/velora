from __future__ import annotations

import datetime as _dt
import os
import uuid
from pathlib import Path


def velora_home() -> Path:
    env_home = os.environ.get("VELORA_HOME")
    if env_home:
        return Path(env_home).expanduser()
    return Path.home() / ".velora"


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def now_iso() -> str:
    return _dt.datetime.now(tz=_dt.timezone.utc).replace(microsecond=0).isoformat()


def repo_slug(owner: str, repo: str) -> str:
    return f"{owner}__{repo}"


def build_task_id() -> str:
    ts = _dt.datetime.now(tz=_dt.timezone.utc).strftime("%Y%m%d%H%M%S")
    return f"{ts}-{uuid.uuid4().hex[:8]}"

