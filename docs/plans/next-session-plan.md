# Next Session Plan

_Written: 2026-03-16, after PR #74 (JSON repair + investigator + multi-backend harness)_

## What just landed (PR #74)

- JSON repair for local model multiline output
- Investigator-based test discovery (full pipeline)
- run_probe action for environment probing
- Multi-backend worker harness (local + Anthropic API)
- Soft investigate cap, unrestricted read scope, gate tool detection
- Dogfood validated: Opus + Sonnet, Opus + qwen3.5-9b both committed on tiny-lab

## Next priorities (in order)

### 1. Fix orchestrator hang after endgame

The orchestrator hangs after a successful commit when trying to publish the branch to GitHub. Observed in both the Sonnet and qwen3.5-9b runs — the commit succeeds but the process never exits. Likely the `_publish_branch` or PR creation step is blocking on a `gh` CLI call or waiting for input. This is the most immediate issue since it prevents fully autonomous runs.

### 2. Separate coordinator and worker model configs

Currently `VELORA_LOCAL_MODEL` is shared between coordinator and worker. The hybrid config (Opus coordinator + local worker) works around this by using `direct-claude` for the coordinator, but fully-local runs can't split models. Add `VELORA_HARNESS_MODEL` as a distinct override for the worker harness LLM, falling back to `VELORA_LOCAL_MODEL` when unset.

### 3. run.py decomposition

~2300 lines, exceeds 25K tokens. The state machine structure from PR #72 creates natural seams. Extract:
- State handler functions into `velora/states/` package
- History/evaluation helpers into `velora/evaluation.py`
- Worker dispatch helpers into `velora/dispatch.py`

This is quality-of-life — the code works but is hard to navigate.

### 4. Wire run_structured_review into _state_dispatching_review

The `DISPATCHING_REVIEW` state handler currently bridges to the legacy review path. `run_structured_review` exists but isn't called from the state machine. Small diff, already designed.

### 5. Named failure modes in coordinator/worker prompts

Add self-audit vocabulary: SCOPE_EXPLOSION, INFERENCE_CHAIN, PREMATURE_DONE, etc. These are grepable in debug logs and give the coordinator/worker explicit anti-patterns to check against. Already specified in next-tasks.md.

### 6. Propulsion principle in agent prompts

One paragraph per prompt: execute immediately, no plan narration, no confirmation seeking. Already specified.

### 7. Smarter investigate prompt engineering

Current investigate workers waste turns reading files that don't exist (setup.cfg, tox.ini, Makefile on tiny-lab). The prompt says "figure it out" but the worker still guesses common filenames. Consider:
- Teaching the worker to list first, then read only what exists
- Adding a "stop investigating after you have a test command" instruction
- Investigating whether the coordinator should pre-populate scope_hints better

### 8. Worker turn budget awareness

Workers have no sense of how many turns they've used vs the cap. Adding a turn counter to the action result feedback (e.g., "turn 15/20") would let the model prioritize finishing over exploring. Low effort, potentially big impact on local model efficiency.

## What NOT to do next

- Don't expand to multi-agent/parallel workers. Single-worker must be battle-tested first.
- Don't build a prompt-tuning framework. Manual iteration is fine at this stage.
- Don't add more LLM backends. Local + Anthropic covers the needed configs.
