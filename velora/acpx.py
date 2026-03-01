from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from functools import lru_cache
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


def run_cmd(
    cmd: list[str],
    cwd: Path | None = None,
    input_text: str | None = None,
    env: dict[str, str] | None = None,
) -> CmdResult:
    proc = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        input=input_text,
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )
    return CmdResult(returncode=proc.returncode, stdout=proc.stdout, stderr=proc.stderr)


def ensure_codex_session(session_name: str, cwd: Path, env: dict[str, str]) -> CmdResult:
    acpx_cmd = resolve_acpx_cmd(env=env)
    cmd = [
        acpx_cmd,
        "--cwd",
        str(cwd),
        "codex",
        "sessions",
        "ensure",
        "--name",
        session_name,
    ]
    return run_cmd(cmd, env=env)


def run_codex(session_name: str, cwd: Path, prompt: str) -> CmdResult:
    # acpx codex requires OPENAI_API_KEY; pull from env or Vault.
    env = os.environ.copy()
    env["OPENAI_API_KEY"] = get_vault_key("OPENAI_API_KEY", env=env)

    ensure = ensure_codex_session(session_name=session_name, cwd=cwd, env=env)
    if ensure.returncode != 0:
        return ensure

    acpx_cmd = resolve_acpx_cmd(env=env)
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
    return run_cmd(cmd, input_text=prompt, env=env)


def parse_codex_footer(output: str) -> dict[str, str]:
    """Parse the Codex machine footer.

    Codex generally prints footer fields on their own lines, but in practice it may
    accidentally glue them to the end of a previous sentence. Be tolerant: search
    the full output for the markers.
    """
    import re

    def _find(pattern: str) -> str | None:
        m = re.search(pattern, output, flags=re.MULTILINE)
        return m.group(1).strip() if m else None

    branch = _find(r"BRANCH:\s*(\S+)")
    head_sha = _find(r"HEAD_SHA:\s*([0-9a-fA-F]{6,40})")
    summary = _find(r"SUMMARY:\s*(.+)")

    parsed: dict[str, str] = {
        "branch": branch or "",
        "head_sha": head_sha or "",
        "summary": summary or "",
    }

    missing = [k for k, v in parsed.items() if not v]
    if missing:
        raise RuntimeError(f"Codex output missing footer fields: {', '.join(missing)}")
    return parsed


def _gemini_generate_content(*, api_key: str, model: str, prompt: str, timeout_s: int = 60) -> str:
    """Call the Gemini REST API directly (stdlib-only).

    This avoids relying on a local `gemini` binary, which may not be installed.
    """

    model_name = model[len("models/") :] if model.startswith("models/") else model
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{model_name}:generateContent?key={api_key}"
    )
    body = {
        "contents": [
            {
                "role": "user",
                "parts": [{"text": prompt}],
            }
        ],
        "generationConfig": {
            "temperature": 0.2,
            "maxOutputTokens": 512,
        },
    }

    req = urllib.request.Request(
        url=url,
        method="POST",
        headers={"Content-Type": "application/json"},
        data=json.dumps(body).encode("utf-8"),
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        # Do NOT include the URL (it contains the API key).
        msg = detail
        try:
            parsed = json.loads(detail)
            msg = str(parsed.get("error", {}).get("message") or detail)
        except Exception:  # noqa: BLE001
            pass
        raise RuntimeError(f"Gemini API request failed: HTTP {exc.code}: {msg}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Gemini API request failed: {exc.reason}") from exc

    payload = json.loads(raw) if raw else {}
    try:
        parts = payload["candidates"][0]["content"]["parts"]
        if not isinstance(parts, list) or not parts:
            raise KeyError("parts")
        texts: list[str] = []
        for part in parts:
            if isinstance(part, dict) and "text" in part:
                texts.append(str(part["text"]))
        if not texts:
            raise KeyError("text")
        return "".join(texts)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"Gemini API response missing expected text field: {payload}") from exc


def run_gemini_review(diff_text: str) -> CmdResult:
    prompt_prefix = (
        "Review the code diff. Output at most 5 bullet points, each labeled BLOCKER or NIT. "
        "Focus on correctness and regressions. Keep it concise.\n\n"
    )

    env = os.environ.copy()
    api_key = get_vault_key("GEMINI_API_KEY", env=env)

    model = env.get("VELORA_GEMINI_MODEL", "gemini-3-flash-preview")
    max_diff_chars = int(env.get("VELORA_GEMINI_MAX_DIFF_CHARS", "120000"))
    diff_trimmed = diff_text
    if len(diff_trimmed) > max_diff_chars:
        diff_trimmed = diff_trimmed[:max_diff_chars] + "\n\n[diff truncated]\n"

    try:
        text = _gemini_generate_content(api_key=api_key, model=model, prompt=prompt_prefix + diff_trimmed)
        return CmdResult(returncode=0, stdout=text + "\n", stderr="")
    except Exception as exc:  # noqa: BLE001
        return CmdResult(returncode=1, stdout="", stderr=str(exc))


def _read_file(path: Path) -> str:
    if not path.exists():
        raise RuntimeError(f"Missing required Vault credential file: {path}")
    value = path.read_text(encoding="utf-8").strip()
    if not value:
        raise RuntimeError(f"Vault credential file is empty: {path}")
    return value


def _vault_request(
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
    token: str | None = None,
) -> dict[str, Any]:
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
