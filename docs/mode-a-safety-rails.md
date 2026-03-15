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

## Structured review protocol

Review in Mode A is protocol-driven. The coordinator owns all review decisions; the orchestrator validates and routes but never interprets review content.

### Flow

1. **Coordinator requests review** -- emits a `request_review` decision with a `ReviewBrief` that specifies the reviewer backend, objective, acceptance/rejection criteria, areas of concern, and diff scope.
2. **Orchestrator dispatches review** -- sends the `ReviewBrief` to the specified reviewer and receives a `ReviewResult` containing a verdict (`approve` or `reject`), structured findings, and a summary.
3. **Orchestrator validates** -- the `ReviewResult` must pass protocol validation, including verdict/findings coherence (e.g., `approve` is not allowed with blocker-severity findings; `reject` requires at least one blocker).
4. **Coordinator decides** -- the review result is fed back to the coordinator, which chooses one of:
   - `finalize_success` -- accept the work
   - `execute_work_item` -- issue a repair WorkItem to address findings
   - `dismiss_finding` -- explicitly dismiss specific findings with a justification
   - `stop_failure` -- abandon the task

### Protocol objects

- `ReviewBrief` -- coordinator-authored request specifying what to review and how to judge it
- `ReviewResult` -- reviewer output with verdict, findings, and summary
- `ReviewFinding` -- individual finding with severity, category, location, and description
- `FindingDismissal` -- coordinator-authored dismissal referencing specific finding IDs with a required justification

### Key invariants

- The orchestrator never decides whether findings are blocking; that is the coordinator's job.
- Finding dismissal requires explicit `finding_ids` and a non-empty `justification`.
- `ReviewResult` coherence is enforced at parse time: an `approve` verdict with blocker findings, or a `reject` verdict without blockers, is a protocol error.
- When `review_enabled=true`, the coordinator must issue at least one `request_review` before `finalize_success` is allowed.

## Mild testing doctrine

Velora prefers:

- real project gates and meaningful checks over decorative or no-op tests
- honest `not_run` reporting when a meaningful test cannot be executed
- explicit evidence of what was actually run

Velora discourages fake confidence:

- do not claim tests passed unless they actually ran
- do not use low-value mocked tests purely to satisfy optics when a better real check is feasible

This is a preference for honesty and signal quality, not a ban on all mocks.
