from __future__ import annotations

"""Coordinator execution (control-plane) for Mode A.

This module is intentionally small:
- Render a strict coordinator prompt from a CoordinatorRequest JSON object.
- Execute the coordinator via the selected backend (direct or ACP-backed).
- Parse strict JSON output.
- Validate it against protocol v1.

Any protocol violation is a hard failure (no remaps).
"""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .acpx import CmdResult, run_claude, run_codex
from .protocol import CoordinatorResponse, ProtocolError, enforce_specialist_matrix, validate_coordinator_response


COORDINATOR_PROMPT_TEMPLATE_V1 = """You are Velora Coordinator, the control-plane orchestrator for an autonomous engineering loop.

### Operating mode
- Mode A only: single branch, sequential work. Exactly one WorkItem per iteration.
- You do not run commands, edit code, or browse files directly. You decide what to do next and delegate to a specialist worker.
- You must follow the provided policy and required gates.
- Prefer the smallest change that makes measurable progress.
- Do not request or reveal secrets. If auth is missing, stop with a clear message.

### Input
You will be given a single JSON object called CoordinatorRequest.
Treat it as the authoritative state of the run. Do not assume additional context.
{specialist_matrix_section}{replay_section}
CoordinatorRequest:
{request_json}

### Output (STRICT)
Return ONLY a single JSON object. No markdown. No prose outside JSON.

The JSON MUST conform to this CoordinatorResponse schema (protocol_version=1):

{{
  "protocol_version": 1,
  "decision": "execute_work_item" | "finalize_success" | "stop_failure",
  "reason": "string",

  "selected_specialist": {{
    "role": "implementer" | "docs" | "refactor" | "investigator",
    "runner": "codex" | "claude",
    "model": "string (optional)"
  }},

  "work_item": {{
    "id": "WI-####",
    "kind": "implement" | "repair" | "refactor" | "docs" | "test_only" | "investigate",
    "rationale": "string",
    "instructions": ["string", "..."],
    "scope_hints": {{"likely_files": ["..."], "search_terms": ["..."]}},
    "acceptance": {{
      "must": ["..."],
      "must_not": ["..."],
      "gates": ["tests" | "lint" | "security" | "ci" | "docs", "..."]
    }},
    "limits": {{"max_diff_lines": 50|100|200|400, "max_commits": 1}},
    "commit": {{
      "message": "string",
      "footer": {{
        "VELORA_RUN_ID": "string",
        "VELORA_ITERATION": 1,
        "WORK_ITEM_ID": "WI-####"
      }}
    }}
  }}
}}

Rules:
- selected_specialist is REQUIRED for ALL decisions (attribution)
- You MUST choose selected_specialist within CoordinatorRequest.policy.specialist_matrix (out-of-bounds is a hard failure)
- work_item is REQUIRED only when decision=execute_work_item; it must be omitted otherwise
- reason MUST be a string
- Unknown keys are forbidden
- `work_item.limits.max_diff_lines` MUST be EXACTLY one of: 50, 100, 200, 400
- NEVER invent intermediate `max_diff_lines` values like 75, 150, 250, or 300
- `work_item.limits.max_commits` MUST be exactly 1
- Before you answer, silently verify that every enum/limit value exactly matches the allowed schema; do not output the verification step
"""


@dataclass(frozen=True)
class CoordinatorRunResult:
    response: CoordinatorResponse
    cmd: CmdResult


def _render_specialist_matrix_section(request: dict[str, Any]) -> str:
    policy = request.get("policy") if isinstance(request, dict) else None
    matrix = policy.get("specialist_matrix") if isinstance(policy, dict) else None
    if not isinstance(matrix, dict) or not matrix:
        return ""

    lines = [
        "\n### Allowed specialist matrix for this run",
        "These role/runner pairings are authoritative. You MUST stay within them.",
        "Choosing any other runner for a role is a hard failure.",
        "",
    ]
    for role in sorted(matrix):
        runners = matrix.get(role)
        if isinstance(runners, list) and runners:
            allowed = ", ".join(str(x) for x in runners)
            lines.append(f"- {role}: {allowed}")
    lines.append("")
    return "\n".join(lines)


