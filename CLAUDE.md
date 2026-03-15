# CLAUDE.md

This file is for Claude Code or any local coding-agent session working directly inside the `velora` repo.

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

## Current state (as of 2026-03-15)

Recently landed on `main`:
- **PR #72** — structured review protocol + orchestrator state machine (the big one)
- **PR #66** — optional post-success review stage for Mode A
- **PR #67** — `velora audit inspect --json`
- **PR #69** — bounded coordinator schema-retry mode
- **PR #70** — blocked worker outcome sanitization

What this means right now:
- **Orchestrator is a state machine** — `run_task_mode_a` is 7 handler functions dispatched from a loop, not a 1200-line procedure. States: PREFLIGHT, AWAITING_DECISION, DISPATCHING_WORKER, POLLING_CI, DISPATCHING_REVIEW, PROCESSING_DISMISSAL, TERMINAL.
- **Coordinator has 5 decisions** — `execute_work_item`, `request_review`, `dismiss_finding`, `finalize_success`, `stop_failure`
- **Review is protocol-driven** — `ReviewBrief` (coordinator tells reviewer what to check), `ReviewResult` with `ReviewFinding` objects (structured output), `FindingDismissal` (coordinator justifies dismissing findings). No more prose parsing for the new path.
- **Local LLM backend exists** — `direct-local` backend calls any OpenAI-compatible endpoint (e.g., LM Studio at localhost:1234). Works for coordinator; worker harness still needed.
- **Dogfooded** — qwen3.5-35b (local) produced valid `execute_work_item` and `request_review` responses against the new protocol

Active next step: **design and build a local worker harness** — see `docs/plans/2026-03-15-next-session-plan.md`

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

Production-like defaults:

```bash
export VELORA_ALLOWED_OWNERS=darcuri
export VELORA_COORDINATOR_BACKEND=direct-claude
export VELORA_WORKER_BACKEND=direct-codex
export VELORA_MODE_A_REVIEW_ENABLED=true
```

Local LLM testing (no API tokens burned):

```bash
export VELORA_COORDINATOR_BACKEND=direct-local
export VELORA_WORKER_BACKEND=direct-local  # needs worker harness, not yet built
# optional:
export VELORA_LOCAL_BASE_URL=http://localhost:1234
export VELORA_LOCAL_MODEL=qwen/qwen3.5-35b-a3b
export VELORA_LOCAL_TIMEOUT=600
```

Auth expected for production backends:
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
- `velora/run.py` — state machine orchestrator (7 handler functions: _state_preflight, _state_awaiting_decision, _state_dispatching_worker, _state_polling_ci, _state_dispatching_review, _state_processing_dismissal, _state_terminal)
- `velora/protocol.py` — all protocol objects (WorkItem, WorkResult, ReviewBrief, ReviewResult, ReviewFinding, FindingDismissal, CoordinatorResponse)
- `velora/coordinator.py` — coordinator prompt template and execution
- `velora/runners.py` — backend dispatch (acp-claude, acp-codex, direct-claude, direct-codex, direct-local)
- `velora/acpx.py` — LLM API calls including run_local_llm and run_structured_review
- `velora/cli.py` — CLI surface including `audit inspect`
- `velora/audit.py` — run-scoped audit artifacts
- `tests/test_mode_a_work_result_integration.py` — most important integration-style safety net
- `tests/test_review_protocol_integration.py` — end-to-end review protocol flow tests
- `tests/test_state_machine.py` — state machine transition and review gate tests
- `docs/plans/` — current working state + near-term roadmap
- `docs/plans/2026-03-15-next-session-plan.md` — immediate next steps

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

See `docs/plans/2026-03-15-next-session-plan.md` for immediate next steps, and `docs/plans/next-tasks.md` for the full roadmap. Short version:

1. **Design and build local worker harness** — command allowlist, file scope, iteration model, failsafe gates. This enables dogfooding without burning API tokens.
2. **Wire run_structured_review** into _state_dispatching_review (replace legacy bridge)
3. **Named failure modes** in coordinator/worker prompts (self-audit vocabulary)
4. **Propulsion principle** in prompts (execute immediately, no plan narration)

## Design principles (established 2026-03-15)

- **The coordinator is the only LLM that makes judgment calls.** The orchestrator validates and routes — it never interprets results.
- **Strict protocol boundaries constrain drift without micromanaging work.** Agents do their jobs; protocol objects validate that the output is shaped correctly.
- **The orchestrator is a state machine, not a procedure.** Each state does one thing. New capabilities are new handlers, not new branches.
- **Review is protocol-driven end-to-end.** Structured input (ReviewBrief), structured output (ReviewResult), structured coordinator decisions (request_review, dismiss_finding).

## Reference architecture

Overstory (jayminwest/overstory) is a mature multi-agent orchestrator used as a reference for Velora's expansion trajectory. See `/home/arcuri/overstory.md` for the detailed alchemy report. Key patterns already adopted or queued: named failure modes, propulsion principle, HOW/WHAT instruction separation, worktree isolation (future), typed messaging (future).

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
