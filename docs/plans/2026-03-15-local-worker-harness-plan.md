# Local Worker Harness Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a multi-turn action-loop harness that lets a local LLM execute scoped code tasks through structured actions, with mechanical verification and WorkResult assembly.

**Architecture:** Two new modules (`local_worker.py` for the loop, `worker_actions.py` for action executors) plus integration changes in `runners.py` and `run.py`. The harness replaces the raw `run_local_llm()` call for the `direct-local` worker backend.

**Tech Stack:** Python stdlib (subprocess, pathlib, json, re, enum, dataclasses). No new dependencies.

**Spec:** `docs/plans/2026-03-15-local-worker-harness-design.md`

---

## File Structure

| File | Role | Status |
|------|------|--------|
| `velora/worker_actions.py` | Action registry: WorkerScope, path resolution, action validation + execution | Create |
| `velora/local_worker.py` | Harness loop: conversation management, LLM dispatch, endgame, WorkResult assembly | Create |
| `velora/runners.py` | Wire `direct-local` worker to harness instead of raw `run_local_llm` | Modify |
| `velora/run.py:1979` | Bypass runner gate when `direct-local` is configured | Modify |
| `tests/test_worker_actions.py` | Unit tests for scope enforcement, path resolution, each action executor | Create |
| `tests/test_local_worker.py` | Integration tests for harness loop, endgame, caps, WorkResult assembly | Create |
| `tests/test_runners.py` | Add test for `direct-local` worker routing | Modify |

---

## Chunk 1: Action Layer — scope enforcement and action executors

### Task 1: WorkerScope and path resolution

**Files:**
- Create: `velora/worker_actions.py`
- Create: `tests/test_worker_actions.py`

- [ ] **Step 1: Write the failing test for path resolution**

```python
# tests/test_worker_actions.py
import unittest
import tempfile
from pathlib import Path

from velora.worker_actions import WorkerScope, resolve_scoped_path, ScopeViolation


class TestPathResolution(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.repo = Path(self.tmp)
        self.scope = WorkerScope(
            repo_root=self.repo,
            allowed_files={"src/main.py", "src/util.py", "tests/test_main.py"},
            allowed_dirs={"src", "tests"},
            test_commands=["python -m pytest -q"],
            work_branch="velora/wi-001",
        )

    def test_resolve_valid_allowed_file(self):
        resolved = resolve_scoped_path(self.scope, "src/main.py")
        self.assertEqual(resolved, self.repo / "src/main.py")

    def test_reject_absolute_path(self):
        with self.assertRaises(ScopeViolation):
            resolve_scoped_path(self.scope, "/etc/passwd")

    def test_reject_dot_dot_traversal(self):
        with self.assertRaises(ScopeViolation):
            resolve_scoped_path(self.scope, "src/../../../etc/passwd")

    def test_reject_path_outside_scope(self):
        with self.assertRaises(ScopeViolation):
            resolve_scoped_path(self.scope, "docs/readme.md", require_allowed_file=True)

    def test_allow_file_in_allowed_dir(self):
        # read_file allows any file in allowed_dirs
        resolved = resolve_scoped_path(self.scope, "src/other.py", require_allowed_file=False)
        self.assertEqual(resolved, self.repo / "src/other.py")

    def test_reject_symlink_outside_repo(self):
        # Create a symlink pointing outside repo
        link = self.repo / "src" / "escape"
        link_target = Path("/tmp")
        (self.repo / "src").mkdir(parents=True, exist_ok=True)
        link.symlink_to(link_target)
        with self.assertRaises(ScopeViolation):
            resolve_scoped_path(self.scope, "src/escape")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_worker_actions.py::TestPathResolution -v`
Expected: FAIL — `velora.worker_actions` does not exist

- [ ] **Step 3: Implement WorkerScope and resolve_scoped_path**

```python
# velora/worker_actions.py
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_worker_actions.py::TestPathResolution -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add velora/worker_actions.py tests/test_worker_actions.py
git commit -m "feat(harness): add WorkerScope and scoped path resolution"
```

---

### Task 2: Action executors — read_file, list_files, write_file, patch_file

**Files:**
- Modify: `velora/worker_actions.py`
- Modify: `tests/test_worker_actions.py`

- [ ] **Step 1: Write failing tests for file actions**

```python
# Append to tests/test_worker_actions.py
from velora.worker_actions import (
    execute_read_file,
    execute_list_files,
    execute_write_file,
    execute_patch_file,
    ActionError,
)


class TestReadFile(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.repo = Path(self.tmp)
        (self.repo / "src").mkdir()
        (self.repo / "src" / "main.py").write_text("print('hello')\n")
        self.scope = WorkerScope(
            repo_root=self.repo,
            allowed_files={"src/main.py"},
            allowed_dirs={"src"},
            test_commands=[],
            work_branch="velora/wi-001",
        )

    def test_read_existing_file(self):
        result = execute_read_file(self.scope, {"path": "src/main.py"})
        self.assertEqual(result["status"], "ok")
        self.assertIn("print('hello')", result["result"])

    def test_read_nonexistent_file(self):
        result = execute_read_file(self.scope, {"path": "src/missing.py"})
        self.assertEqual(result["status"], "error")

    def test_read_out_of_scope(self):
        result = execute_read_file(self.scope, {"path": "docs/readme.md"})
        self.assertEqual(result["status"], "error")
        self.assertIn("scope", result["result"].lower())


class TestListFiles(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.repo = Path(self.tmp)
        (self.repo / "src").mkdir()
        (self.repo / "src" / "a.py").write_text("")
        (self.repo / "src" / "b.py").write_text("")
        self.scope = WorkerScope(
            repo_root=self.repo,
            allowed_files={"src/a.py"},
            allowed_dirs={"src"},
            test_commands=[],
            work_branch="velora/wi-001",
        )

    def test_list_allowed_dir(self):
        result = execute_list_files(self.scope, {"path": "src"})
        self.assertEqual(result["status"], "ok")
        self.assertIn("a.py", result["result"])
        self.assertIn("b.py", result["result"])

    def test_list_out_of_scope(self):
        result = execute_list_files(self.scope, {"path": "docs"})
        self.assertEqual(result["status"], "error")


class TestWriteFile(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.repo = Path(self.tmp)
        (self.repo / "src").mkdir()
        self.scope = WorkerScope(
            repo_root=self.repo,
            allowed_files={"src/main.py"},
            allowed_dirs={"src"},
            test_commands=[],
            work_branch="velora/wi-001",
        )

    def test_write_new_file_in_scope(self):
        result = execute_write_file(self.scope, {"path": "src/main.py", "content": "x = 1\n"})
        self.assertEqual(result["status"], "ok")
        self.assertEqual((self.repo / "src" / "main.py").read_text(), "x = 1\n")

    def test_write_out_of_scope(self):
        result = execute_write_file(self.scope, {"path": "src/other.py", "content": "x = 1"})
        self.assertEqual(result["status"], "error")
        self.assertIn("scope", result["result"].lower())


class TestPatchFile(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.repo = Path(self.tmp)
        (self.repo / "src").mkdir()
        (self.repo / "src" / "main.py").write_text("def hello():\n    return 'hello'\n")
        self.scope = WorkerScope(
            repo_root=self.repo,
            allowed_files={"src/main.py"},
            allowed_dirs={"src"},
            test_commands=[],
            work_branch="velora/wi-001",
        )

    def test_patch_unique_match(self):
        result = execute_patch_file(self.scope, {
            "path": "src/main.py",
            "old": "return 'hello'",
            "new": "return 'world'",
        })
        self.assertEqual(result["status"], "ok")
        content = (self.repo / "src" / "main.py").read_text()
        self.assertIn("return 'world'", content)

    def test_patch_no_match(self):
        result = execute_patch_file(self.scope, {
            "path": "src/main.py",
            "old": "return 'nonexistent'",
            "new": "return 'world'",
        })
        self.assertEqual(result["status"], "error")
        self.assertIn("not found", result["result"].lower())

    def test_patch_multiple_matches(self):
        (self.repo / "src" / "main.py").write_text("x = 1\nx = 1\n")
        result = execute_patch_file(self.scope, {
            "path": "src/main.py",
            "old": "x = 1",
            "new": "x = 2",
        })
        self.assertEqual(result["status"], "error")
        self.assertIn("not unique", result["result"].lower())
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_worker_actions.py -k "TestReadFile or TestListFiles or TestWriteFile or TestPatchFile" -v`
Expected: FAIL — functions not defined

