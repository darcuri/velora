# Structured Review Protocol and Orchestrator State Machine

_Design approved: 2026-03-14_

## Problem

The review gate is prose-dependent. Gemini outputs `OK:` or `BLOCKER:`/`NIT:` lines, regex parses them, and the orchestrator hardcodes what to do with the result. The coordinator has no control over review timing, reviewer selection, or review context. False nits from dogfooding already triggered unnecessary repair loops.

The orchestrator (`run.py`, 2600 lines) is a procedural monolith that grew from simple cases. Each new capability was bolted on as another branch in a nested conditional. Adding structured review on top of this would make it worse.

## Design Principles

- **Strict protocol boundaries constrain drift without micromanaging work.** Agents do their jobs; protocol objects validate that the output is shaped correctly.
- **The coordinator is the only LLM.** Every judgment call flows through the coordinator. The orchestrator validates and executes — it never interprets.
- **The orchestrator is a state machine, not a procedure.** Each state does one thing. New capabilities are new handlers, not new branches.
- **Review is protocol-driven end-to-end.** Structured input (ReviewBrief), structured output (ReviewResult), structured coordinator decisions (request_review, dismiss_finding).

## New Protocol Objects

### ReviewBrief

Sent by the coordinator with a `request_review` decision. Tells the reviewer what to evaluate and how.

```
ReviewBrief:
  id: str                          # "RB-0001", scoped to run
  reviewer: str                    # "gemini" | "claude"
  model: str | None                # optional model override
  objective: str                   # what the work item aimed to accomplish
  acceptance_criteria: list[str]   # what "done" means
  rejection_criteria: list[str]    # what's forbidden
  areas_of_concern: list[str]      # specific things to look harder at
  scope: ReviewScope

ReviewScope:
  kind: str                        # "full_diff" | "files"
  base_ref: str                    # diff base
  head_sha: str                    # diff head
  files: list[str]                 # restrict to these paths when kind="files"
```

### ReviewFinding

A single discrete finding from the reviewer. Independently addressable by the coordinator.

```
ReviewFinding:
  id: str                          # "RF-001", scoped to the ReviewResult
  severity: str                    # "blocker" | "nit"
  category: str                    # "correctness" | "security" | "regression" | "style" | "docs"
  location: str                    # file:line or file or "" if general
  description: str                 # what the issue is
  criterion_id: int | None         # index into acceptance_criteria this violates, or None
```

### ReviewResult

The reviewer's structured output. Replaces prose parsing.

```
ReviewResult:
  review_brief_id: str             # ties back to ReviewBrief.id
  verdict: str                     # "approve" | "reject"
  findings: list[ReviewFinding]    # may be empty (approve) or non-empty
  summary: str                     # one-line human-readable summary
```

Validation rules:
- `verdict="approve"` requires zero blocker-severity findings (nits allowed)
- `verdict="reject"` requires at least one blocker-severity finding
- Each finding must have valid severity and category from allowed enums
- `review_brief_id` must match the dispatched brief

### FindingDismissal

Sent by the coordinator with a `dismiss_finding` decision.

```
FindingDismissal:
  finding_ids: list[str]           # which RF-### findings to dismiss
  justification: str               # why the coordinator considers these non-blocking
```

## Coordinator Decision Vocabulary

Expands from three to five decisions:

| Decision | When | Required payload |
|---|---|---|
| `execute_work_item` | (unchanged) | `work_item`, `selected_specialist` |
| `request_review` | Coordinator wants a review run | `review_brief`, `selected_specialist` |
| `dismiss_finding` | Coordinator considers findings non-blocking | `finding_dismissal` |
| `finalize_success` | (unchanged) | `selected_specialist` |
| `stop_failure` | (unchanged) | `selected_specialist` |

CoordinatorResponse validation:
- Exactly one of `work_item` / `review_brief` / `finding_dismissal` present, matching the decision
- The others must be absent (not null)
- `selected_specialist` required for all decisions
- For `request_review`, specialist matrix enforced on reviewer selection via `reviewer` role

Policy enforcement:
- If `review_enabled=true`, at least one `request_review` must have occurred before `finalize_success` is accepted. Otherwise protocol error.
- If `review_enabled=false`, coordinator can finalize directly after CI.

## Orchestrator State Machine

### States

