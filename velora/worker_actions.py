from __future__ import annotations

import enum
import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class ScopeViolation(ValueError):
    """Raised when an action attempts to access a path outside allowed scope."""
    pass


class ActionError(ValueError):
    """Raised when an action has invalid params or execution fails."""
    pass


@dataclass(frozen=True)
class WorkerScope:
    repo_root: Path
    allowed_files: set[str]   # repo-relative paths
    allowed_dirs: set[str]    # parent dirs of allowed_files
    test_commands: list[str]  # joined allowlist strings
    work_branch: str
    unrestricted_read: bool = False  # investigate items can read any file


def resolve_scoped_path(
    scope: WorkerScope,
    path_str: str,
    *,
    require_allowed_file: bool = False,
) -> Path:
    """Resolve a repo-relative path with scope enforcement.

    Args:
        scope: The active worker scope.
        path_str: Repo-relative path string from the LLM.
        require_allowed_file: If True, path must be exactly in allowed_files.
            If False, path must be in allowed_files or within allowed_dirs.

    Returns:
        Absolute resolved path.

    Raises:
        ScopeViolation: If the path is out of scope.
    """
    stripped = path_str.strip()
    if not stripped:
        raise ScopeViolation("empty path")
    if stripped.startswith("/"):
        raise ScopeViolation(f"absolute paths not allowed: {stripped}")
    if ".." in Path(stripped).parts:
        raise ScopeViolation(f"path traversal not allowed: {stripped}")

    resolved = (scope.repo_root / stripped).resolve()

    # Symlink check: resolved path must be under repo_root
    try:
        resolved.relative_to(scope.repo_root.resolve())
    except ValueError:
        raise ScopeViolation(f"path resolves outside repo: {stripped}")

    # Normalize to repo-relative for scope checks
    repo_relative = str(resolved.relative_to(scope.repo_root.resolve()))

    if require_allowed_file:
        if repo_relative not in scope.allowed_files:
            raise ScopeViolation(
                f"path not in allowed_files: {repo_relative}"
            )
    elif not scope.unrestricted_read:
        # Must be in allowed_files OR within an allowed_dir
        if repo_relative not in scope.allowed_files:
            parts = Path(repo_relative).parts
            in_allowed_dir = False
            for i in range(len(parts)):
                prefix = str(Path(*parts[: i + 1])) if i > 0 else parts[0]
                if prefix in scope.allowed_dirs:
                    in_allowed_dir = True
                    break
            if not in_allowed_dir:
                raise ScopeViolation(
                    f"path not in scope: {repo_relative}"
                )

    return resolved


def _action_result(status: str, result: str) -> dict[str, str]:
    return {"status": status, "result": result}


def execute_read_file(scope: WorkerScope, params: dict[str, Any]) -> dict[str, str]:
    path_str = params.get("path", "")
    try:
        resolved = resolve_scoped_path(scope, path_str, require_allowed_file=False)
    except ScopeViolation as e:
        return _action_result("error", f"Scope violation: {e}")
    if not resolved.is_file():
        return _action_result("error", f"File not found: {path_str}")
    try:
        content = resolved.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return _action_result("error", f"Read error: {e}")
    return _action_result("ok", content)


def execute_list_files(scope: WorkerScope, params: dict[str, Any]) -> dict[str, str]:
    path_str = params.get("path", "")
    try:
        resolved = resolve_scoped_path(scope, path_str, require_allowed_file=False)
    except ScopeViolation as e:
        return _action_result("error", f"Scope violation: {e}")
    if not resolved.is_dir():
        return _action_result("error", f"Directory not found: {path_str}")
    try:
        entries = sorted(p.name for p in resolved.iterdir() if not p.name.startswith("."))
    except OSError as e:
        return _action_result("error", f"List error: {e}")
    return _action_result("ok", "\n".join(entries))


def execute_write_file(scope: WorkerScope, params: dict[str, Any]) -> dict[str, str]:
    path_str = params.get("path", "")
    content = params.get("content", "")
    if not isinstance(content, str):
        return _action_result("error", "content must be a string")
    try:
        resolved = resolve_scoped_path(scope, path_str, require_allowed_file=True)
    except ScopeViolation as e:
        return _action_result("error", f"Scope violation: {e}")
    try:
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_text(content, encoding="utf-8")
    except OSError as e:
        return _action_result("error", f"Write error: {e}")
    return _action_result("ok", f"Wrote {len(content)} bytes to {path_str}")


def execute_patch_file(scope: WorkerScope, params: dict[str, Any]) -> dict[str, str]:
    path_str = params.get("path", "")
    old = params.get("old", "")
    new = params.get("new", "")
    if not isinstance(old, str) or not isinstance(new, str):
        return _action_result("error", "old and new must be strings")
    if not old:
        return _action_result("error", "old string must not be empty")
    try:
        resolved = resolve_scoped_path(scope, path_str, require_allowed_file=True)
    except ScopeViolation as e:
        return _action_result("error", f"Scope violation: {e}")
    if not resolved.is_file():
        return _action_result("error", f"File not found: {path_str}")
    try:
        content = resolved.read_text(encoding="utf-8")
    except OSError as e:
        return _action_result("error", f"Read error: {e}")
    count = content.count(old)
    if count == 0:
        return _action_result("error", f"Old string not found in {path_str}")
    if count > 1:
        return _action_result("error", f"Old string not unique in {path_str} ({count} matches)")
    patched = content.replace(old, new, 1)
    try:
        resolved.write_text(patched, encoding="utf-8")
    except OSError as e:
        return _action_result("error", f"Write error: {e}")
    return _action_result("ok", f"Patched {path_str}")