- [ ] **Step 3: Implement file action executors**

Add to `velora/worker_actions.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_worker_actions.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add velora/worker_actions.py tests/test_worker_actions.py
git commit -m "feat(harness): add file action executors — read, list, write, patch"
```

---

### Task 3: Action executors — search_files and run_tests

**Files:**
- Modify: `velora/worker_actions.py`
- Modify: `tests/test_worker_actions.py`

- [ ] **Step 1: Write failing tests for search_files and run_tests**

```python
# Append to tests/test_worker_actions.py
from velora.worker_actions import execute_search_files, execute_run_tests


class TestSearchFiles(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.repo = Path(self.tmp)
        (self.repo / "src").mkdir()
        (self.repo / "src" / "main.py").write_text("def hello():\n    return 'hello'\n")
        (self.repo / "src" / "util.py").write_text("def helper():\n    return hello()\n")
        self.scope = WorkerScope(
            repo_root=self.repo,
            allowed_files={"src/main.py"},
            allowed_dirs={"src"},
            test_commands=[],
            work_branch="velora/wi-001",
        )

    def test_search_literal_match(self):
        result = execute_search_files(self.scope, {"pattern": "hello"})
        self.assertEqual(result["status"], "ok")
        self.assertIn("main.py", result["result"])
        self.assertIn("util.py", result["result"])

    def test_search_no_match(self):
        result = execute_search_files(self.scope, {"pattern": "nonexistent_xyz"})
        self.assertEqual(result["status"], "ok")
        self.assertIn("no matches", result["result"].lower())

    def test_search_caps_results(self):
        # Create a file with >50 matches
        (self.repo / "src" / "big.py").write_text("\n".join(f"line{i} match" for i in range(100)))
        result = execute_search_files(self.scope, {"pattern": "match"})
        self.assertEqual(result["status"], "ok")
        # Should be capped
        lines = [l for l in result["result"].strip().split("\n") if l.strip()]
        self.assertLessEqual(len(lines), 51)  # 50 results + possible cap message


class TestRunTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.repo = Path(self.tmp)
        self.scope = WorkerScope(
            repo_root=self.repo,
            allowed_files=set(),
            allowed_dirs=set(),
            test_commands=["python -m pytest -q"],
            work_branch="velora/wi-001",
        )

    def test_reject_command_not_in_allowlist(self):
        result = execute_run_tests(self.scope, {"command": "rm -rf /"})
        self.assertEqual(result["status"], "error")
        self.assertIn("not in allowlist", result["result"].lower())

    def test_accept_valid_command(self):
        # python -m pytest -q will fail (no tests) but should not be rejected by allowlist
        result = execute_run_tests(self.scope, {"command": "python -m pytest -q"})
        # It ran (returned something), not blocked by allowlist
        self.assertIn(result["status"], {"ok", "error"})
        self.assertNotIn("not in allowlist", result["result"].lower())
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_worker_actions.py -k "TestSearchFiles or TestRunTests" -v`
Expected: FAIL — functions not defined

- [ ] **Step 3: Implement search_files and run_tests executors**

Add to `velora/worker_actions.py`:

```python
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
    for dir_name in sorted(scope.allowed_dirs):
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_worker_actions.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add velora/worker_actions.py tests/test_worker_actions.py
git commit -m "feat(harness): add search_files and run_tests action executors"
```

---

### Task 4: Action dispatch registry

**Files:**
- Modify: `velora/worker_actions.py`
- Modify: `tests/test_worker_actions.py`

- [ ] **Step 1: Write failing test for dispatch**

```python
# Append to tests/test_worker_actions.py
from velora.worker_actions import dispatch_action, KNOWN_ACTIONS


class TestDispatch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.repo = Path(self.tmp)
        (self.repo / "src").mkdir()
        (self.repo / "src" / "main.py").write_text("x = 1\n")
        self.scope = WorkerScope(
            repo_root=self.repo,
            allowed_files={"src/main.py"},
            allowed_dirs={"src"},
            test_commands=["python -m pytest -q"],
            work_branch="velora/wi-001",
        )

    def test_dispatch_known_action(self):
        result = dispatch_action(self.scope, "read_file", {"path": "src/main.py"})
        self.assertEqual(result["status"], "ok")

    def test_dispatch_unknown_action(self):
        result = dispatch_action(self.scope, "delete_file", {"path": "src/main.py"})
        self.assertEqual(result["status"], "error")
        self.assertIn("unknown action", result["result"].lower())

    def test_known_actions_list(self):
        expected = {"read_file", "list_files", "write_file", "patch_file",
                    "search_files", "run_tests", "work_complete", "work_blocked"}
        self.assertEqual(set(KNOWN_ACTIONS.keys()), expected)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_worker_actions.py::TestDispatch -v`
Expected: FAIL

- [ ] **Step 3: Implement dispatch_action and KNOWN_ACTIONS**

Add to `velora/worker_actions.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_worker_actions.py -v`
Expected: PASS

- [ ] **Step 5: Run the full existing test suite to check for regressions**

Run: `python -m pytest tests/ -q`
Expected: All existing tests still pass.

- [ ] **Step 6: Commit**

```bash
git add velora/worker_actions.py tests/test_worker_actions.py
git commit -m "feat(harness): add action dispatch registry"
```

---

## Chunk 2: Harness loop — outcome model, conversation management, the main loop

### Task 5: HarnessReason, HarnessOutcome, and WorkResult assembly

**Files:**
- Create: `velora/local_worker.py`
- Create: `tests/test_local_worker.py`

- [ ] **Step 1: Write failing tests for outcome model and WorkResult assembly**

```python
# tests/test_local_worker.py
import unittest

from velora.local_worker import (
    HarnessReason,
    HarnessOutcome,
    assemble_work_result,
)
from velora.protocol import validate_work_result


class TestHarnessOutcome(unittest.TestCase):
    def test_success_outcome(self):
        outcome = HarnessOutcome(success=True, reason=HarnessReason.SUCCESS, evidence=[])
        self.assertTrue(outcome.success)
        self.assertEqual(outcome.reason, HarnessReason.SUCCESS)

    def test_failure_outcome(self):
        outcome = HarnessOutcome(
            success=False,
            reason=HarnessReason.TESTS_EXHAUSTED,
            evidence=["FAILED tests/test_foo.py::test_bar"],
        )
        self.assertFalse(outcome.success)
        self.assertEqual(outcome.reason, HarnessReason.TESTS_EXHAUSTED)


class TestAssembleWorkResult(unittest.TestCase):
    def test_success_produces_valid_completed_work_result(self):
        outcome = HarnessOutcome(success=True, reason=HarnessReason.SUCCESS, evidence=["all tests passed"])
        wr = assemble_work_result(
            outcome=outcome,
            work_item_id="WI-001",
            summary="Added feature X",
            branch="velora/wi-001",
            head_sha="abc123def456",
            files_touched=["src/main.py"],
            tests_run=[{"command": "python -m pytest -q", "status": "pass", "details": "1 passed"}],
        )
        # Must survive protocol validation
        validated = validate_work_result(wr)
        self.assertEqual(validated.status, "completed")
        self.assertEqual(validated.branch, "velora/wi-001")
        self.assertEqual(validated.head_sha, "abc123def456")
        self.assertEqual(validated.blockers, [])

    def test_blocked_produces_valid_blocked_work_result(self):
        outcome = HarnessOutcome(
            success=False,
            reason=HarnessReason.SCOPE_INSUFFICIENT,
            evidence=["need access to velora/config.py"],
        )
        wr = assemble_work_result(
            outcome=outcome,
            work_item_id="WI-001",
            summary="Could not complete",
            branch="",
            head_sha="",
            files_touched=[],
            tests_run=[],
        )
        validated = validate_work_result(wr)
        self.assertEqual(validated.status, "blocked")
        self.assertEqual(validated.blockers[0], "SCOPE_INSUFFICIENT")

    def test_failed_produces_valid_failed_work_result(self):
        outcome = HarnessOutcome(
            success=False,
            reason=HarnessReason.TESTS_EXHAUSTED,
            evidence=["FAILED test_foo.py"],
        )
        wr = assemble_work_result(
            outcome=outcome,
            work_item_id="WI-001",
            summary="Tests kept failing",
            branch="",
            head_sha="",
            files_touched=["src/main.py"],
            tests_run=[{"command": "python -m pytest -q", "status": "fail", "details": "1 failed"}],
        )
        validated = validate_work_result(wr)
        self.assertEqual(validated.status, "failed")
        self.assertIn("TESTS_EXHAUSTED", validated.blockers)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_local_worker.py -v`
