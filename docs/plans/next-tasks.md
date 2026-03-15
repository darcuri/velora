# Next Tasks

_Last updated: 2026-03-14_

This is the near-term roadmap after the PR #66 / #67 / #69 / #70 cleanup pass.

## 1) Add schema-retry observability

### Why
The bounded coordinator schema-retry path exists, but it is too quiet.
When it fires, humans should be able to tell:
- what failed validation
- that a retry happened
- whether the retry succeeded or failed

### Goal
Add first-class visibility for schema-retry behavior in audit/debug artifacts.

### Good shape
- explicit audit/debug event for initial schema validation failure
- explicit event for retry attempt
- explicit event for retry success/failure
- enough detail to debug without leaking secrets or giant payloads
- `velora audit inspect` or future tooling can surface that cleanly

### Anti-goals
- no giant raw prompt dumps
- no fuzzy parsing fallback
- no weakening of validation rules

## 2) ~~Make post-success review less flaky~~ (addressed)

Replaced by the structured review protocol. Review is now protocol-driven via typed objects (`ReviewBrief`, `ReviewResult`, `ReviewFinding`, `FindingDismissal`) with verdict/findings coherence enforced at parse time. The coordinator owns review decisions (`request_review`, `dismiss_finding`) instead of the orchestrator interpreting prose.

## 2a) Tune reviewer prompt for structured JSON output quality

### Why
The reviewer backend now needs to produce structured JSON (`ReviewResult` with typed findings). Prompt quality directly affects false-positive and malformed-response rates.

### Goal
Iterate on the reviewer prompt so that structured output is reliable across common review scenarios.

### Good directions
- test the prompt against representative diffs (clean code, obvious bugs, style-only nits)
- measure parse success rate and false-positive rate
- tighten the prompt to reduce hallucinated findings

### Anti-goals
- do not build a prompt-tuning framework
- do not weaken protocol validation to compensate for bad prompts

## 2b) Add reviewer role to specialist matrix configuration

### Why
The reviewer backend is currently implicit. It should be a first-class entry in the specialist matrix so that model selection and configuration are consistent with other roles.

### Goal
Add a `reviewer` role alongside `coordinator` and `worker` in the specialist matrix config.

## 2c) Dogfood the structured review protocol on a real task

### Why
The protocol objects and state machine wiring exist, but the full structured review path has not been exercised end-to-end in a real dogfood run.

### Goal
Run a medium-scope task with the structured review protocol active and verify the full flow: `request_review` -> reviewer produces `ReviewResult` -> coordinator acts on findings.

## 2d) Wire run_structured_review into _state_dispatching_review

### Why
The `DISPATCHING_REVIEW` state handler currently bridges to the legacy review path. `run_structured_review` exists as a dispatcher but is not yet called from the state machine.

### Goal
Replace the legacy bridge in `_state_dispatching_review` with a call to `run_structured_review`, completing the structured review wiring.

### Anti-goals
- do not remove the legacy review path until the structured path is proven in dogfood

## 3) Add deterministic proof for the schema-retry path

### Why
PR #69 is merged, but production dogfood is still opportunistic.
If the coordinator behaves, the retry path never gets exercised.

### Goal
Create a deterministic way to prove the retry path works end-to-end.

### Good options
- a narrow fault-injection hook that forces the first coordinator response to be schema-invalid
- a dedicated integration test path that simulates invalid-first / valid-second coordinator output

### Anti-goals
- no user-facing “chaos mode” feature
- no permanent complexity that only serves testing

## 4) Run another medium-scope dogfood

### Why
The loop is healthier now. It should be stressed again on a task that naturally deserves multiple turns.

### Goal
Pick a bounded but real task likely to force 2–3 coordinator decisions.

### Good task characteristics
- touches more than one file
- likely to need tests + docs or orchestration + CLI coordination
- not so trivial that it auto-one-shots
- not so broad that failure teaches nothing

## 5) Named failure modes in coordinator and worker prompts

### Why
Agents drift. Prompt-level instructions like "be careful" are vague. Named failure modes give the coordinator and worker a self-audit vocabulary — specific anti-patterns they can check themselves against. They're also grepable in debug logs and discussable in post-mortems.

Inspired by Overstory's agent definitions, which list explicit named anti-patterns with corrective actions.

### Goal
Add named failure modes to coordinator and worker prompts as explicit checklist items.

### Coordinator failure modes
- `SCOPE_EXPLOSION` — decomposing into too many work items when one would do
- `INFERENCE_CHAIN` — more than 2 unsupported assumptions in a single decision
- `PLAN_WITHOUT_EVIDENCE` — choosing a strategy without referencing evaluation/history state
- `PREMATURE_DONE` — emitting `finalize_success` before all acceptance criteria are verified
- `SILENT_ESCALATION_DROP` — receiving a blocker or review finding and not acting on it

