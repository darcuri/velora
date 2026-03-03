from __future__ import annotations

import json
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from .util import velora_home


@dataclass(frozen=True)
class VeloraConfig:
    allowed_owners: set[str]
    max_attempts: int

    # Mode A policy defaults (coordinator loop).
    mode_a_max_cost_usd: int
    mode_a_no_progress_max: int
    mode_a_max_wall_seconds: int

    # Coordinator-selected specialist policy.
    # JSON shape: { role: {"runners": ["codex"|"claude"], "models": ["..."]} }
    specialist_matrix: dict[str, dict[str, list[str]]]

    runner: str  # codex | claude
    codex_session_prefix: str
    claude_session_prefix: str

    vault_addr: str
    vault_role_id_file: Path
    vault_secret_id_file: Path
    vault_api_keys_path: str

    acpx_cmd: str | None
    acpx_fallback: Path | None


def _default_config_paths() -> list[Path]:
    # Explicit override (single source of truth).
    raw = os.environ.get("VELORA_CONFIG_PATH", "").strip()
    if raw:
        return [Path(raw).expanduser()]

    paths: list[Path] = []

    xdg = os.environ.get("XDG_CONFIG_HOME", "").strip()
    if xdg:
        base = Path(xdg).expanduser()
    else:
        base = Path.home() / ".config"
    paths.append(base / "velora" / "config.json")

    # Back-compat / simple local override location.
    paths.append(velora_home() / "config.json")

    return paths


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in config file {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"Config file {path} must contain a JSON object")
    return data


def _parse_owners(value: object) -> set[str]:
    if value is None:
        return set()
    if isinstance(value, str):
        parts = [p.strip() for p in value.split(",")]
        return {p for p in parts if p}
    if isinstance(value, list):
        owners: set[str] = set()
        for item in value:
            if isinstance(item, str) and item.strip():
                owners.add(item.strip())
        return owners
    raise ValueError("allowed_owners must be a comma-separated string or a list of strings")