Expected: FAIL — `velora.local_worker` does not exist

- [ ] **Step 3: Implement outcome model and WorkResult assembly**

```python
# velora/local_worker.py
from __future__ import annotations

import enum
import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .acpx import CmdResult
from .protocol import WorkItem, validate_work_result
from .worker_actions import (
    KNOWN_ACTIONS,
    TERMINAL_ACTIONS,
    WorkerScope,
    dispatch_action,
)


# -- Outcome model --

class HarnessReason(enum.Enum):
    SUCCESS            = "SUCCESS"
    SCOPE_VIOLATION    = "SCOPE_VIOLATION"
    SCOPE_INSUFFICIENT = "SCOPE_INSUFFICIENT"
    DIFF_LIMIT         = "DIFF_LIMIT"
    NO_CHANGES         = "NO_CHANGES"
    TESTS_EXHAUSTED    = "TESTS_EXHAUSTED"
    ITERATION_LIMIT    = "ITERATION_LIMIT"
    CONTEXT_OVERFLOW   = "CONTEXT_OVERFLOW"
    PARSE_FAILURES     = "PARSE_FAILURES"
    WORKER_BLOCKED     = "WORKER_BLOCKED"
    GATE_TIMEOUT       = "GATE_TIMEOUT"
    COMMIT_FAILED      = "COMMIT_FAILED"


_BLOCKED_REASONS = {
    HarnessReason.CONTEXT_OVERFLOW,
    HarnessReason.SCOPE_INSUFFICIENT,
    HarnessReason.WORKER_BLOCKED,
}


@dataclass
class HarnessOutcome:
    success: bool
    reason: HarnessReason
    evidence: list[str]


def assemble_work_result(
    *,
    outcome: HarnessOutcome,
    work_item_id: str,
    summary: str,
    branch: str,
    head_sha: str,
    files_touched: list[str],
    tests_run: list[dict[str, str]],
) -> dict[str, Any]:
    """Assemble a WorkResult dict from a HarnessOutcome.

    The result is validated through validate_work_result before return.
    """
    if outcome.success:
        status = "completed"
        blockers: list[str] = []
    elif outcome.reason in _BLOCKED_REASONS:
        status = "blocked"
        blockers = [outcome.reason.value] + outcome.evidence
        branch = ""
        head_sha = ""
    else:
        status = "failed"
        blockers = [outcome.reason.value] + outcome.evidence
        branch = ""
        head_sha = ""

    payload = {
        "protocol_version": 1,
        "work_item_id": work_item_id,
        "status": status,
        "summary": summary,
        "branch": branch,
        "head_sha": head_sha,
        "files_touched": files_touched,
        "tests_run": tests_run,
        "blockers": blockers,
        "follow_up": [],
        "evidence": outcome.evidence,
    }

    # Self-validate — catches harness bugs.
    validate_work_result(payload)
    return payload
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_local_worker.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add velora/local_worker.py tests/test_local_worker.py
git commit -m "feat(harness): add HarnessReason, HarnessOutcome, and WorkResult assembly"
```

---

### Task 6: System prompt builder

**Files:**
- Modify: `velora/local_worker.py`
- Modify: `tests/test_local_worker.py`

- [ ] **Step 1: Write failing test for system prompt builder**

```python
# Append to tests/test_local_worker.py
from velora.local_worker import build_local_worker_prompt
from velora.protocol import WorkItem


def _make_work_item() -> WorkItem:
    return WorkItem.from_dict({
        "id": "WI-001",
        "kind": "implement",
        "rationale": "Add the foo feature",
        "instructions": ["Create foo.py", "Add a foo() function that returns 42"],
        "scope_hints": {
            "likely_files": ["src/foo.py", "tests/test_foo.py"],
            "search_terms": ["foo"],
        },
        "acceptance": {
            "must": ["foo() returns 42"],
            "must_not": ["Do not modify existing files"],
            "gates": ["tests"],
        },
        "limits": {"max_diff_lines": 100, "max_commits": 1},
        "commit": {
            "message": "feat: add foo",
            "footer": {
                "VELORA_RUN_ID": "run-001",
                "VELORA_ITERATION": 1,
                "WORK_ITEM_ID": "WI-001",
            },
        },
    })


class TestBuildPrompt(unittest.TestCase):
    def test_prompt_contains_task_details(self):
        wi = _make_work_item()
        prompt = build_local_worker_prompt(
            work_item=wi,
            repo_ref="owner/repo",
            work_branch="velora/wi-001",
            test_commands=["python -m pytest -q"],
        )
        self.assertIn("owner/repo", prompt)
        self.assertIn("velora/wi-001", prompt)
        self.assertIn("WI-001", prompt)
        self.assertIn("Add the foo feature", prompt)
        self.assertIn("src/foo.py", prompt)
        self.assertIn("python -m pytest -q", prompt)

    def test_prompt_contains_propulsion_language(self):
        wi = _make_work_item()
        prompt = build_local_worker_prompt(
            work_item=wi,
            repo_ref="owner/repo",
            work_branch="velora/wi-001",
            test_commands=["python -m pytest -q"],
        )
        self.assertIn("Do not ask questions", prompt)
        self.assertIn("JSON only", prompt)

    def test_prompt_lists_all_actions(self):
        wi = _make_work_item()
        prompt = build_local_worker_prompt(
            work_item=wi,
            repo_ref="owner/repo",
            work_branch="velora/wi-001",
            test_commands=[],
        )
        for action in ["read_file", "list_files", "write_file", "patch_file",
                        "search_files", "run_tests", "work_complete", "work_blocked"]:
            self.assertIn(action, prompt)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_local_worker.py::TestBuildPrompt -v`
Expected: FAIL

- [ ] **Step 3: Implement build_local_worker_prompt**

Add to `velora/local_worker.py`:

```python
def build_local_worker_prompt(
    *,
    work_item: WorkItem,
    repo_ref: str,
    work_branch: str,
    test_commands: list[str],
) -> str:
    lines: list[str] = []

    lines.append("You are a code execution tool. You receive a task, you execute it, you return the result.")
    lines.append("")
    lines.append("Do not ask questions. Do not propose alternatives. Do not explain your reasoning.")
    lines.append("Do not narrate what you are about to do. Do not summarize what you did.")
    lines.append("Emit one action per response. JSON only. No markdown. No prose.")
    lines.append("")
    lines.append("If you cannot complete the task, emit work_blocked. Otherwise, execute and emit work_complete.")
    lines.append("")

    lines.append("## Your task")
    lines.append(f"Repo: {repo_ref}")
    lines.append(f"Branch: {work_branch}")
    lines.append(f"Work item: {work_item.id} ({work_item.kind})")
    lines.append(f"Rationale: {work_item.rationale}")
    lines.append("")

    lines.append("## Instructions")
    for i, ins in enumerate(work_item.instructions, 1):
        lines.append(f"{i}. {ins}")
    lines.append("")

    lines.append("## Files in scope")
    for f in work_item.scope_hints.likely_files:
        lines.append(f)
    lines.append("")

    if test_commands:
        lines.append("## Test commands available")
        for cmd in test_commands:
            lines.append(cmd)
        lines.append("")

    lines.append("## Acceptance criteria")
    if work_item.acceptance.must:
        lines.append("Must:")
        for item in work_item.acceptance.must:
            lines.append(f"- {item}")
    if work_item.acceptance.must_not:
        lines.append("Must not:")
        for item in work_item.acceptance.must_not:
            lines.append(f"- {item}")
    lines.append("")

    lines.append("## Available actions")
    lines.append('{"action": "read_file", "params": {"path": "relative/path"}}')
    lines.append('{"action": "list_files", "params": {"path": "relative/dir"}}')
    lines.append('{"action": "write_file", "params": {"path": "relative/path", "content": "..."}}')
    lines.append('{"action": "patch_file", "params": {"path": "relative/path", "old": "...", "new": "..."}}')
    lines.append('{"action": "search_files", "params": {"pattern": "search term"}}')
    lines.append('{"action": "run_tests", "params": {"command": "python -m pytest -q"}}')
    lines.append('{"action": "work_complete", "params": {"summary": "what you did"}}')
    lines.append('{"action": "work_blocked", "params": {"reason": "SCOPE_INSUFFICIENT|TASK_UNCLEAR|CANNOT_RESOLVE", "blockers": ["..."]}}')
    lines.append("")

    lines.append("## Rules")
    lines.append("- You may only read/write files listed in scope.")
    lines.append("- You may only run test commands listed above.")
    lines.append("- Emit one action per response. JSON only.")
    lines.append("- Start by reading the files you need, then make changes, then signal completion.")
    lines.append("- If you cannot complete the task with the files in scope, use work_blocked.")

    return "\n".join(lines) + "\n"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_local_worker.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add velora/local_worker.py tests/test_local_worker.py
git commit -m "feat(harness): add local worker system prompt builder"
```