_SEARCH_RESULT_CAP = 50
_RUN_TESTS_TIMEOUT_S = int(os.environ.get("VELORA_HARNESS_TEST_TIMEOUT", "120"))


def execute_search_files(scope: WorkerScope, params: dict[str, Any]) -> dict[str, str]:
    pattern_str = params.get("pattern", "")
    if not pattern_str:
        return _action_result("error", "pattern must be non-empty")
    try:
        compiled = re.compile(pattern_str)
    except re.error as e:
        return _action_result("error", f"Invalid regex: {e}")

    matches: list[str] = []
    search_dirs = sorted(scope.allowed_dirs) if not scope.unrestricted_read else ["."]
    for dir_name in search_dirs:
        dir_path = scope.repo_root / dir_name
        if not dir_path.is_dir():
            continue
        for file_path in sorted(dir_path.rglob("*")):
            if not file_path.is_file():
                continue
            if file_path.name.startswith("."):
                continue
            try:
                text = file_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            rel = str(file_path.relative_to(scope.repo_root))
            for line_no, line in enumerate(text.splitlines(), 1):
                if compiled.search(line):
                    matches.append(f"{rel}:{line_no}: {line.rstrip()}")
                    if len(matches) >= _SEARCH_RESULT_CAP:
                        matches.append(f"[capped at {_SEARCH_RESULT_CAP} results]")
                        return _action_result("ok", "\n".join(matches))

    if not matches:
        return _action_result("ok", "No matches found.")
    return _action_result("ok", "\n".join(matches))


def execute_run_tests(scope: WorkerScope, params: dict[str, Any]) -> dict[str, str]:
    command_str = params.get("command", "").strip()
    if not command_str:
        return _action_result("error", "command must be non-empty")
    if command_str not in scope.test_commands:
        return _action_result(
            "error",
            f"Command not in allowlist: {command_str}. "
            f"Allowed: {', '.join(scope.test_commands)}",
        )
    cmd_parts = command_str.split()
    try:
        proc = subprocess.run(
            cmd_parts,
            cwd=str(scope.repo_root),
            text=True,
            capture_output=True,
            check=False,
            timeout=_RUN_TESTS_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return _action_result("error", f"Test command timed out after {_RUN_TESTS_TIMEOUT_S}s")
    except OSError as e:
        return _action_result("error", f"Failed to execute test command: {e}")

    output = (proc.stdout or "") + (proc.stderr or "")
    status = "ok" if proc.returncode == 0 else "error"
    return _action_result(status, output)


_PROBE_TIMEOUT_S = 10


def execute_run_probe(scope: WorkerScope, params: dict[str, Any]) -> dict[str, str]:
    """Run a probe command during investigation. No allowlist — the point is discovery.

    Only available when scope.unrestricted_read is True (investigate items).
    No shell expansion. Short timeout. Returns output regardless of exit code.
    """
    if not scope.unrestricted_read:
        return _action_result("error", "run_probe is only available during investigate")
    command_str = params.get("command", "").strip()
    if not command_str:
        return _action_result("error", "command must be non-empty")
    cmd_parts = command_str.split()
    try:
        proc = subprocess.run(
            cmd_parts,
            cwd=str(scope.repo_root),
            text=True,
            capture_output=True,
            check=False,
            timeout=_PROBE_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return _action_result("error", f"Probe timed out after {_PROBE_TIMEOUT_S}s")
    except OSError as e:
        return _action_result("error", f"Probe failed: {e}")

    output = (proc.stdout or "") + (proc.stderr or "")
    rc = proc.returncode
    return _action_result("ok" if rc == 0 else "not_found", f"exit_code={rc}\n{output}")


# Terminal actions return None — the loop handles them specially.
def _terminal_noop(scope: WorkerScope, params: dict[str, Any]) -> dict[str, str]:
    """Placeholder executor for terminal actions (work_complete, work_blocked).
    The loop handles these before dispatch reaches here."""
    return _action_result("ok", "")


KNOWN_ACTIONS: dict[str, Any] = {
    "read_file": execute_read_file,
    "list_files": execute_list_files,
    "write_file": execute_write_file,
    "patch_file": execute_patch_file,
    "search_files": execute_search_files,
    "run_tests": execute_run_tests,
    "run_probe": execute_run_probe,
    "work_complete": _terminal_noop,
    "work_blocked": _terminal_noop,
}

TERMINAL_ACTIONS = {"work_complete", "work_blocked"}


def dispatch_action(
    scope: WorkerScope,
    action: str,
    params: dict[str, Any],
) -> dict[str, str]:
    executor = KNOWN_ACTIONS.get(action)
    if executor is None:
        return _action_result(
            "error",
            f"Unknown action: {action}. Valid actions: {', '.join(sorted(KNOWN_ACTIONS))}",
        )
    return executor(scope, params)
