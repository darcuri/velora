from __future__ import annotations

"""Runner dispatch for Velora coordinator/worker backends.

Phase 1 starts by introducing a thin abstraction layer around coordinator
execution so the orchestration loop no longer depends directly on ACPX-specific
helpers. ACP-backed Claude/Codex coordinator runs continue to work as before,
while direct Claude execution can now be exercised behind the same stable call
surface.
"""

import json
import os
from pathlib import Path
from typing import Any

from .acpx import CmdResult, _ensure_anthropic_auth, get_vault_key, run_claude, run_cmd, run_codex, run_local_llm
from .coordinator import (
    CoordinatorRunResult,
    render_coordinator_prompt_v1,
    run_coordinator_v1_with_cmd,
    validate_coordinator_cmd_result,
)
from .run_memory import coordinator_replay_paths


SUPPORTED_COORDINATOR_BACKENDS = {"acp-claude", "acp-codex", "direct-claude", "direct-local"}
SUPPORTED_WORKER_BACKENDS = {"acp-claude", "acp-codex", "direct-claude", "direct-codex", "direct-local"}


def normalize_coordinator_backend(*, backend: str | None = None, runner: str = "claude") -> str:
    """Resolve the coordinator backend key.

    If no explicit backend is provided, fall back to the legacy runner selection
    and map it onto an ACP-backed backend key. This keeps current behavior while
    giving the loop a stable backend abstraction for future direct runners.
    """

    if backend is not None:
        key = backend.strip().lower()
    else:
        runner_key = (runner or "claude").strip().lower() or "claude"
        if runner_key not in {"claude", "codex"}:
            raise ValueError("coordinator runner must be one of: claude, codex")
        key = f"acp-{runner_key}"

    if key not in SUPPORTED_COORDINATOR_BACKENDS:
        allowed = ", ".join(sorted(SUPPORTED_COORDINATOR_BACKENDS))
        raise ValueError(f"unsupported coordinator backend: {key} (expected one of: {allowed})")
    return key


def _load_replay_memory(cwd: Path, request: dict[str, Any]) -> str | None:
    run_id = str(request.get("run_id") or "")
    if not run_id:
        return None
    memory_path = coordinator_replay_paths(cwd, run_id)["memory"]
    if not memory_path.exists():
        return None
    return memory_path.read_text(encoding="utf-8")



def _load_replay_brief(cwd: Path, request: dict[str, Any]) -> dict[str, Any] | None:
    run_id = str(request.get("run_id") or "")
    if not run_id:
        return None
    brief_path = coordinator_replay_paths(cwd, run_id)["brief"]
    if not brief_path.exists():
        return None
    payload = json.loads(brief_path.read_text(encoding="utf-8"))
    return payload if isinstance(payload, dict) else None



def _run_direct_claude_coordinator(*, cwd: Path, request: dict[str, Any]) -> CoordinatorRunResult:
    replay_memory = _load_replay_memory(cwd, request)
    replay_brief = _load_replay_brief(cwd, request)
    prompt = render_coordinator_prompt_v1(request, replay_memory=replay_memory, brief=replay_brief)
    env = os.environ.copy()
    env.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    _ensure_anthropic_auth(env)
    result = run_cmd(
        [
            "claude",
            "--print",
            "--permission-mode",
            "bypassPermissions",
            "-p",
            prompt,
        ],
        cwd=cwd,
        env=env,
    )
    return validate_coordinator_cmd_result(result=result, request=request)


def run_coordinator(
    *,
    session_name: str,
    cwd: Path,
    request: dict[str, Any],
    runner: str = "claude",
    backend: str | None = None,
) -> CoordinatorRunResult:
    """Run the coordinator through the selected backend."""

    backend_key = normalize_coordinator_backend(backend=backend, runner=runner)

    if backend_key == "acp-claude":
        return run_coordinator_v1_with_cmd(
            session_name=session_name,
            cwd=cwd,
            request=request,
            runner="claude",
        )
    if backend_key == "acp-codex":
        return run_coordinator_v1_with_cmd(
            session_name=session_name,
            cwd=cwd,
            request=request,
            runner="codex",
        )
    if backend_key == "direct-claude":
        return _run_direct_claude_coordinator(cwd=cwd, request=request)
    if backend_key == "direct-local":
        return _run_direct_local_coordinator(cwd=cwd, request=request)

    raise AssertionError(f"unreachable coordinator backend: {backend_key}")