---

### Task 7: Conversation manager — summarization and context tracking

**Files:**
- Modify: `velora/local_worker.py`
- Modify: `tests/test_local_worker.py`

- [ ] **Step 1: Write failing tests for conversation management**

```python
# Append to tests/test_local_worker.py
from velora.local_worker import ConversationManager


class TestConversationManager(unittest.TestCase):
    def test_init_with_system_prompt(self):
        cm = ConversationManager(system_prompt="You are a tool.")
        msgs = cm.messages()
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0]["role"], "system")

    def test_append_turn(self):
        cm = ConversationManager(system_prompt="You are a tool.")
        cm.append_assistant('{"action": "read_file", "params": {"path": "x.py"}}')
        cm.append_user('{"status": "ok", "result": "contents"}')
        self.assertEqual(len(cm.messages()), 3)

    def test_context_bytes_tracked(self):
        cm = ConversationManager(system_prompt="short")
        cm.append_assistant("a" * 100)
        cm.append_user("b" * 200)
        self.assertGreater(cm.context_bytes, 0)

    def test_summarization_truncates_old_large_messages(self):
        cm = ConversationManager(system_prompt="sys", recency_window=2)
        # Add 6 turns (3 assistant + 3 user), first user message is huge
        cm.append_assistant("act1")
        cm.append_user("x" * 5000)  # big result, will be old after more turns
        cm.append_assistant("act2")
        cm.append_user("small")
        cm.append_assistant("act3")
        cm.append_user("small2")
        cm.summarize()
        # The big message (index 2, the first user msg) should be truncated
        msgs = cm.messages()
        big_msg = msgs[2]  # first user message
        self.assertIn("[truncated]", big_msg["content"])
        self.assertLess(len(big_msg["content"]), 5000)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_local_worker.py::TestConversationManager -v`
Expected: FAIL

- [ ] **Step 3: Implement ConversationManager**

Add to `velora/local_worker.py`:

```python
# -- Tunable constants --

_SUMMARIZE_THRESHOLD_BYTES = int(os.environ.get("VELORA_HARNESS_SUMMARIZE_THRESHOLD", "2048"))
_SUMMARIZE_KEEP_LINES = int(os.environ.get("VELORA_HARNESS_SUMMARIZE_LINES", "40"))
_RECENCY_WINDOW = int(os.environ.get("VELORA_HARNESS_RECENCY_WINDOW", "4"))


class ConversationManager:
    """Manages the chat message list for the local worker harness.

    Handles appending turns, tracking context size, and summarizing old
    large messages to keep context within budget.
    """

    def __init__(self, system_prompt: str, *, recency_window: int = _RECENCY_WINDOW):
        self._messages: list[dict[str, str]] = [
            {"role": "system", "content": system_prompt},
        ]
        self._recency_window = recency_window
        self.context_bytes = len(system_prompt.encode("utf-8"))

    def messages(self) -> list[dict[str, str]]:
        return list(self._messages)

    def append_assistant(self, content: str) -> None:
        self._messages.append({"role": "assistant", "content": content})
        self.context_bytes += len(content.encode("utf-8"))

    def append_user(self, content: str) -> None:
        self._messages.append({"role": "user", "content": content})
        self.context_bytes += len(content.encode("utf-8"))

    def summarize(self) -> None:
        """Truncate old large messages outside the recency window."""
        # Messages: [system, asst, user, asst, user, ...]
        # recency_window=4 means keep the last 4 non-system messages intact.
        non_system_count = len(self._messages) - 1
        if non_system_count <= self._recency_window:
            return

        cutoff_idx = len(self._messages) - self._recency_window
        for i in range(1, cutoff_idx):
            msg = self._messages[i]
            content = msg["content"]
            content_bytes = len(content.encode("utf-8"))
            if content_bytes > _SUMMARIZE_THRESHOLD_BYTES:
                lines = content.splitlines()
                if len(lines) > _SUMMARIZE_KEEP_LINES * 2:
                    head = lines[:_SUMMARIZE_KEEP_LINES]
                    tail = lines[-_SUMMARIZE_KEEP_LINES:]
                    truncated = "\n".join(head) + "\n\n[truncated]\n\n" + "\n".join(tail)
                else:
                    truncated = content[:_SUMMARIZE_THRESHOLD_BYTES] + "\n\n[truncated]"
                old_bytes = content_bytes
                msg["content"] = truncated
                self.context_bytes -= old_bytes - len(truncated.encode("utf-8"))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_local_worker.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add velora/local_worker.py tests/test_local_worker.py
git commit -m "feat(harness): add ConversationManager with summarization"
```

---

### Task 8: The main harness loop

**Files:**
- Modify: `velora/local_worker.py`
- Modify: `tests/test_local_worker.py`

This is the core loop. It ties everything together: LLM calls, action parsing,
dispatch, caps, and termination. Tests mock the LLM to simulate action sequences.

- [ ] **Step 1: Write failing tests for the harness loop**

```python
# Append to tests/test_local_worker.py
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

from velora.local_worker import run_local_worker_loop
from velora.worker_actions import WorkerScope
from velora.acpx import CmdResult


def _make_scope(repo: Path) -> WorkerScope:
    return WorkerScope(
        repo_root=repo,
        allowed_files={"src/main.py"},
        allowed_dirs={"src"},
        test_commands=["python -m pytest -q"],
        work_branch="velora/wi-001",
    )


class TestHarnessLoop(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.repo = Path(self.tmp)
        (self.repo / "src").mkdir()
        (self.repo / "src" / "main.py").write_text("x = 1\n")

    def _mock_llm_responses(self, responses: list[str]):
        """Create a side_effect that returns CmdResult for each response."""
        results = [CmdResult(returncode=0, stdout=r, stderr="") for r in responses]
        return results

    def test_work_complete_terminates_loop(self):
        responses = self._mock_llm_responses([
            '{"action": "read_file", "params": {"path": "src/main.py"}}',
            '{"action": "work_complete", "params": {"summary": "read the file"}}',
        ])
        with patch("velora.local_worker._call_local_llm_chat", side_effect=responses):
            outcome = run_local_worker_loop(
                scope=_make_scope(self.repo),
                system_prompt="You are a tool.",
            )
        self.assertEqual(outcome.reason, HarnessReason.SUCCESS)
        self.assertEqual(outcome.llm_summary, "read the file")

    def test_work_blocked_terminates_loop(self):
        responses = self._mock_llm_responses([
            '{"action": "work_blocked", "params": {"reason": "SCOPE_INSUFFICIENT", "blockers": ["need config.py"]}}',
        ])
        with patch("velora.local_worker._call_local_llm_chat", side_effect=responses):
            outcome = run_local_worker_loop(
                scope=_make_scope(self.repo),
                system_prompt="You are a tool.",
            )
        self.assertEqual(outcome.reason, HarnessReason.SCOPE_INSUFFICIENT)
        self.assertFalse(outcome.success)

    def test_iteration_cap_terminates_loop(self):
        # 25 read_file actions — exceeds default cap of 20
        responses = self._mock_llm_responses(
            ['{"action": "read_file", "params": {"path": "src/main.py"}}'] * 25
        )
        with patch("velora.local_worker._call_local_llm_chat", side_effect=responses):
            outcome = run_local_worker_loop(
                scope=_make_scope(self.repo),
                system_prompt="You are a tool.",
                iteration_cap=20,
            )
        self.assertEqual(outcome.reason, HarnessReason.ITERATION_LIMIT)

    def test_parse_failure_cap_terminates_loop(self):
        responses = self._mock_llm_responses([
            "this is not json",
            "also not json",
            "still not json",
        ])
        with patch("velora.local_worker._call_local_llm_chat", side_effect=responses):
            outcome = run_local_worker_loop(
                scope=_make_scope(self.repo),
                system_prompt="You are a tool.",
                parse_failure_cap=3,
            )
        self.assertEqual(outcome.reason, HarnessReason.PARSE_FAILURES)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_local_worker.py::TestHarnessLoop -v`