def _parse_int(value: object, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return int(value)
        except ValueError as exc:
            raise ValueError(f"Expected int, got {value!r}") from exc
    return default


def _parse_specialist_matrix(value: object, default: dict[str, Any]) -> dict[str, dict[str, list[str]]]:
    """Parse the coordinator-selected specialist policy matrix.

    Shape:
      { role: {"runners": ["codex"|"claude"], "models": ["..."]} }

    Notes:
    - Unknown roles are rejected (hard fail).
    - Missing roles fall back to defaults.
    """

    allowed_roles = {"implementer", "docs", "refactor", "investigator"}
    allowed_runners = {"codex", "claude"}

    if value is None:
        raw = dict(default)
    elif isinstance(value, dict):
        raw = dict(default)
        raw.update(value)
    else:
        raise ValueError("specialist_matrix must be an object")

    matrix: dict[str, dict[str, list[str]]] = {}

    for role, rule in raw.items():
        if role not in allowed_roles:
            raise ValueError(f"specialist_matrix has unknown role: {role}")
        if not isinstance(rule, dict):
            raise ValueError(f"specialist_matrix[{role}] must be an object")

        runners_raw = rule.get("runners")
        if not isinstance(runners_raw, list) or not runners_raw:
            raise ValueError(f"specialist_matrix[{role}].runners must be a non-empty list")
        runners: list[str] = []
        for r in runners_raw:
            if not isinstance(r, str) or not r.strip():
                raise ValueError(f"specialist_matrix[{role}].runners must contain strings")
            rs = r.strip().lower()
            if rs not in allowed_runners:
                raise ValueError(f"specialist_matrix[{role}].runners contains invalid runner: {rs}")
            runners.append(rs)

        models_raw = rule.get("models", [])
        if models_raw is None:
            models_raw = []
        if not isinstance(models_raw, list):
            raise ValueError(f"specialist_matrix[{role}].models must be a list")
        models: list[str] = []
        for m in models_raw:
            if not isinstance(m, str) or not m.strip():
                raise ValueError(f"specialist_matrix[{role}].models must contain strings")
            models.append(m.strip())

        matrix[role] = {"runners": runners, "models": models}

    return matrix


def load_config() -> VeloraConfig:
    # Defaults.
    defaults = {
        # Safety: default-deny; user must explicitly allow owners via config/env.
        "allowed_owners": [],
        "max_attempts": 3,

        # Mode A policy defaults.
        "mode_a_max_cost_usd": 20,
        "mode_a_no_progress_max": 4,
        "mode_a_max_wall_seconds": 30 * 60,

        # Coordinator-selected specialist policy.
        "specialist_matrix": {
            "implementer": {"runners": ["codex"], "models": []},
            "docs": {"runners": ["codex", "claude"], "models": []},
            "refactor": {"runners": ["codex"], "models": []},
            "investigator": {"runners": ["codex", "claude"], "models": []},
        },

        "runner": "codex",
        "codex_session_prefix": "velora-codex-",
        "claude_session_prefix": "velora-claude-",
        # Generic Vault default. Prefer VELORA_VAULT_ADDR/VAULT_ADDR; fall back to local dev.
        "vault_addr": "http://127.0.0.1:8200",
        # Optional AppRole convenience defaults (override via env/config).
        "vault_role_id_file": str(velora_home() / "vault-role-id"),
        "vault_secret_id_file": str(velora_home() / "vault-secret-id"),
        "vault_api_keys_path": "/v1/secret/data/velora/api-keys",
        "acpx_cmd": None,
        "acpx_fallback": None,
    }

    # File config (first existing wins).
    file_cfg: dict[str, Any] = {}
    for path in _default_config_paths():
        if path.exists():
            file_cfg = _load_json(path)
            break

    # Env config.
    env = os.environ
    env_cfg: dict[str, Any] = {}
    if env.get("VELORA_ALLOWED_OWNERS"):
        env_cfg["allowed_owners"] = env.get("VELORA_ALLOWED_OWNERS")
    if env.get("VELORA_MAX_ATTEMPTS"):
        env_cfg["max_attempts"] = env.get("VELORA_MAX_ATTEMPTS")

    if env.get("VELORA_MODE_A_MAX_COST_USD"):
        env_cfg["mode_a_max_cost_usd"] = env.get("VELORA_MODE_A_MAX_COST_USD")
    if env.get("VELORA_MODE_A_NO_PROGRESS_MAX"):
        env_cfg["mode_a_no_progress_max"] = env.get("VELORA_MODE_A_NO_PROGRESS_MAX")
    if env.get("VELORA_MODE_A_MAX_WALL_SECONDS"):
        env_cfg["mode_a_max_wall_seconds"] = env.get("VELORA_MODE_A_MAX_WALL_SECONDS")

    if env.get("VELORA_RUNNER"):
        env_cfg["runner"] = env.get("VELORA_RUNNER")
    if env.get("VELORA_CODEX_SESSION_PREFIX"):
        env_cfg["codex_session_prefix"] = env.get("VELORA_CODEX_SESSION_PREFIX")
    if env.get("VELORA_CLAUDE_SESSION_PREFIX"):
        env_cfg["claude_session_prefix"] = env.get("VELORA_CLAUDE_SESSION_PREFIX")

    # Vault: allow both VELORA_VAULT_ADDR and VAULT_ADDR.
    if env.get("VELORA_VAULT_ADDR") or env.get("VAULT_ADDR"):
        env_cfg["vault_addr"] = env.get("VELORA_VAULT_ADDR") or env.get("VAULT_ADDR")
    if env.get("VELORA_VAULT_ROLE_ID_FILE"):
        env_cfg["vault_role_id_file"] = env.get("VELORA_VAULT_ROLE_ID_FILE")
    if env.get("VELORA_VAULT_SECRET_ID_FILE"):
        env_cfg["vault_secret_id_file"] = env.get("VELORA_VAULT_SECRET_ID_FILE")
    if env.get("VELORA_VAULT_API_KEYS_PATH"):
        env_cfg["vault_api_keys_path"] = env.get("VELORA_VAULT_API_KEYS_PATH")

    if env.get("VELORA_ACPX_CMD"):
        env_cfg["acpx_cmd"] = env.get("VELORA_ACPX_CMD")
    if env.get("VELORA_ACPX_FALLBACK"):
        env_cfg["acpx_fallback"] = env.get("VELORA_ACPX_FALLBACK")

    merged: dict[str, Any] = dict(defaults)
    merged.update(file_cfg)
    merged.update(env_cfg)

    allowed_owners = _parse_owners(merged.get("allowed_owners"))
    max_attempts = max(1, min(_parse_int(merged.get("max_attempts"), 3), 10))

    mode_a_max_cost_usd = max(1, min(_parse_int(merged.get("mode_a_max_cost_usd"), 20), 500))
    mode_a_no_progress_max = max(1, min(_parse_int(merged.get("mode_a_no_progress_max"), 4), 50))
    mode_a_max_wall_seconds = max(60, min(_parse_int(merged.get("mode_a_max_wall_seconds"), 30 * 60), 24 * 60 * 60))

    specialist_matrix = _parse_specialist_matrix(merged.get("specialist_matrix"), defaults["specialist_matrix"])

    runner = str(merged.get("runner") or defaults["runner"]).strip().lower()
    if runner not in {"codex", "claude"}:
        raise ValueError("runner must be one of: codex, claude")

    codex_prefix = str(merged.get("codex_session_prefix") or defaults["codex_session_prefix"])
    claude_prefix = str(merged.get("claude_session_prefix") or defaults["claude_session_prefix"])

    vault_addr = str(merged.get("vault_addr") or defaults["vault_addr"])
    vault_role_id_file = Path(str(merged.get("vault_role_id_file") or defaults["vault_role_id_file"]))
    vault_secret_id_file = Path(str(merged.get("vault_secret_id_file") or defaults["vault_secret_id_file"]))
    vault_api_keys_path = str(merged.get("vault_api_keys_path") or defaults["vault_api_keys_path"])

    acpx_cmd = merged.get("acpx_cmd")
    if acpx_cmd is not None:
        acpx_cmd = str(acpx_cmd).strip() or None

    acpx_fallback = merged.get("acpx_fallback")
    acpx_fallback_path: Path | None = None
    if acpx_fallback is not None and str(acpx_fallback).strip():
        acpx_fallback_path = Path(str(acpx_fallback)).expanduser()

    return VeloraConfig(
        allowed_owners=allowed_owners,
        max_attempts=max_attempts,

        mode_a_max_cost_usd=mode_a_max_cost_usd,
        mode_a_no_progress_max=mode_a_no_progress_max,
        mode_a_max_wall_seconds=mode_a_max_wall_seconds,

        specialist_matrix=specialist_matrix,

        runner=runner,
        codex_session_prefix=codex_prefix,
        claude_session_prefix=claude_prefix,

        vault_addr=vault_addr,
        vault_role_id_file=vault_role_id_file.expanduser(),
        vault_secret_id_file=vault_secret_id_file.expanduser(),
        vault_api_keys_path=vault_api_keys_path,
        acpx_cmd=acpx_cmd,
        acpx_fallback=acpx_fallback_path,
    )


@lru_cache(maxsize=1)
def get_config() -> VeloraConfig:
    return load_config()
