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


# -- System prompt builder --

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


# -- Conversation manager --

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


# -- Endgame --

@dataclass
class EndgameOutcome:
    """Outcome from the endgame phase."""
    success: bool
    reason: HarnessReason
    evidence: list[str]
    head_sha: str
    files_touched: list[str]
    tests_run: list[dict[str, str]]


# Gate name -> command list
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
