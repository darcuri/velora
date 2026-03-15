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
    else:
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
