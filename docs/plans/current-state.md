# Current State

_Last updated: 2026-03-14_

## Snapshot

Velora is in a much healthier state than it was at the start of the 2026-03-14 dogfood session.

Main things proven / landed:
- **Mode A post-success review stage** is on `main` and has been exercised in real runs.
- **Coordinator schema resilience** now exists via a bounded one-shot retry path.
- **Blocked worker outcomes** are sanitized so blocked / no-op paths do not fail protocol validation on stale refs.
- **Audit inspection** now has a JSON output mode for machine-readable summaries.

## What landed today

### PR #66 — post-success review stage
Merged.

Added:
- optional `mode_a_review_enabled` / `VELORA_MODE_A_REVIEW_ENABLED`
- structured `ReviewResult`
- review audit events
- review handoff back into coordinator evaluation
- audit inspect rendering for review events

Meaning:
- successful CI + review can now feed a second coordinator decision instead of ending immediately

### PR #67 — `velora audit inspect --json`
Merged.

Added:
- machine-readable JSON summary output for audit inspect
- optional `review_events` in JSON when present
- CLI + smoke tests for both text and JSON modes

### PR #69 — bounded coordinator schema retry
Merged.

Added:
- one strict retry when coordinator output fails schema validation
- retry request includes the exact validation error
- second failure still hard-fails

Meaning:
- simple coordinator schema mistakes no longer immediately kill the run
- schema remains strict; this is not fuzzy parsing

### PR #70 — blocked worker outcome sanitization
Merged.

Added:
- blocked worker outcomes force `branch` / `head_sha` to empty strings before validation
- regression tests for repair-blocked and no-op blocked paths

Meaning:
- the blocked-result protocol bug exposed during dogfooding is fixed on `main`

## What was intentionally not kept

### PR #68 — one-off `commit.font -> footer` alias
Closed as superseded.

Reason:
- the bounded schema-retry path from PR #69 is the more principled fix
- we did not want to accumulate one-off typo aliases as the primary strategy

## What real dogfood proved

During the 2026-03-14 session, real runs showed:
- a second coordinator turn can happen after review and successfully finalize
- blocked-result protocol issues were real and reproducible before the fix
- schema typos in coordinator output were also real and reproducible before the retry fix
- after the fixes, the previously failing blocked-result task completed cleanly on current `main`

## Remaining gaps

Important gaps still open:
- **schema-retry visibility is weak** — retry behavior is not surfaced clearly enough in audit/debug artifacts
- **review stage is still too prose-shaped** — false review nits are possible and can trigger unnecessary repair loops
- **deterministic retry-path proof is still missing** — we have the feature, but not a guaranteed end-to-end fault-injection path that proves it fires when expected

## High-value files right now

If you need to work on the current hot path, start with:
- `velora/run.py`
- `velora/cli.py`
- `velora/audit.py`
- `velora/protocol.py`
- `tests/test_mode_a_work_result_integration.py`
- `tests/test_cli_smoke.py`
- `docs/mode-a-safety-rails.md`

## Local dogfood defaults

Typical local pilot configuration:

```bash
export VELORA_ALLOWED_OWNERS=darcuri
export VELORA_COORDINATOR_BACKEND=direct-claude
export VELORA_WORKER_BACKEND=direct-codex
export VELORA_MODE_A_REVIEW_ENABLED=true
```

## Bottom line

Velora is no longer in “proof of life only” territory.

It now has:
- a working review-enabled second-turn loop
- bounded coordinator schema recovery
- sane blocked-result handling
- machine-readable audit summaries

The next work should improve **visibility** and **review quality**, not re-litigate the basic loop.
