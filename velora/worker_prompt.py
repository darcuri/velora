from __future__ import annotations

"""Build worker prompts from WorkItems (protocol v1).

This is the translation layer from coordinator intent (WorkItem) to an implementer
prompt that can be executed by Codex/Claude.

We keep this deterministic and boring.
"""

from .protocol import WorkItem


def build_worker_prompt_v1(
    *,
    repo_ref: str,
    verb: str,
    objective: str,
    run_id: str,
    iteration: int,
    work_branch: str,
    work_item: WorkItem,
) -> str:
    lines: list[str] = []
    lines.append(f"You are working on {repo_ref}.")
    lines.append(f"Run ID: {run_id}")
    lines.append(f"Verb: {verb}")
    lines.append(f"Objective: {objective}")
    lines.append(f"Iteration: {iteration}")
    lines.append(f"WorkItem: {work_item.id} ({work_item.kind})")
    lines.append("")

    lines.append("Requirements:")
    lines.append(f"- Checkout branch {work_branch} (create it if it does not exist)")
    lines.append("- Implement exactly this WorkItem (bounded scope; do not roam)")
    if work_item.acceptance.gates:
        lines.append(f"- Ensure gates pass: {', '.join(work_item.acceptance.gates)}")
    lines.append(f"- Keep diff <= ~{work_item.limits.max_diff_lines} lines")
    lines.append("- Do not introduce new dependencies")
    lines.append("")

    lines.append("WorkItem rationale:")
    lines.append(work_item.rationale)
    lines.append("")

    lines.append("Instructions:")
    for ins in work_item.instructions:
        lines.append(f"- {ins}")
    lines.append("")

    if work_item.scope_hints.likely_files or work_item.scope_hints.search_terms:
        lines.append("Scope hints:")
        if work_item.scope_hints.likely_files:
            lines.append("- Likely files: " + ", ".join(work_item.scope_hints.likely_files))
        if work_item.scope_hints.search_terms:
            lines.append("- Search terms: " + ", ".join(work_item.scope_hints.search_terms))
        lines.append("")

    lines.append("Acceptance criteria (must):")
    for item in work_item.acceptance.must:
        lines.append(f"- {item}")
    lines.append("")

    if work_item.acceptance.must_not:
        lines.append("Acceptance criteria (must NOT):")
        for item in work_item.acceptance.must_not:
            lines.append(f"- {item}")
        lines.append("")

    lines.append("Commit requirements:")
    lines.append(f"- Commit message subject: {work_item.commit.message}")
    lines.append("- Include this footer in the commit message (exact keys):")
    for k in ("VELORA_RUN_ID", "VELORA_ITERATION", "WORK_ITEM_ID"):
        lines.append(f"  {k}: {work_item.commit.footer[k]}")
    lines.append("")

    lines.append("After pushing, print this machine-readable footer exactly:")
    lines.append("BRANCH: <branch-name>")
    lines.append("HEAD_SHA: <commit-sha>")
    lines.append("SUMMARY: <one-line summary>")

    return "\n".join(lines) + "\n"
