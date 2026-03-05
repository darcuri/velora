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

from .config import get_config


@dataclass(frozen=True)
class AcpUsage:
    """Best-effort usage metadata extracted from acpx JSON output.

    Notes:
    - `used`/`size` come from acp `usage_update` events. In practice this is
      *context usage*, not guaranteed-billed token counts.
    - `model_id` is best-effort from session/new or session/load results.
    """

    used: int | None = None
    size: int | None = None
    model_id: str | None = None


@dataclass
class CmdResult:
    returncode: int
    stdout: str
    stderr: str
    usage: AcpUsage | None = None


# Optional fallback for developers running Velora inside an OpenClaw checkout.
# Prefer PATH (acpx), VELORA_ACPX_CMD, or config.json.
DEFAULT_FALLBACK_ACPX = Path("./extensions/acpx/node_modules/.bin/acpx")

# Generic Vault default. Prefer VELORA_VAULT_ADDR/VAULT_ADDR or config.json; fall back to local dev.
DEFAULT_VAULT_ADDR = "http://127.0.0.1:8200"


def _fallback_acpx_path(env: dict[str, str] | None = None) -> Path:
    env_map = env if env is not None else os.environ
    raw = env_map.get("VELORA_ACPX_FALLBACK", "").strip()
    if raw:
        return Path(raw).expanduser()

    cfg = get_config()
    if cfg.acpx_fallback is not None:
        return cfg.acpx_fallback

    return DEFAULT_FALLBACK_ACPX


def _vault_addr(env: dict[str, str] | None = None) -> str:
    env_map = env if env is not None else os.environ
    raw = env_map.get("VELORA_VAULT_ADDR", "").strip() or env_map.get("VAULT_ADDR", "").strip()
    if raw:
        return raw

    cfg = get_config()
    if cfg.vault_addr:
        return cfg.vault_addr

    return DEFAULT_VAULT_ADDR


def _fallback_acpx_exists(env: dict[str, str] | None = None) -> bool:
    return _fallback_acpx_path(env=env).exists()


