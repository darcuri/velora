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