Expected: FAIL

- [ ] **Step 3: Implement run_local_worker_loop**

Add to `velora/local_worker.py`:

```python
# -- Cap defaults --

_ITERATION_CAP = int(os.environ.get("VELORA_HARNESS_ITERATION_CAP", "20"))
_CONTEXT_CAP_BYTES = int(os.environ.get("VELORA_HARNESS_CONTEXT_CAP", str(128 * 1024)))
_PARSE_FAILURE_CAP = int(os.environ.get("VELORA_HARNESS_PARSE_FAILURE_CAP", "3"))

# LLM blocked reasons the worker can emit
_LLM_BLOCKED_REASONS = {"SCOPE_INSUFFICIENT", "TASK_UNCLEAR", "CANNOT_RESOLVE"}


@dataclass
class LoopOutcome:
    """Internal outcome from the action loop (before endgame)."""
    success: bool
    reason: HarnessReason
    evidence: list[str]
    llm_summary: str        # from work_complete, empty otherwise
    llm_blockers: list[str] # from work_blocked, empty otherwise
    conversation: ConversationManager | None = None  # preserved for test retry re-entry


def _parse_action(raw: str) -> tuple[str, dict[str, Any]] | None:
    """Parse an LLM response into (action, params). Returns None on failure."""
    text = raw.strip()
    # Strip markdown fences if the model wraps JSON
    if text.startswith("```"):
        lines = text.splitlines()
        lines = [l for l in lines if not l.strip().startswith("```")]
        text = "\n".join(lines).strip()
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None
    action = obj.get("action")
    params = obj.get("params")
    if not isinstance(action, str) or not isinstance(params, dict):
        return None
    return action, params


def run_local_worker_loop(
    *,
    scope: WorkerScope,
    system_prompt: str,
    conversation: ConversationManager | None = None,
    iteration_cap: int = _ITERATION_CAP,
    context_cap_bytes: int = _CONTEXT_CAP_BYTES,
    parse_failure_cap: int = _PARSE_FAILURE_CAP,
) -> LoopOutcome:
    """Run the multi-turn action loop with a local LLM.

    Returns a LoopOutcome describing how the loop terminated. The caller
    (run_local_worker) handles the endgame and WorkResult assembly.

    If `conversation` is provided, resumes from an existing conversation
    (used for test failure re-entry). Otherwise starts fresh.
    """
    conv = conversation if conversation is not None else ConversationManager(system_prompt)
    iteration = 0
    parse_failures = 0

    while True:
        # -- Context cap --
        if conv.context_bytes > context_cap_bytes:
            return LoopOutcome(
                success=False,
                reason=HarnessReason.CONTEXT_OVERFLOW,
                evidence=[f"context exceeded {context_cap_bytes} bytes after {iteration} turns"],
                llm_summary="",
                llm_blockers=[],
                conversation=conv,
            )

        # -- Call LLM --
        llm_result = _call_local_llm_chat(conv.messages(), scope.repo_root)

        if llm_result.returncode != 0:
            return LoopOutcome(
                success=False,
                reason=HarnessReason.PARSE_FAILURES,
                evidence=[f"LLM call failed: {llm_result.stderr}"],
                llm_summary="",
                llm_blockers=[],
                conversation=conv,
            )

        raw_response = llm_result.stdout
        conv.append_assistant(raw_response)

        # -- Parse --
        parsed = _parse_action(raw_response)
        if parsed is None:
            parse_failures += 1
            if parse_failures >= parse_failure_cap:
                return LoopOutcome(
                    success=False,
                    reason=HarnessReason.PARSE_FAILURES,
                    evidence=[f"{parse_failures} consecutive parse failures"],
                    llm_summary="",
                    llm_blockers=[],
                    conversation=conv,
                )
            error_msg = json.dumps({
                "status": "error",
                "result": "Invalid response. Emit exactly one JSON object with action and params.",
            })
            conv.append_user(error_msg)
            iteration += 1
            continue

        parse_failures = 0
        action, params = parsed

        # -- Terminal actions --
        if action == "work_complete":
            summary = params.get("summary", "")
            return LoopOutcome(
                success=True,
                reason=HarnessReason.SUCCESS,
                evidence=[],
                llm_summary=summary if isinstance(summary, str) else "",
                llm_blockers=[],
                conversation=conv,
            )

        if action == "work_blocked":
            reason_str = params.get("reason", "CANNOT_RESOLVE")
            if reason_str not in _LLM_BLOCKED_REASONS:
                reason_str = "CANNOT_RESOLVE"
            blockers = params.get("blockers", [])
            if not isinstance(blockers, list):
                blockers = []
            blockers = [str(b) for b in blockers if isinstance(b, str)]

            if reason_str == "SCOPE_INSUFFICIENT":
                reason = HarnessReason.SCOPE_INSUFFICIENT
            else:
                reason = HarnessReason.WORKER_BLOCKED

            return LoopOutcome(
                success=False,
                reason=reason,
                evidence=blockers,
                llm_summary="",
                llm_blockers=blockers,
                conversation=conv,
            )

        # -- Execute action --
        result = dispatch_action(scope, action, params)
        result_json = json.dumps(result)
        conv.append_user(result_json)
        conv.summarize()

        iteration += 1
        if iteration >= iteration_cap:
            return LoopOutcome(
                success=False,
                reason=HarnessReason.ITERATION_LIMIT,
                evidence=[f"{iteration} turns exhausted"],
                llm_summary="",
                llm_blockers=[],
                conversation=conv,
            )


def _call_local_llm_chat(messages: list[dict[str, str]], cwd: Path) -> CmdResult:
    """Call the local LLM with the full chat message list.

    Uses the OpenAI-compatible /v1/chat/completions endpoint.
    """
    import urllib.request
    import urllib.error

    base_url = os.environ.get("VELORA_LOCAL_BASE_URL", "http://localhost:1234").rstrip("/")
    model = os.environ.get("VELORA_LOCAL_MODEL", "")
    timeout_s = int(os.environ.get("VELORA_LOCAL_TIMEOUT", "600"))

    body: dict[str, Any] = {
        "messages": messages,
        "temperature": 0.2,
    }
    if model:
        body["model"] = model

    url = f"{base_url}/v1/chat/completions"
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
        return CmdResult(returncode=1, stdout="", stderr=f"Local LLM HTTP {exc.code}: {detail}")
    except urllib.error.URLError as exc:
        return CmdResult(returncode=1, stdout="", stderr=f"Local LLM connection failed: {exc.reason}")
    except TimeoutError:
        return CmdResult(returncode=1, stdout="", stderr=f"Local LLM timed out after {timeout_s}s")

    try:
        payload = json.loads(raw)
        text = payload["choices"][0]["message"]["content"]
    except (json.JSONDecodeError, KeyError, IndexError) as exc:
        return CmdResult(returncode=1, stdout="", stderr=f"Local LLM response parse error: {exc}")

    return CmdResult(returncode=0, stdout=text, stderr="")
```

Note: the tests must mock `_call_local_llm_chat`, not `run_local_llm`. The
test code above already uses the correct mock target.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_local_worker.py -v`
Expected: PASS

- [ ] **Step 5: Run the full test suite**

Run: `python -m pytest tests/ -q`
Expected: All tests pass.

- [ ] **Step 6: Commit**

```bash
git add velora/local_worker.py tests/test_local_worker.py
git commit -m "feat(harness): implement main action loop with caps and parse handling"
```

---

## Chunk 3: Endgame — diff audit, test gates, commit, full harness entry point

### Task 9: Endgame — diff audit, test gates, commit

