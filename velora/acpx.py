from __future__ import annotations

import json
import os
import subprocess
from functools import lru_cache
from dataclasses import dataclass
from pathlib import Path
from shutil import which
from typing import Any
import urllib.error
import urllib.request


@dataclass
class CmdResult:
    returncode: int
    stdout: str
    stderr: str


FALLBACK_ACPX = Path("/home/merlin/openclaw/extensions/acpx/node_modules/.bin/acpx")
VAULT_ADDR = "https://entropy-internal.duckdns.org:8200"


def _fallback_acpx_exists() -> bool:
    return FALLBACK_ACPX.exists()


def resolve_acpx_cmd(env: dict[str, str] | None = None) -> str:
    env_map = env if env is not None else os.environ
    env_cmd = env_map.get("VELORA_ACPX_CMD", "").strip()
    if env_cmd:
        return env_cmd

    resolved = which("acpx")
    if resolved:
        return resolved

    if _fallback_acpx_exists():
        return str(FALLBACK_ACPX)

    raise RuntimeError(
        "acpx command not found. Set VELORA_ACPX_CMD or install acpx in PATH "
        f"or at {FALLBACK_ACPX}."
    )


def run_cmd(cmd: list[str], cwd: Path | None = None, input_text: str | None = None) -> CmdResult:
    proc = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        input=input_text,
        text=True,
        capture_output=True,
        check=False,
    )
    return CmdResult(returncode=proc.returncode, stdout=proc.stdout, stderr=proc.stderr)


def run_codex(session_name: str, cwd: Path, prompt: str) -> CmdResult:
    acpx_cmd = resolve_acpx_cmd()
    cmd = [
        acpx_cmd,
        "--cwd",
        str(cwd),
        "--approve-all",
        "--format",
        "quiet",
        "codex",
        "prompt",
        "-s",
        session_name,
        "-f",
        "-",
    ]
    return run_cmd(cmd, input_text=prompt)


def parse_codex_footer(output: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for line in output.splitlines():
        if line.startswith("BRANCH:"):
            parsed["branch"] = line.split(":", 1)[1].strip()
        elif line.startswith("HEAD_SHA:"):
            parsed["head_sha"] = line.split(":", 1)[1].strip()
        elif line.startswith("SUMMARY:"):
            parsed["summary"] = line.split(":", 1)[1].strip()
    required = ("branch", "head_sha", "summary")
    missing = [k for k in required if not parsed.get(k)]
    if missing:
        raise RuntimeError(f"Codex output missing footer fields: {', '.join(missing)}")
    return parsed


def run_gemini_review(diff_text: str) -> CmdResult:
    prompt = (
        "Review the code diff. Output at most 5 bullet points, each labeled BLOCKER or NIT. "
        "Focus on correctness and regressions. Keep it concise.\n\n"
        f"{diff_text}"
    )
    acpx_cmd = resolve_acpx_cmd()
    return run_cmd([acpx_cmd, "--format", "quiet", "gemini", "exec", "-f", "-"], input_text=prompt)


def _read_file(path: Path) -> str:
    if not path.exists():
        raise RuntimeError(f"Missing required Vault credential file: {path}")
    value = path.read_text(encoding="utf-8").strip()
    if not value:
        raise RuntimeError(f"Vault credential file is empty: {path}")
    return value


def _vault_request(method: str, path: str, body: dict[str, Any] | None = None, token: str | None = None) -> dict[str, Any]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    url = f"{VAULT_ADDR}{path}"
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Vault-Token"] = token
    req = urllib.request.Request(url=url, method=method, headers=headers, data=data)
    try:
        with urllib.request.urlopen(req) as resp:
            payload = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Vault request failed for {path}: {exc.code} {detail}") from exc
    return json.loads(payload) if payload else {}


@lru_cache(maxsize=1)
def _load_vault_api_keys() -> dict[str, str]:
    vault_dir = Path.home() / ".openclaw"
    role_id = _read_file(vault_dir / ".vault-role-id")
    secret_id = _read_file(vault_dir / ".vault-secret-id")

    login = _vault_request(
        "POST",
        "/v1/auth/approle/login",
        body={"role_id": role_id, "secret_id": secret_id},
    )
    token = login.get("auth", {}).get("client_token")
    if not token:
        raise RuntimeError("Vault login succeeded but did not return client token")

    secret = _vault_request("GET", "/v1/secret/data/openclaw/api-keys", token=token)
    data = secret.get("data", {}).get("data", {})
    if not isinstance(data, dict):
        raise RuntimeError("Vault secret payload missing expected data object")
    return {str(k): str(v) for k, v in data.items()}


def get_vault_key(key: str, env: dict[str, str] | None = None) -> str:
    env_map = env if env is not None else os.environ
    env_val = env_map.get(key, "").strip()
    if env_val:
        return env_val

    keys = _load_vault_api_keys()
    value = keys.get(key, "").strip()
    if not value:
        raise RuntimeError(f"Vault key '{key}' not found")
    return value
