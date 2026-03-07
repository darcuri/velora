from __future__ import annotations

"""Canonical policy/rail definitions for Velora's Mode A workflow.

Keep these boring, explicit, and reusable across prompts/docs/tests.
"""

WORKER_HARD_BLOCKS: tuple[str, ...] = (
    "Do not run git push.",
    "Do not merge or close pull requests.",
    "Do not delete branches, rewrite history, or reset commits.",
    "Do not work outside the designated repository checkout and assigned task branch.",
    "Do not edit Velora task records or orchestrator-owned state artifacts.",
)

WORKFLOW_CANON: tuple[str, ...] = (
    "Work happens only on the designated task branch inside the checked-out target repository.",
    "Velora task truth lives in orchestrator-owned task artifacts under ~/.velora/tasks.json and ~/.velora/tasks/<task_id>/.",
    "Velora orchestrator owns PR creation, CI polling, automated review, and final status transitions.",
    "A task is only ready when the task record is marked ready with PR URL and head SHA after review resolution.",
)

TESTING_DOCTRINE: tuple[str, ...] = (
    "Prefer real project gates and meaningful checks over decorative or no-op tests.",
    "If a meaningful test cannot be run, report not_run with the real reason instead of inventing coverage.",
    "Do not claim tests passed unless you actually ran them.",
)