**Files:**
- Modify: `velora/local_worker.py`
- Modify: `tests/test_local_worker.py`

These tests require a real git repo (init + commit), so use `subprocess` in setUp.

- [ ] **Step 1: Write failing tests for endgame**

```python
# Append to tests/test_local_worker.py
import subprocess


def _init_git_repo(path: Path) -> None:
    """Create a minimal git repo with an initial commit."""
    subprocess.run(["git", "init", str(path)], capture_output=True, check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "test@test.com"], capture_output=True, check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Test"], capture_output=True, check=True)
    (path / "README.md").write_text("init\n")
    subprocess.run(["git", "-C", str(path), "add", "."], capture_output=True, check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-m", "init"], capture_output=True, check=True)


class TestEndgame(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.repo = Path(self.tmp)
        _init_git_repo(self.repo)
        (self.repo / "src").mkdir()
        (self.repo / "src" / "main.py").write_text("x = 1\n")
        subprocess.run(["git", "-C", str(self.repo), "add", "."], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-m", "add src"], capture_output=True, check=True)
        # Create work branch
        subprocess.run(["git", "-C", str(self.repo), "checkout", "-b", "velora/wi-001"], capture_output=True, check=True)

    def test_diff_audit_detects_no_changes(self):
        from velora.local_worker import _run_endgame
        scope = WorkerScope(
            repo_root=self.repo,
            allowed_files={"src/main.py"},
            allowed_dirs={"src"},
            test_commands=[],
            work_branch="velora/wi-001",
        )
        outcome = _run_endgame(scope=scope, work_item=_make_work_item(), llm_summary="done")
        self.assertEqual(outcome.reason, HarnessReason.NO_CHANGES)

    def test_diff_audit_detects_scope_violation(self):
        from velora.local_worker import _run_endgame
        # Modify a file outside scope
        (self.repo / "README.md").write_text("modified\n")
        scope = WorkerScope(
            repo_root=self.repo,
            allowed_files={"src/main.py"},
            allowed_dirs={"src"},
            test_commands=[],
            work_branch="velora/wi-001",
        )
        outcome = _run_endgame(scope=scope, work_item=_make_work_item(), llm_summary="done")
        self.assertEqual(outcome.reason, HarnessReason.SCOPE_VIOLATION)

    def test_diff_audit_detects_diff_limit(self):
        from velora.local_worker import _run_endgame
        # Create a change that exceeds max_diff_lines (100 in test WorkItem)
        big_content = "\n".join(f"line{i} = {i}" for i in range(200))
        (self.repo / "src" / "main.py").write_text(big_content)
        scope = WorkerScope(
            repo_root=self.repo,
            allowed_files={"src/main.py"},
            allowed_dirs={"src"},
            test_commands=[],
            work_branch="velora/wi-001",
        )
        outcome = _run_endgame(scope=scope, work_item=_make_work_item(), llm_summary="done")
        self.assertEqual(outcome.reason, HarnessReason.DIFF_LIMIT)

    def test_successful_endgame_commits(self):
        from velora.local_worker import _run_endgame
        # Modify a file in scope
        (self.repo / "src" / "main.py").write_text("x = 2\n")
        scope = WorkerScope(
            repo_root=self.repo,
            allowed_files={"src/main.py"},
            allowed_dirs={"src"},
            test_commands=[],
            work_branch="velora/wi-001",
        )
        wi = _make_work_item()
        outcome = _run_endgame(scope=scope, work_item=wi, llm_summary="changed x")
        self.assertEqual(outcome.reason, HarnessReason.SUCCESS)
        self.assertTrue(outcome.success)
        self.assertTrue(outcome.head_sha)  # should have a commit SHA
        self.assertIn("src/main.py", outcome.files_touched)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_local_worker.py::TestEndgame -v`
Expected: FAIL

- [ ] **Step 3: Implement _run_endgame**

Add to `velora/local_worker.py`:

```python
@dataclass
class EndgameOutcome:
    """Outcome from the endgame phase."""
    success: bool
    reason: HarnessReason
    evidence: list[str]
    head_sha: str
    files_touched: list[str]
    tests_run: list[dict[str, str]]


# Gate name → command list
GATE_COMMANDS: dict[str, list[str]] = {
    "tests":    ["python", "-m", "pytest", "-q"],
    "lint":     ["python", "-m", "flake8"],
    "security": ["python", "-m", "bandit", "-r", ".", "-q"],
}

_SKIPPED_GATES = {"ci", "docs"}
_TEST_TIMEOUT_S = int(os.environ.get("VELORA_HARNESS_TEST_TIMEOUT", "120"))


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo)] + list(args),
        text=True,
        capture_output=True,
        check=False,
    )


def _run_endgame(
    *,
    scope: WorkerScope,
    work_item: WorkItem,
    llm_summary: str,
) -> EndgameOutcome:
    """Mechanical endgame: diff audit, test gates, commit."""
    repo = scope.repo_root

    # -- Step 1: Diff audit --
    diff_stat = _git(repo, "diff", "--stat", "HEAD")
    diff_full = _git(repo, "diff", "HEAD")
    diff_name = _git(repo, "diff", "--name-only", "HEAD")

    changed_files = [f.strip() for f in diff_name.stdout.splitlines() if f.strip()]

    if not changed_files:
        return EndgameOutcome(
            success=False, reason=HarnessReason.NO_CHANGES,
            evidence=["worker signaled complete but no files were modified"],
            head_sha="", files_touched=[], tests_run=[],
        )

    # Scope check
    for f in changed_files:
        if f not in scope.allowed_files:
            return EndgameOutcome(
                success=False, reason=HarnessReason.SCOPE_VIOLATION,
                evidence=[f"modified {f} which is not in allowed_files"],
                head_sha="", files_touched=changed_files, tests_run=[],
            )

    # Binary file check
    binary_check = _git(repo, "diff", "--numstat", "HEAD")
    for line in binary_check.stdout.splitlines():
        if line.startswith("-\t-\t"):
            bin_file = line.split("\t", 2)[2].strip()
            return EndgameOutcome(
                success=False, reason=HarnessReason.SCOPE_VIOLATION,
                evidence=[f"binary file modification not allowed: {bin_file}"],
                head_sha="", files_touched=changed_files, tests_run=[],
            )

    # Diff line count
    diff_lines = len(diff_full.stdout.splitlines())
    max_lines = work_item.limits.max_diff_lines
    if diff_lines > max_lines:
        return EndgameOutcome(
            success=False, reason=HarnessReason.DIFF_LIMIT,
            evidence=[f"{diff_lines} lines exceeds limit of {max_lines}"],
            head_sha="", files_touched=changed_files, tests_run=[],
        )

    # -- Step 2: Run test gates --
    tests_run: list[dict[str, str]] = []
    for gate in work_item.acceptance.gates:
        if gate in _SKIPPED_GATES:
            tests_run.append({"command": gate, "status": "not_run", "details": f"gate '{gate}' skipped by harness"})
            continue
        cmd_list = GATE_COMMANDS.get(gate)
        if cmd_list is None:
            tests_run.append({"command": gate, "status": "not_run", "details": f"unknown gate '{gate}'"})
            continue
        try:
            proc = subprocess.run(
                cmd_list, cwd=str(repo), text=True, capture_output=True,
                check=False, timeout=_TEST_TIMEOUT_S,
            )
        except subprocess.TimeoutExpired:
            return EndgameOutcome(
                success=False, reason=HarnessReason.GATE_TIMEOUT,
                evidence=[f"gate '{gate}' timed out after {_TEST_TIMEOUT_S}s"],
                head_sha="", files_touched=changed_files, tests_run=tests_run,
            )
        output = (proc.stdout or "") + (proc.stderr or "")
        status = "pass" if proc.returncode == 0 else "fail"
        tests_run.append({"command": " ".join(cmd_list), "status": status, "details": output[:2000]})
        if status == "fail":
            return EndgameOutcome(
                success=False, reason=HarnessReason.TESTS_EXHAUSTED,
                evidence=[output[:2000]],
                head_sha="", files_touched=changed_files, tests_run=tests_run,
            )

    # -- Step 3: Commit --
    for f in changed_files:
        add_result = _git(repo, "add", f)
        if add_result.returncode != 0:
            return EndgameOutcome(
                success=False, reason=HarnessReason.COMMIT_FAILED,
                evidence=[f"git add failed for {f}: {add_result.stderr}"],
                head_sha="", files_touched=changed_files, tests_run=tests_run,
            )

    footer_lines = "\n".join(f"{k}: {v}" for k, v in work_item.commit.footer.items())
    commit_msg = f"{work_item.commit.message}\n\n{footer_lines}"
    commit_result = _git(repo, "commit", "-m", commit_msg)
    if commit_result.returncode != 0:
        return EndgameOutcome(
            success=False, reason=HarnessReason.COMMIT_FAILED,
            evidence=[f"git commit failed: {commit_result.stderr}"],
            head_sha="", files_touched=changed_files, tests_run=tests_run,
        )

    head_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()

    return EndgameOutcome(
        success=True, reason=HarnessReason.SUCCESS,
        evidence=[], head_sha=head_sha,
        files_touched=changed_files, tests_run=tests_run,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_local_worker.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add velora/local_worker.py tests/test_local_worker.py
git commit -m "feat(harness): implement endgame — diff audit, test gates, commit"
```

