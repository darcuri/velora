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

## 2) Make post-success review less flaky

### Why
The review stage currently depends too much on prose and parsing.
A false nit already created a pointless repair path during dogfooding.

### Goal
Make review outcomes more structured and less hallucination-prone.

### Good directions
- structured review output shape instead of loose prose where practical
- stronger validation of review outputs before they trigger repair
- clearer distinction between:
  - approve
  - repair-worthy issue
  - malformed review response

### Anti-goals
- do not add a sprawling new review subsystem
- do not make the review path “smart” in a way that is hard to reason about

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
2. review-stage robustness
3. deterministic retry-path proof
4. another medium-scope dogfood
