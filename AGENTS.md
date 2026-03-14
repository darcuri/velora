# AGENTS.md

This file is for coding agents working directly inside the `velora` repo (Codex, Claude Code, or similar local sessions).

## What Velora is

Velora is a Python CLI orchestrator for repo automation.

Core loop:
- take a task spec
- run a coordinator / worker flow
- create a PR
- gate on CI and review
- loop or finalize

Two modes exist:
- **Legacy**: direct worker prompt + FIRE retry loop
- **Mode A**: coordinator → WorkItem → worker → CI/review → repeat

## Current working stance

Velora should stay:
- **small**
- **conservative**
- **boring**
- **strict at protocol boundaries**

Do not add cleverness unless it clearly reduces toil without weakening safety.

## Current state (as of 2026-03-14)

Recently landed on `main`:
- **PR #66** — optional post-success review stage for Mode A
- **PR #67** — `velora audit inspect --json`
- **PR #69** — bounded coordinator schema-retry mode
- **PR #70** — blocked worker outcome sanitization (`branch` / `head_sha` empty for blocked outcomes)

Explicitly closed as superseded:
- **PR #68** — one-off `commit.font -> footer` alias fix

What this means right now:
- review-enabled second-turn finalize flow is proven in real runs
- coordinator schema mistakes get one strict repair retry instead of immediate run death
- blocked repair / no-op blocked paths are now protocol-valid
- audit inspection has both human-readable and JSON output

## First files to read before changing anything

If you are touching orchestration logic:
- `README.md`
- `CONTRIBUTING.md`
- `docs/mode-a-safety-rails.md`
- `docs/cli.md`
- `docs/config.md`
- `velora/run.py`
- `tests/test_mode_a_work_result_integration.py`

If you are continuing active work, also read:
- `docs/plans/current-state.md`
- `docs/plans/next-tasks.md`

## Local defaults for dogfooding

These are the local pilot defaults we have been using:

```bash
export VELORA_ALLOWED_OWNERS=darcuri
export VELORA_COORDINATOR_BACKEND=direct-claude
export VELORA_WORKER_BACKEND=direct-codex
export VELORA_MODE_A_REVIEW_ENABLED=true
```

Auth expected in local shells:
- `OPENAI_API_KEY` for Codex-backed paths
- `ANTHROPIC_AUTH_TOKEN` or `ANTHROPIC_API_KEY` for Claude-backed paths
- `GEMINI_API_KEY` for review
- `gh auth status` should be healthy

## Fast commands

Baseline tests:

```bash
python -m unittest discover -s tests -p 'test_*.py'
```

Useful local checks when available:

```bash
PYTHONPATH=. pytest -q
python3 -m bandit -r velora -x tests --severity-level medium --confidence-level medium
```

Bandit may be unavailable in some restricted local environments. Do not invent success; say it was not run.

Useful runtime commands:

```bash
velora status
velora audit inspect
velora audit inspect --json
```

Typical local dogfood run:

```bash
cat > spec.json <<'JSON'
{
  "task": "Describe the task here"
}
JSON

velora run darcuri/velora fix --spec spec.json --coordinator --debug
```

## Repo map

High-value files and directories:
- `velora/run.py` — main orchestration loop; most recent work has landed here
- `velora/cli.py` — CLI surface including `audit inspect`
- `velora/protocol.py` — protocol validation
- `velora/audit.py` — run-scoped audit artifacts
- `tests/test_mode_a_work_result_integration.py` — most important integration-style safety net
- `tests/test_cli_smoke.py` / `tests/test_cli_args.py` — CLI behavior coverage
- `docs/` — user-facing docs
- `docs/plans/` — current working state + near-term roadmap

## Working rules

When making changes:
- prefer **small, bounded diffs**
- keep protocol validation **strict** unless there is a strong reason not to
- prefer **bounded retries** over fuzzy parsing or silent recovery
- preserve the split of responsibilities:
  - coordinator decides
  - worker implements
  - orchestrator owns PR / CI / review / task state
- add or update tests in the same change
- update docs when the user-facing surface changes

Avoid:
- broad parser fuzziness
- hidden state
- “magic” recovery that makes failures harder to reason about
- sprawling new architecture when a narrow fix will do

## Current priorities

See `docs/plans/next-tasks.md`, but the short version is:
1. add observability for schema-retry firing
2. make post-success review less flaky / less prose-dependent
3. keep dogfooding medium-scope tasks that force genuine multi-turn behavior

## Output expectations for agent work

A good change should usually leave behind:
- code
- tests
- a short explanation of what changed and why
- any caveats (especially skipped checks or environment limitations)

Be explicit about:
- what was actually run
- what passed
- what could not be run locally

If a change is only a tactical bandage, say so plainly.