def resolve_acpx_cmd(env: dict[str, str] | None = None) -> str:
    env_map = env if env is not None else os.environ
    env_cmd = env_map.get("VELORA_ACPX_CMD", "").strip()
    if env_cmd:
        return env_cmd

    cfg = get_config()
    if cfg.acpx_cmd:
        return cfg.acpx_cmd

    resolved = which("acpx")
    if resolved:
        return resolved

    fallback = _fallback_acpx_path(env=env_map)
    if _fallback_acpx_exists(env=env_map):
        return str(fallback)

    raise RuntimeError(
        "acpx command not found. Set VELORA_ACPX_CMD or install acpx in PATH "
        f"or set VELORA_ACPX_FALLBACK to point at a fallback binary (tried {fallback})."
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


def _parse_acpx_json_prompt_output(raw_stdout: str) -> tuple[str, AcpUsage]:
    """Parse acpx --format json --json-strict output into plain text + usage.

    acpx emits JSON-RPC messages (one JSON object per line).

    We extract:
    - output text from session/update.agent_message_chunk
    - latest usage_update (used/size)
    - best-effort model_id from any result.models.currentModelId
    """

    out_chunks: list[str] = []
    used: int | None = None
    size: int | None = None
    model_id: str | None = None

    for raw_line in (raw_stdout or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue

        res = obj.get("result")
        if isinstance(res, dict):
            models = res.get("models")
            if isinstance(models, dict):
                mid = models.get("currentModelId")
                if isinstance(mid, str) and mid.strip():
                    model_id = mid.strip()

        if obj.get("method") != "session/update":
            continue

        params = obj.get("params")
        if not isinstance(params, dict):
            continue
        update = params.get("update")
        if not isinstance(update, dict):
            continue

        kind = update.get("sessionUpdate")
        if kind == "agent_message_chunk":
            content = update.get("content")
            if isinstance(content, dict) and content.get("type") == "text":
                out_chunks.append(str(content.get("text") or ""))
        elif kind == "usage_update":
            u = update.get("used")
            s = update.get("size")
            if isinstance(u, int):
                used = u
            if isinstance(s, int):
                size = s

    return "".join(out_chunks), AcpUsage(used=used, size=size, model_id=model_id)


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
    # Keep repo checkouts clean: many tools create __pycache__ by default.
    env.setdefault("PYTHONDONTWRITEBYTECODE", "1")
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
        "json",
        "--json-strict",
        "codex",
        "prompt",
        "-s",
        session_name,
        "-f",
        "-",
    ]
    res = run_cmd(cmd, input_text=prompt, env=env)
    if res.returncode != 0:
        return res

    try:
        text, usage = _parse_acpx_json_prompt_output(res.stdout)
        return CmdResult(returncode=0, stdout=text, stderr=res.stderr, usage=usage)
    except Exception as exc:  # noqa: BLE001
        # Fallback: preserve raw output for debugging.
        return CmdResult(returncode=0, stdout=res.stdout, stderr=f"failed to parse acpx json output: {exc}")


def ensure_claude_session(session_name: str, cwd: Path, env: dict[str, str]) -> CmdResult:
    acpx_cmd = resolve_acpx_cmd(env=env)
    cmd = [
        acpx_cmd,
        "--cwd",
        str(cwd),
        "claude",
        "sessions",
        "ensure",
        "--name",
        session_name,
    ]
    return run_cmd(cmd, env=env)


def _ensure_anthropic_auth(env: dict[str, str]) -> None:
    """Ensure Claude has credentials in env.

    Support both:
    - ANTHROPIC_AUTH_TOKEN (OAuth / Claude Code style)
    - ANTHROPIC_API_KEY (API key)

    Prefer env. If missing, use Vault/AppRole fallback only if configured.
    """

    token = env.get("ANTHROPIC_AUTH_TOKEN", "").strip()
    api_key = env.get("ANTHROPIC_API_KEY", "").strip()
    if token or api_key:
        return

    configured, detail = _vault_fallback_configured(env)
    if not configured:
        raise RuntimeError(
            "Neither ANTHROPIC_AUTH_TOKEN nor ANTHROPIC_API_KEY is set and Vault fallback is not configured "
            f"({detail}). Set one of those env vars to run Claude."
        )

    keys = _load_vault_api_keys()
    token = str(keys.get("ANTHROPIC_AUTH_TOKEN", "")).strip()
    api_key = str(keys.get("ANTHROPIC_API_KEY", "")).strip()

    if token:
        env["ANTHROPIC_AUTH_TOKEN"] = token
        return
    if api_key:
        env["ANTHROPIC_API_KEY"] = api_key
        return

    raise RuntimeError(
        "Vault did not return ANTHROPIC_AUTH_TOKEN or ANTHROPIC_API_KEY. Set one of those env vars to run Claude."
    )


def run_claude(session_name: str, cwd: Path, prompt: str) -> CmdResult:
    env = os.environ.copy()
    # Keep repo checkouts clean: many tools create __pycache__ by default.
    env.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    _ensure_anthropic_auth(env)

    ensure = ensure_claude_session(session_name=session_name, cwd=cwd, env=env)
    if ensure.returncode != 0:
        return ensure

    acpx_cmd = resolve_acpx_cmd(env=env)
    cmd = [
        acpx_cmd,
        "--cwd",
        str(cwd),
        "--approve-all",
        "--format",
        "json",
        "--json-strict",
        "claude",
        "prompt",
        "-s",
        session_name,
        "-f",
        "-",
    ]
    res = run_cmd(cmd, input_text=prompt, env=env)
    if res.returncode != 0:
        return res

    try:
        text, usage = _parse_acpx_json_prompt_output(res.stdout)
        return CmdResult(returncode=0, stdout=text, stderr=res.stderr, usage=usage)
    except Exception as exc:  # noqa: BLE001
        return CmdResult(returncode=0, stdout=res.stdout, stderr=f"failed to parse acpx json output: {exc}")


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


def _gemini_generate_content(
    *,
    api_key: str,
    model: str,
    prompt: str,
    max_output_tokens: int = 1024,
    timeout_s: int = 60,
) -> str:
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
            "maxOutputTokens": max_output_tokens,
        },
    }

    req = urllib.request.Request(
        url=url,
        method="POST",
        headers={"Content-Type": "application/json"},
        data=json.dumps(body).encode("utf-8"),
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:  # nosec B310 (controlled URL)
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


def _strip_bullet_prefix(line: str) -> str:
    s = line.strip()
    # Common markdown bullet prefixes.
    while s and s[0] in "-*•":
        s = s[1:].lstrip()
    return s


def _normalize_review_lines(text: str) -> list[str]:
    lines: list[str] = []
    for raw in text.splitlines():
        s = _strip_bullet_prefix(raw).strip()
        if s:
            lines.append(s)
    return lines


def review_has_blocker(review_text: str) -> bool:
    for line in _normalize_review_lines(review_text):
        if line.startswith("BLOCKER:"):
            return True
    return False


def _review_text_valid(review_text: str) -> bool:
    lines = _normalize_review_lines(review_text)
    if not lines:
        return False

    # OK is allowed only as a single-line "all clear".
    if len(lines) == 1 and lines[0].startswith("OK:"):
        return lines[0].endswith(".")

    if len(lines) > 5:
        return False

    for line in lines:
        if not (line.startswith("BLOCKER:") or line.startswith("NIT:")):
            return False
        if not line.endswith("."):
            return False

    return True


GEMINI_REVIEW_PROMPT_PREFIX = (
    "Review the code diff for correctness/regressions. Output either: \n"
    "- exactly one line starting with 'OK:' if you find no issues, OR\n"
    "- 1–5 bullet lines, each starting with 'BLOCKER:' or 'NIT:'.\n"
    "Use 'BLOCKER:' ONLY when the diff itself proves a serious issue (crash, test failure, incorrect behavior, data loss, or security flaw). "
    "If you're not sure, use 'NIT:' instead.\n"
    "Do not speculate beyond the diff.\n"
    "Every line must be a complete sentence ending with a period.\n\n"
)


def run_gemini_review(diff_text: str) -> CmdResult:
    # Keep the prompt strict: short, complete, and actionable.
    env = os.environ.copy()
    api_key = get_vault_key("GEMINI_API_KEY", env=env)

    primary_model = env.get("VELORA_GEMINI_MODEL", "gemini-3-flash-preview")
    fallback_model = env.get("VELORA_GEMINI_FALLBACK_MODEL", "gemini-3.1-pro-preview")
    fallback_model_2 = env.get("VELORA_GEMINI_FALLBACK_MODEL_2", "gemini-pro-latest")
    models = [m for m in [primary_model, fallback_model, fallback_model_2] if m]

    max_output_tokens = int(env.get("VELORA_GEMINI_MAX_OUTPUT_TOKENS", "1024"))

    max_diff_chars = int(env.get("VELORA_GEMINI_MAX_DIFF_CHARS", "120000"))
    diff_trimmed = diff_text
    if len(diff_trimmed) > max_diff_chars:
        diff_trimmed = diff_trimmed[:max_diff_chars] + "\n\n[diff truncated]\n"

    last_err = ""
    malformed_text = ""
    malformed_model = ""
    for model in models:
        try:
            text = _gemini_generate_content(
                api_key=api_key,
                model=model,
                prompt=GEMINI_REVIEW_PROMPT_PREFIX + diff_trimmed,
                max_output_tokens=max_output_tokens,
            ).strip()

            if not _review_text_valid(text):
                last_err = f"Gemini review did not match required format using model {model}: {text[:200]!r}"
                if text:
                    malformed_text = text
                    malformed_model = model
                continue

            return CmdResult(returncode=0, stdout=text + "\n", stderr="")
        except Exception as exc:  # noqa: BLE001
            last_err = f"Gemini review failed using model {model}: {exc}"
            continue

    if malformed_text:
        return CmdResult(
            returncode=0,
            stdout=malformed_text + "\n",
            stderr=f"Gemini review malformed format using model {malformed_model}",
        )

    return CmdResult(returncode=1, stdout="", stderr=last_err or "Gemini review failed")


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
    url = f"{_vault_addr()}{path}"
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Vault-Token"] = token
    req = urllib.request.Request(url=url, method=method, headers=headers, data=data)
    try:
        with urllib.request.urlopen(req) as resp:  # nosec B310 (Vault addr is user-configured)
            payload = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Vault request failed for {path}: {exc.code} {detail}") from exc
    return json.loads(payload) if payload else {}


@lru_cache(maxsize=1)
def _load_vault_api_keys() -> dict[str, str]:
    env = os.environ
    cfg = get_config()

    default_role_id = cfg.vault_role_id_file
    default_secret_id = cfg.vault_secret_id_file

    role_id_path = Path(env.get("VELORA_VAULT_ROLE_ID_FILE", str(default_role_id))).expanduser()
    secret_id_path = Path(env.get("VELORA_VAULT_SECRET_ID_FILE", str(default_secret_id))).expanduser()

    role_id = _read_file(role_id_path)
    secret_id = _read_file(secret_id_path)

    login = _vault_request(
        "POST",
        "/v1/auth/approle/login",
        body={"role_id": role_id, "secret_id": secret_id},
    )
    token = login.get("auth", {}).get("client_token")
    if not token:
        raise RuntimeError("Vault login succeeded but did not return client token")

    secret_path = env.get("VELORA_VAULT_API_KEYS_PATH", cfg.vault_api_keys_path)
    secret = _vault_request("GET", secret_path, token=token)
    data = secret.get("data", {}).get("data", {})
    if not isinstance(data, dict):
        raise RuntimeError("Vault secret payload missing expected data object")
    return {str(k): str(v) for k, v in data.items()}


def _vault_fallback_configured(env_map: dict[str, str]) -> tuple[bool, str]:
    cfg = get_config()
    role_id_path = Path(env_map.get("VELORA_VAULT_ROLE_ID_FILE", str(cfg.vault_role_id_file))).expanduser()
    secret_id_path = Path(env_map.get("VELORA_VAULT_SECRET_ID_FILE", str(cfg.vault_secret_id_file))).expanduser()

    missing: list[str] = []
    if not role_id_path.exists():
        missing.append(str(role_id_path))
    if not secret_id_path.exists():
        missing.append(str(secret_id_path))

    if missing:
        return False, "missing AppRole credential file(s): " + ", ".join(missing)
    return True, ""


def get_vault_key(key: str, env: dict[str, str] | None = None) -> str:
    """Get a secret value.

    Order:
    1) Environment variable named `key` (e.g. OPENAI_API_KEY)
    2) Vault/AppRole fallback (only if configured)

    Rationale: Vault is optional; env vars must always work without any Vault/OpenBao setup.
    """

    env_map = env if env is not None else os.environ
    env_val = env_map.get(key, "").strip()
    if env_val:
        return env_val

    configured, detail = _vault_fallback_configured(env_map)
    if not configured:
        raise RuntimeError(
            f"{key} is not set and Vault fallback is not configured ({detail}). "
            f"Set {key} in the environment, or configure Vault via VELORA_VAULT_ADDR/VAULT_ADDR and "
            "VELORA_VAULT_ROLE_ID_FILE/VELORA_VAULT_SECRET_ID_FILE."
        )

    try:
        keys = _load_vault_api_keys()
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            f"{key} is not set and Vault fallback failed: {exc}. "
            f"Set {key} in the environment to bypass Vault."
        ) from exc

    value = keys.get(key, "").strip()
    if not value:
        raise RuntimeError(f"Vault did not return a value for '{key}'. Set {key} in the environment.")
    return value