def render_coordinator_prompt_v1(request: dict[str, Any], *, replay_memory: str | None = None) -> str:
    request_json = json.dumps(request, indent=2, sort_keys=True)
    specialist_matrix_section = _render_specialist_matrix_section(request)
    replay_text = (replay_memory or "").strip()
    replay_section = ""
    if replay_text:
        replay_section = (
            "\n### Replay context\n"
            "The following replay summary is provided only to help continuity between coordinator turns.\n"
            "It may omit details and is not authoritative.\n"
            "If anything here conflicts with CoordinatorRequest, trust CoordinatorRequest.\n\n"
            f"{replay_text}\n\n"
        )
    return COORDINATOR_PROMPT_TEMPLATE_V1.format(
        request_json=request_json,
        specialist_matrix_section=specialist_matrix_section,
        replay_section=replay_section,
    )


def _parse_strict_json_object(text: str) -> dict[str, Any]:
    """Parse a strict JSON object.

    Coordinator output is required to be JSON-only. If the model emits any extra
    characters beyond surrounding whitespace, treat it as a protocol violation.
    """

    raw = text.strip()
    if not raw:
        raise ProtocolError("Coordinator output was empty")
    if raw.startswith("```"):
        # Tolerate a single outer code-fence wrapper (common model failure mode).
        # We still require the *inner* content to be a single JSON object.
        lines = raw.splitlines()
        if len(lines) >= 3 and lines[0].startswith("```") and lines[-1].strip() == "```":
            raw = "\n".join(lines[1:-1]).strip()

    if raw.startswith("```") or raw.endswith("```"):
        raise ProtocolError("Coordinator output must not be wrapped in markdown fences")
    if not (raw.startswith("{") and raw.endswith("}")):
        raise ProtocolError("Coordinator output must be a single JSON object")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ProtocolError(f"Coordinator output was not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ProtocolError("Coordinator output JSON must be an object")
    return payload


def validate_coordinator_cmd_result(*, result: CmdResult, request: dict[str, Any]) -> CoordinatorRunResult:
    if result.returncode != 0:
        msg = (result.stderr or result.stdout).strip() or "unknown error"
        raise RuntimeError(f"Coordinator runner failed: {msg}")

    try:
        payload = _parse_strict_json_object(result.stdout)
        resp = validate_coordinator_response(payload)
        # Hard-fail if coordinator selected an out-of-policy specialist/runner/model.
        policy = request.get("policy") if isinstance(request, dict) else None
        matrix = policy.get("specialist_matrix") if isinstance(policy, dict) else None
        enforce_specialist_matrix(resp, matrix)
        return CoordinatorRunResult(response=resp, cmd=result)
    except ProtocolError as exc:
        excerpt = (result.stdout or "").strip().replace("\n", " ")[:500]
        raise ProtocolError(f"{exc} | coordinator_output_excerpt={excerpt!r}") from exc


def run_coordinator_v1_with_cmd(
    *,
    session_name: str,
    cwd: Path,
    request: dict[str, Any],
    runner: str = "claude",
) -> CoordinatorRunResult:
    """Run the coordinator and return response + raw command result.

    The raw CmdResult contains best-effort usage metadata (tokens/context usage)
    from acpx, which Mode A uses for token budgeting.
    """

    prompt = render_coordinator_prompt_v1(request)

    runner_key = (runner or "claude").strip().lower()
    if runner_key not in {"claude", "codex"}:
        raise ValueError("coordinator runner must be one of: claude, codex")

    result: CmdResult = (
        run_claude(session_name=session_name, cwd=cwd, prompt=prompt)
        if runner_key == "claude"
        else run_codex(session_name=session_name, cwd=cwd, prompt=prompt)
    )

    return validate_coordinator_cmd_result(result=result, request=request)


def run_coordinator_v1(
    *,
    session_name: str,
    cwd: Path,
    request: dict[str, Any],
    runner: str = "claude",
) -> CoordinatorResponse:
    """Run the coordinator model and return a validated CoordinatorResponse."""

    return run_coordinator_v1_with_cmd(
        session_name=session_name,
        cwd=cwd,
        request=request,
        runner=runner,
    ).response
