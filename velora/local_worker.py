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