def _run_direct_local_coordinator(*, cwd: Path, request: dict[str, Any]) -> CoordinatorRunResult:
    replay_memory = _load_replay_memory(cwd, request)
    replay_brief = _load_replay_brief(cwd, request)
    prompt = render_coordinator_prompt_v1(request, replay_memory=replay_memory, brief=replay_brief)
    result = run_local_llm(prompt, cwd=cwd)
    return validate_coordinator_cmd_result(result=result, request=request)


def normalize_worker_backend(*, backend: str | None = None, runner: str = "codex") -> str:
    """Resolve the worker backend key.

    If no explicit backend is provided, preserve current behavior by mapping the
    selected specialist runner onto ACP-backed execution.

    Explicit backend overrides are allowed, but runner-specific backends must
    still agree with the coordinator-selected worker runner. This prevents
    silently routing a `runner=codex` work item through Claude (or vice versa).
    """

    runner_key = (runner or "codex").strip().lower() or "codex"
    if runner_key not in {"claude", "codex"}:
        raise ValueError("worker runner must be one of: claude, codex")

    if backend is not None:
        key = backend.strip().lower()
    else:
        key = f"acp-{runner_key}"

    if key not in SUPPORTED_WORKER_BACKENDS:
        allowed = ", ".join(sorted(SUPPORTED_WORKER_BACKENDS))
        raise ValueError(f"unsupported worker backend: {key} (expected one of: {allowed})")

    # direct-local is runner-agnostic — skip runner-matching check.
    if key == "direct-local":
        return key

    backend_runner = key.removeprefix("acp-").removeprefix("direct-")
    if backend_runner not in {"claude", "codex"}:
        raise ValueError(f"worker backend must target claude or codex: {key}")
    if backend_runner != runner_key:
        raise ValueError(
            f"worker backend '{key}' does not match selected runner '{runner_key}'"
        )
    return key


def _run_direct_claude_worker(*, cwd: Path, prompt: str) -> CmdResult:
    env = os.environ.copy()
    env.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    _ensure_anthropic_auth(env)
    return run_cmd(
        [
            "claude",
            "--print",
            "--permission-mode",
            "bypassPermissions",
            "-p",
            prompt,
        ],
        cwd=cwd,
        env=env,
    )


def _run_direct_codex_worker(*, cwd: Path, prompt: str) -> CmdResult:
    env = os.environ.copy()
    env.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    env["OPENAI_API_KEY"] = get_vault_key("OPENAI_API_KEY", env=env)
    return run_cmd(
        [
            "codex",
            "exec",
            "--full-auto",
            "-C",
            str(cwd),
            "-",
        ],
        cwd=cwd,
        input_text=prompt,
        env=env,
    )


def run_worker(
    *,
    session_name: str,
    cwd: Path,
    prompt: str,
    runner: str = "codex",
    backend: str | None = None,
) -> CmdResult:
    """Run the worker through the selected backend."""

    backend_key = normalize_worker_backend(backend=backend, runner=runner)

    if backend_key == "acp-codex":
        return run_codex(session_name=session_name, cwd=cwd, prompt=prompt)
    if backend_key == "acp-claude":
        return run_claude(session_name=session_name, cwd=cwd, prompt=prompt)
    if backend_key == "direct-claude":
        return _run_direct_claude_worker(cwd=cwd, prompt=prompt)
    if backend_key == "direct-codex":
        return _run_direct_codex_worker(cwd=cwd, prompt=prompt)
    if backend_key == "direct-local":
        return run_local_llm(prompt, cwd=cwd)

    raise AssertionError(f"unreachable worker backend: {backend_key}")