```
PREFLIGHT             → repo checkout, validation, config
                        → AWAITING_DECISION

AWAITING_DECISION     → check breakers, call coordinator, validate response
                        → DISPATCHING_WORKER     (execute_work_item)
                        → DISPATCHING_REVIEW     (request_review)
                        → PROCESSING_DISMISSAL   (dismiss_finding)
                        → TERMINAL               (finalize_success | stop_failure)

DISPATCHING_WORKER    → build prompt, run worker, validate WorkResult
                        → POLLING_CI             (if completed)
                        → AWAITING_DECISION      (if blocked/failed)

POLLING_CI            → poll, classify, retry infra outages
                        → AWAITING_DECISION      (always)

DISPATCHING_REVIEW    → build reviewer prompt from ReviewBrief, run reviewer, validate ReviewResult
                        → AWAITING_DECISION

PROCESSING_DISMISSAL  → validate FindingDismissal against active ReviewResult
                        → AWAITING_DECISION

TERMINAL              → persist final state, emit audit events
                        → done
```

### State handler responsibilities

| State | Owns | Does NOT own |
|---|---|---|
| PREFLIGHT | Repo checkout, config, branch setup | — |
| AWAITING_DECISION | Coordinator call, schema retry, protocol validation, breaker checks | Interpreting what results mean |
| DISPATCHING_WORKER | Worker prompt, execution, WorkResult validation, branch publication | Whether the result is good enough |
| POLLING_CI | CI poll loop, infra classification, infra retries | What CI failure means for the task |
| DISPATCHING_REVIEW | ReviewBrief → prompt, execution, ReviewResult validation | Whether findings are blocking |
| PROCESSING_DISMISSAL | Validation of dismissal against findings, recording justification | Whether dismissal is warranted |
| TERMINAL | State persistence, audit summary, final status | — |

### Key principle

Every judgment call goes through AWAITING_DECISION. The orchestrator never interprets outcomes. CI failed? Update evaluation, go to AWAITING_DECISION. Worker blocked? Update evaluation, go to AWAITING_DECISION. Review found blockers? Update evaluation, go to AWAITING_DECISION.

The coordinator is the only entity that decides "so what?"

### Breakers

Breakers stay in the orchestrator, checked in AWAITING_DECISION before calling the coordinator:
- Wall clock
- Token budget
- No-progress streak
- Oscillation detection

If a breaker trips, the orchestrator forces TERMINAL without consulting the coordinator.

### RunContext

Single state object carried through all transitions:

```
RunContext:
  # Identity
  task_id, run_id, repo_ref, verb

  # Repo
  owner, repo, base_branch, work_branch, repo_path

  # Config / policy
  config, max_attempts, max_tokens, max_wall_seconds, no_progress_max, review_enabled

  # Mutable state
  iteration, record, request, active_review_result

  # Infrastructure
  gh, home, task_dir, debug, loop_start
```

### Main loop

```python
state = PREFLIGHT
while state != DONE:
    state = STATE_HANDLERS[state](ctx)
```

## Codebase Impact

### Changes

- **`velora/protocol.py`** — new protocol objects, expanded CoordinatorResponse, validation functions
- **`velora/run.py`** — `run_task_mode_a` refactored into state machine with handler functions; RunContext dataclass; legacy mode untouched
- **`velora/coordinator.py`** — prompt template expanded with new decision schemas
- **`velora/acpx.py`** — reviewer dispatch refactored to accept ReviewBrief and return structured output
- **`velora/audit.py`** — new event types: REVIEW_REQUESTED, FINDING_DISMISSED
- **`velora/run_memory.py`** — new replay events for review request, result, dismissal

### Unchanged

- Legacy mode (`run_task_legacy`)
- CI polling logic (moves into handler, same behavior)
- Branch publication logic
- PR creation logic
- Worker prompt and WorkResult protocol objects
- Specialist matrix enforcement (extended to cover reviewer role)
- Fault injection mechanism
- CLI surface

## Migration Path

1. Refactor `run_task_mode_a` into state machine — existing tests must pass with no behavior change
2. Add new protocol objects with validation tests
3. Add state handler unit tests
4. Layer review protocol expansion on top of working state machine

## Not In Scope (Yet)

- Parallel worker dispatch
- Reviewer selection beyond Gemini and Claude
- `investigate` decision type
- Policy-driven review requirements (e.g., mandatory security review for auth changes)
