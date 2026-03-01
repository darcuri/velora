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
    codex_session_prefix: str

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


def load_config() -> VeloraConfig:
    # Defaults.
    defaults = {
        "allowed_owners": ["darcuri"],
        "max_attempts": 3,
        "codex_session_prefix": "velora-codex-",
        "vault_addr": "https://entropy-internal.duckdns.org:8200",
        "vault_role_id_file": str(Path.home() / ".openclaw" / ".vault-role-id"),
        "vault_secret_id_file": str(Path.home() / ".openclaw" / ".vault-secret-id"),
        "vault_api_keys_path": "/v1/secret/data/openclaw/api-keys",
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
    if env.get("VELORA_CODEX_SESSION_PREFIX"):
        env_cfg["codex_session_prefix"] = env.get("VELORA_CODEX_SESSION_PREFIX")

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

    allowed_owners = _parse_owners(merged.get("allowed_owners")) or {"darcuri"}
    max_attempts = max(1, min(_parse_int(merged.get("max_attempts"), 3), 10))
    prefix = str(merged.get("codex_session_prefix") or "velora-codex-")

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
        codex_session_prefix=prefix,
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