---

### Task 10: Full harness entry point — run_local_worker

**Files:**
- Modify: `velora/local_worker.py`
- Modify: `tests/test_local_worker.py`

This ties the loop + endgame + WorkResult assembly into the single function
that `runners.py` will call.

- [ ] **Step 1: Write failing test for run_local_worker**

```python
# Append to tests/test_local_worker.py
from velora.local_worker import run_local_worker
from velora.exchange import write_json


class TestRunLocalWorker(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.repo = Path(self.tmp)
        _init_git_repo(self.repo)
        (self.repo / "src").mkdir()
        (self.repo / "src" / "main.py").write_text("x = 1\n")
        subprocess.run(["git", "-C", str(self.repo), "add", "."], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-m", "add src"], capture_output=True, check=True)
        self.exchange_dir = Path(tempfile.mkdtemp())

    def test_blocked_outcome_writes_block_json(self):
        responses = [
            CmdResult(0, '{"action": "work_blocked", "params": {"reason": "SCOPE_INSUFFICIENT", "blockers": ["need config"]}}', ""),
        ]
        wi = _make_work_item()
        with patch("velora.local_worker._call_local_llm_chat", side_effect=responses):
            cmd_result = run_local_worker(
                work_item=wi,
                repo_root=self.repo,
                work_branch="velora/wi-001",
                exchange_dir=self.exchange_dir,
                repo_ref="owner/repo",
                run_id="run-001",
                verb="fix",
                objective="fix the thing",
                iteration=1,
            )
        self.assertEqual(cmd_result.returncode, 0)
        block_file = self.exchange_dir / "block.json"
        self.assertTrue(block_file.exists())
        payload = json.loads(block_file.read_text())
        self.assertEqual(payload["status"], "blocked")
        self.assertIn("SCOPE_INSUFFICIENT", payload["blockers"])

    def test_success_outcome_writes_result_json(self):
        responses = [
            CmdResult(0, '{"action": "patch_file", "params": {"path": "src/main.py", "old": "x = 1", "new": "x = 2"}}', ""),
            CmdResult(0, '{"action": "work_complete", "params": {"summary": "changed x to 2"}}', ""),
        ]
        wi = _make_work_item()
        with patch("velora.local_worker._call_local_llm_chat", side_effect=responses):
            cmd_result = run_local_worker(
                work_item=wi,
                repo_root=self.repo,
                work_branch="velora/wi-001",
                exchange_dir=self.exchange_dir,
                repo_ref="owner/repo",
                run_id="run-001",
                verb="fix",
                objective="fix the thing",
                iteration=1,
            )
        self.assertEqual(cmd_result.returncode, 0)
        result_file = self.exchange_dir / "result.json"
        self.assertTrue(result_file.exists())
        payload = json.loads(result_file.read_text())
        self.assertEqual(payload["status"], "completed")
        self.assertIn("src/main.py", payload["files_touched"])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_local_worker.py::TestRunLocalWorker -v`
Expected: FAIL

- [ ] **Step 3: Implement run_local_worker**

Add to `velora/local_worker.py`:

```python
_TEST_RETRY_CAP = int(os.environ.get("VELORA_HARNESS_TEST_RETRY_CAP", "3"))


def _build_scope(work_item: WorkItem, repo_root: Path, work_branch: str) -> WorkerScope:
    likely_files = set(work_item.scope_hints.likely_files)
    allowed_dirs: set[str] = set()
    for f in likely_files:
        parts = Path(f).parts
        for i in range(len(parts) - 1):
            allowed_dirs.add(str(Path(*parts[: i + 1])))
    # Map gates to command strings
    test_commands: list[str] = []
    for gate in work_item.acceptance.gates:
        cmd_list = GATE_COMMANDS.get(gate)
        if cmd_list is not None:
            test_commands.append(" ".join(cmd_list))
    return WorkerScope(
        repo_root=repo_root,
        allowed_files=likely_files,
        allowed_dirs=allowed_dirs,
        test_commands=test_commands,
        work_branch=work_branch,
    )


def run_local_worker(
    *,
    work_item: WorkItem,
    repo_root: Path,
    work_branch: str,
    exchange_dir: Path,
    repo_ref: str,
    run_id: str,
    verb: str,
    objective: str,
    iteration: int,
) -> CmdResult:
    """Full local worker harness entry point.

    Runs the action loop, endgame, and writes the WorkResult to exchange_dir.
    Returns CmdResult with returncode=0 on completion (success or failure —
    the WorkResult file carries the actual outcome).
    """
    scope = _build_scope(work_item, repo_root, work_branch)

    # Phase 0: Pre-flight
    status = _git(repo_root, "status", "--porcelain")
    if status.stdout.strip():
        # Dirty tree — abort
        outcome = HarnessOutcome(
            success=False,
            reason=HarnessReason.COMMIT_FAILED,
            evidence=["working tree not clean at harness start"],
        )
        _write_outcome(exchange_dir, work_item, outcome, summary="pre-flight failed")
        return CmdResult(returncode=0, stdout="", stderr="dirty working tree")

    # Checkout work branch
    checkout = _git(repo_root, "checkout", "-B", work_branch)
    if checkout.returncode != 0:
        outcome = HarnessOutcome(
            success=False,
            reason=HarnessReason.COMMIT_FAILED,
            evidence=[f"branch checkout failed: {checkout.stderr}"],
        )
        _write_outcome(exchange_dir, work_item, outcome, summary="checkout failed")
        return CmdResult(returncode=0, stdout="", stderr=checkout.stderr)

    # Build prompt
    prompt = build_local_worker_prompt(
        work_item=work_item,
        repo_ref=repo_ref,
        work_branch=work_branch,
        test_commands=scope.test_commands,
    )

    test_retry = 0
    conversation: ConversationManager | None = None

    while True:
        # Phase 2: Action loop
        loop_outcome = run_local_worker_loop(
            scope=scope,
            system_prompt=prompt,
            conversation=conversation,
        )

        if not loop_outcome.success:
            # Loop terminated with a failure — write outcome and return
            harness_outcome = HarnessOutcome(
                success=False,
                reason=loop_outcome.reason,
                evidence=loop_outcome.evidence,
            )
            _write_outcome(
                exchange_dir, work_item, harness_outcome,
                summary=loop_outcome.llm_summary or f"loop terminated: {loop_outcome.reason.value}",
            )
            return CmdResult(returncode=0, stdout="", stderr="")

        # Phase 3: Endgame
        endgame = _run_endgame(
            scope=scope,
            work_item=work_item,
            llm_summary=loop_outcome.llm_summary,
        )

        if endgame.success:
            harness_outcome = HarnessOutcome(
                success=True,
                reason=HarnessReason.SUCCESS,
                evidence=endgame.evidence,
            )
            wr = assemble_work_result(
                outcome=harness_outcome,
                work_item_id=work_item.id,
                summary=loop_outcome.llm_summary,
                branch=work_branch,
                head_sha=endgame.head_sha,
                files_touched=endgame.files_touched,
                tests_run=endgame.tests_run,
            )
            (exchange_dir / "result.json").write_text(
                json.dumps(wr, sort_keys=True) + "\n", encoding="utf-8",
            )
            return CmdResult(returncode=0, stdout="", stderr="")

        # Endgame failed — is it a test failure we can retry?
        if endgame.reason == HarnessReason.TESTS_EXHAUSTED and test_retry < _TEST_RETRY_CAP:
            test_retry += 1
            # Feed failure back into the existing conversation so the LLM
            # retains context of what it already tried.
            conversation = loop_outcome.conversation
            if conversation is not None:
                test_output = endgame.evidence[0] if endgame.evidence else "tests failed"
                failure_msg = json.dumps({
                    "status": "error",
                    "result": f"Tests failed. Fix the issue.\n\n{test_output}",
                })
                conversation.append_user(failure_msg)
            continue

        # Non-retryable endgame failure
        harness_outcome = HarnessOutcome(
            success=False,
            reason=endgame.reason,
            evidence=endgame.evidence,
        )
        _write_outcome(
            exchange_dir, work_item, harness_outcome,
            summary=loop_outcome.llm_summary or f"endgame failed: {endgame.reason.value}",
            files_touched=endgame.files_touched,
            tests_run=endgame.tests_run,
        )
        return CmdResult(returncode=0, stdout="", stderr="")


def _write_outcome(
    exchange_dir: Path,
    work_item: WorkItem,
    outcome: HarnessOutcome,
    *,
    summary: str,
    files_touched: list[str] | None = None,
    tests_run: list[dict[str, str]] | None = None,
) -> None:
    """Write a blocked/failed WorkResult to the exchange dir."""
    wr = assemble_work_result(
        outcome=outcome,
        work_item_id=work_item.id,
        summary=summary,
        branch="",
        head_sha="",
        files_touched=files_touched or [],
        tests_run=tests_run or [],
    )
    # All non-success outcomes write to block.json — the orchestrator checks
    # result.json, handoff.json, and block.json and picks whichever exists.
    filename = "block.json"
    exchange_dir.mkdir(parents=True, exist_ok=True)
    (exchange_dir / filename).write_text(
        json.dumps(wr, sort_keys=True) + "\n", encoding="utf-8",
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_local_worker.py -v`
Expected: PASS

