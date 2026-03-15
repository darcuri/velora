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
