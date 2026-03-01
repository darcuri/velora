from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass
class CmdResult:
    returncode: int
    stdout: str
    stderr: str


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
    cmd = [
        "acpx",
        "codex",
        "--session",
        session_name,
        "--cwd",
        str(cwd),
        "--approve-all",
        "--prompt",
        prompt,
    ]
    return run_cmd(cmd)


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
    return run_cmd(["acpx", "gemini", "--prompt", prompt])


def get_vault_key(key: str) -> str:
    result = run_cmd(["acpx", "vault", "get", key])
    if result.returncode != 0:
        raise RuntimeError(f"Failed to read vault key '{key}': {result.stderr.strip()}")
    value = result.stdout.strip()
    if not value:
        raise RuntimeError(f"Vault key '{key}' is empty")
    return value