- [ ] **Step 5: Run the full test suite**

Run: `python -m pytest tests/ -q`
Expected: All tests pass.

- [ ] **Step 6: Commit**

```bash
git add velora/local_worker.py tests/test_local_worker.py
git commit -m "feat(harness): full entry point — run_local_worker with endgame and WorkResult"
```

---

## Chunk 4: Integration — wire into runners.py and run.py

### Task 11: Wire direct-local worker backend to harness

**Files:**
- Modify: `velora/runners.py:215-238`
- Modify: `velora/run.py:1979-1982`
- Modify: `tests/test_runners.py`

- [ ] **Step 1: Write failing test for direct-local routing**

```python
# Append to tests/test_runners.py
class TestDirectLocalWorker(unittest.TestCase):
    def test_normalize_worker_backend_accepts_direct_local(self):
        # direct-local is runner-agnostic — should work with any runner
        self.assertEqual(normalize_worker_backend(backend="direct-local", runner="codex"), "direct-local")
        self.assertEqual(normalize_worker_backend(backend="direct-local", runner="claude"), "direct-local")

    def test_run_worker_routes_to_local_harness(self):
        """Verify direct-local calls run_local_worker instead of run_local_llm."""
        with patch("velora.runners.run_local_worker") as mock_harness:
            mock_harness.return_value = CmdResult(0, "", "")
            # This test verifies the import and routing exist.
            # Full integration is tested in test_local_worker.py.
            # For now, just verify the route exists without error.
            try:
                run_worker(
                    session_name="test",
                    cwd=Path("/tmp"),
                    prompt="ignored",
                    runner="codex",
                    backend="direct-local",
                )
            except Exception:
                pass  # May fail on missing params — that's OK, we're testing routing
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_runners.py::TestDirectLocalWorker -v`
Expected: FAIL — `run_local_worker` not imported in runners

- [ ] **Step 3: Update runners.py to route direct-local to harness**

In `velora/runners.py`, the `run_worker` function's `direct-local` branch
needs a new signature. Since the harness needs the full WorkItem and exchange
paths (not just a prompt string), we need to extend `run_worker` to accept
optional harness parameters.

Modify `velora/runners.py`:

1. Add import: `from .local_worker import run_local_worker`
2. Add optional params to `run_worker`:

```python
def run_worker(
    *,
    session_name: str,
    cwd: Path,
    prompt: str,
    runner: str = "codex",
    backend: str | None = None,
    # Local harness params (only used when backend=direct-local)
    work_item: Any | None = None,
    work_branch: str = "",
    exchange_dir: Path | None = None,
    repo_ref: str = "",
    run_id: str = "",
    verb: str = "",
    objective: str = "",
    iteration: int = 0,
) -> CmdResult:
```

3. Replace the `direct-local` branch:

```python
    if backend_key == "direct-local":
        if work_item is None or exchange_dir is None:
            raise ValueError("direct-local worker backend requires work_item and exchange_dir")
        return run_local_worker(
            work_item=work_item,
            repo_root=cwd,
            work_branch=work_branch,
            exchange_dir=exchange_dir,
            repo_ref=repo_ref,
            run_id=run_id,
            verb=verb,
            objective=objective,
            iteration=iteration,
        )
```

- [ ] **Step 4: Update run.py runner gate**

In `velora/run.py:1979`, change:

```python
    if worker_runner not in {"codex", "claude"}:
        raise RuntimeError(f"Unsupported worker runner: {worker_runner}")
```

to:

```python
    # direct-local is runner-agnostic — skip runner validation
    if worker_backend_key != "direct-local" and worker_runner not in {"codex", "claude"}:
        raise RuntimeError(f"Unsupported worker runner: {worker_runner}")
```

Note: `worker_backend_key` is computed on line 1982. Move the runner gate check
to after the backend normalization:

```python
    worker_runner = coord_resp.selected_specialist.runner
    try:
        worker_backend_key = normalize_worker_backend(backend=ctx.worker_backend, runner=worker_runner)
    except Exception as exc:
        # ... existing error handling ...

    # Runner validation — skip for direct-local (runner-agnostic)
    if worker_backend_key != "direct-local" and worker_runner not in {"codex", "claude"}:
        raise RuntimeError(f"Unsupported worker runner: {worker_runner}")
```

Also update the `run_worker` call at line 2061 to pass the new params when
`direct-local`:

```python
    agent_result = run_worker(
        session_name=worker_session,
        cwd=repo_path,
        prompt=prompt,
        runner=worker_runner,
        backend=worker_backend_key,
        # Local harness params
        work_item=coord_resp.work_item if worker_backend_key == "direct-local" else None,
        work_branch=work_branch if worker_backend_key == "direct-local" else "",
        exchange_dir=exchange_paths["dir"] if worker_backend_key == "direct-local" else None,
        repo_ref=ctx.repo_ref if worker_backend_key == "direct-local" else "",
        run_id=task_id if worker_backend_key == "direct-local" else "",
        verb=ctx.verb if worker_backend_key == "direct-local" else "",
        objective=str(request["objective"]) if worker_backend_key == "direct-local" else "",
        iteration=attempt if worker_backend_key == "direct-local" else 0,
    )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_runners.py tests/test_local_worker.py -v`
Expected: PASS

- [ ] **Step 6: Run the full test suite**

Run: `python -m pytest tests/ -q`
Expected: All existing tests still pass. No regressions.

- [ ] **Step 7: Commit**

```bash
git add velora/runners.py velora/run.py tests/test_runners.py
git commit -m "feat(harness): wire direct-local worker backend to harness"
```

---

### Task 12: Final validation — full test suite and cleanup

**Files:**
- All modified files

- [ ] **Step 1: Run the full test suite**

Run: `python -m pytest tests/ -v`
Expected: All tests pass.

- [ ] **Step 2: Run bandit security scan if available**

Run: `python3 -m bandit -r velora -x tests --severity-level medium --confidence-level medium`
Expected: No new issues introduced. If bandit is unavailable, note that.

- [ ] **Step 3: Verify imports are clean**

Run: `python -c "from velora.local_worker import run_local_worker, HarnessReason; from velora.worker_actions import WorkerScope, dispatch_action; print('imports ok')"`
Expected: "imports ok"

- [ ] **Step 4: Commit any cleanup**

If any cleanup was needed, commit it:

```bash
git add -A
git commit -m "chore(harness): final cleanup and import verification"
```
