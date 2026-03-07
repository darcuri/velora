# Mode A Safety Rails and Workflow Canon

This document defines the non-negotiables for Velora's coordinator/worker loop.

## Worker hard blocks

Workers may **not** do the following directly:

- run `git push`
- merge or close pull requests
- delete branches, rewrite history, or reset commits
- work outside the designated repository checkout and assigned task branch
- edit Velora task records or orchestrator-owned state artifacts

These are orchestrator responsibilities or human-review boundaries, not worker actions.

## Workflow canon

### Where work happens

Worker code changes happen only on the designated task branch inside the checked-out target repository.

### Where task truth lives

The source of truth for task state is Velora's orchestrator-owned task artifacts:

- `~/.velora/tasks.json`
- `~/.velora/tasks/<task_id>/`

Workers do not own these records and must not mutate them.

### Who owns review and state transitions

Velora orchestrator owns:

- PR creation
- CI polling and interpretation
- automated review execution and comment posting
- final task status transitions (`ready`, `failed`, `not-ready`, etc.)

Workers own only the bounded implementation step described by the current `WorkItem`.

### What marks a task as ready

A task is considered ready only when the task record is marked `ready` with the PR URL and head SHA recorded after review resolution.

That artifact matters more than conversational claims like “done” or “looks good.”

## Mild testing doctrine

Velora prefers:

- real project gates and meaningful checks over decorative or no-op tests
- honest `not_run` reporting when a meaningful test cannot be executed
- explicit evidence of what was actually run

Velora discourages fake confidence:

- do not claim tests passed unless they actually ran
- do not use low-value mocked tests purely to satisfy optics when a better real check is feasible

This is a preference for honesty and signal quality, not a ban on all mocks.