### Worker failure modes
- `SCOPE_CREEP` — implementing beyond what the WorkItem instructions specify
- `OBJECTIVE_REWRITE` — reinterpreting the task objective
- `SILENT_STALL` — encountering a problem and not reporting it in blockers
- `PREMATURE_DONE` — reporting status=completed without running tests
- `UNTESTED_CLOSE` — claiming tests pass without actually running them
- `CANONICAL_BRANCH_WRITE` — committing to main/develop instead of the work branch

### Good shape
- each failure mode has a name, definition, and what to do instead
- modes appear in the agent prompt as a self-check section
- grepable in debug/audit output

### Anti-goals
- not a scoring system or grading rubric
- not an exhaustive taxonomy — start small, add as real failures are observed

## 6) Propulsion principle in agent prompts

### Why
Agents waste tokens narrating plans and asking for confirmation. One paragraph instructing "execute immediately, don't ask for permission, start working within your first tool calls" eliminates this overhead.

Inspired by Overstory's propulsion principle, hardcoded into every agent definition.

### Goal
Add to both coordinator and worker system prompts: execute immediately, no plan narration, no confirmation seeking, start working.

### Good shape
- one paragraph per prompt
- measurably fewer "I'll now..." / "Let me plan..." tokens in output

## 7) HOW/WHAT two-layer instruction separation

### Why
Currently, workflow instructions (HOW to be a worker) and task-specific context (WHAT to do) are mixed in prompt generation. Separating them means new roles only need new base definitions, and per-task customization stays in the WorkItem/ReviewBrief.

Inspired by Overstory's base `.md` definitions (HOW) + per-task overlay (WHAT) pattern.

### Goal
Extract stable coordinator/worker workflow instructions into reusable base role definitions. Task-specific context (objective, acceptance criteria, scope) stays in the work item or review brief.

### Good shape
- coordinator prompt = base coordinator definition + rendered request
- worker prompt = base worker definition + rendered WorkItem
- adding a new role (e.g., investigator) means writing one base definition, not rewriting prompt generation

### Anti-goals
- don't template-engine this into a framework
- keep it as simple files or string constants

## Future: patterns for multi-agent expansion

These are not near-term tasks. They're concepts from Overstory and the broader space that Velora should adopt when expanding beyond single-worker mode. Documented here so they're not forgotten.

### Worktree isolation
Each parallel worker gets its own git worktree. One agent per worktree. Required for parallel execution — agents cannot safely share a working tree. Design the lifecycle (create, scope, merge, cleanup) before implementing.

### File scope enforcement
Each worker gets an explicit file scope — which files it can write to. Non-overlapping assignment is the coordinator's responsibility. Enforcement is mechanical (path validation), not just prompt instruction. Critical for preventing merge conflicts.

### Typed inter-agent messaging
Define message types beyond WorkItem/WorkResult: `dispatch`, `error`, `question`, `status`, `merge_ready`. Each type carries semantic meaning the system acts on mechanically. Start simple (structured JSON in files), consider SQLite WAL if agents need concurrent access.

### Tiered merge conflict resolution
When parallel workers produce conflicting changes: clean merge → auto-resolve (keep incoming) → AI-resolve → abort and reimagine. Start with tiers 1-2. The FIFO merge queue serializes operations.

### Tiered health monitoring
Tier 0: mechanical process liveness (no AI, cheap). Tier 1: AI-assisted failure triage (triggered by Tier 0). Tier 2: persistent monitor agent. Cheapest tier runs always; AI invoked only when needed.

### Role specialization beyond implementer
- **Scout**: read-only exploration, produces specs. Cheap.
- **Builder**: implementation specialist, scoped writes. The current worker.
- **Reviewer**: independent read-only validation. Can use different model.
- **Merger**: branch integration specialist.

### Hierarchy enforcement
Code-enforced depth limits (not just prompt-instructed). Track agent depth, reject spawns that exceed limit. Prevents runaway agent multiplication.

### Institutional learning (expertise system)
Agents record patterns/learnings during execution with classification: foundational (stable conventions), tactical (session-specific), observational (unverified). Required as a completion gate, not optional. Per-repo persistence across runs.

## Lower-priority cleanup

These are real but less urgent:
- improve docs around the newest Mode A behavior and local dogfood conventions
- decide whether more machine-readable audit surfaces are worth adding
- continue trimming rough edges in test ergonomics / local setup

## Recommended order

Near-term (current single-worker phase):
1. schema-retry observability
2. wire run_structured_review into _state_dispatching_review (2d)
3. tune reviewer prompt for structured JSON output (2a)
4. named failure modes in coordinator/worker prompts (5)
5. propulsion principle in agent prompts (6)
6. dogfood the structured review protocol on a real task (2c)
7. HOW/WHAT instruction separation (7)
8. deterministic retry-path proof (3)
9. add reviewer role to specialist matrix (2b)
10. another medium-scope dogfood (4)
