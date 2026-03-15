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

## Lower-priority cleanup

These are real but less urgent:
- improve docs around the newest Mode A behavior and local dogfood conventions
- decide whether more machine-readable audit surfaces are worth adding
- continue trimming rough edges in test ergonomics / local setup

## Recommended order

If choosing only the next few tasks, do them in this order:
1. schema-retry observability
2. wire run_structured_review into _state_dispatching_review (2d)
3. tune reviewer prompt for structured JSON output (2a)
4. dogfood the structured review protocol on a real task (2c)
5. deterministic retry-path proof
6. add reviewer role to specialist matrix (2b)
7. another medium-scope dogfood
