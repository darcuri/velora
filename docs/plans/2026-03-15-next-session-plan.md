# Next Session Plan: Local Worker Harness + Quick Wins

_Written: 2026-03-15, end of PR #72 session_

## What just landed (PR #72)

- Structured review protocol (ReviewBrief, ReviewResult, ReviewFinding, FindingDismissal)
- Orchestrator refactored from procedural monolith into state machine (7 handlers)
- Coordinator decisions expanded to 5 (added request_review, dismiss_finding)
- Local LLM backend (direct-local) for testing against LM Studio
- Dogfooded: qwen3.5-35b produced valid CoordinatorResponse with new decisions

## Tomorrow's priorities (in order)

### 1. Design the local worker harness

The `direct-local` backend works for the coordinator (JSON in, JSON out). But the worker needs to actually edit files, run commands, and commit — a raw chat completion can't do that.

**What to design:**

- **Command allowlist**: which shell commands can the harness execute? Likely: `git checkout`, `git add`, `git commit`, `git diff`, `git status`, file read/write operations, test runners (`python -m pytest`, `python -m unittest`), linters. NOT: `git push`, `git merge`, `rm -rf`, `curl`, network access, package installation.

- **File scope enforcement**: can we restrict writes to the work branch's worktree? This is the first place to implement the file scope concept from the Overstory patterns.

- **Iteration model**: the local LLM can't run tools natively. The harness needs to be a loop: prompt → LLM responds with structured action → harness executes action → result fed back to LLM → repeat until LLM signals done. Define the action protocol (what actions exist, what their schemas look like).

- **Failsafe gates**: max iterations per work item, max file modifications, timeout, output size limits. What happens when a gate trips — does the harness produce a `blocked` WorkResult?

- **WorkResult production**: the harness must produce a valid `WorkResult` (result.json) just like the real worker backends. The LLM doesn't write this directly — the harness assembles it from the execution trace.

- **Behavior restrictions (prompt-level)**: what the LLM is told in its system prompt — stay in scope, don't modify files outside the work item's likely_files, don't attempt network access.

- **Code restrictions (mechanical)**: what the harness physically prevents — path validation before writes, command allowlist before execution, branch validation before commits.

**Key question to resolve**: is this a thin tool-use loop (LLM emits tool calls, harness executes them) or a code-generation-and-apply model (LLM generates a patch, harness applies it)? Tool-use is more flexible but needs more guardrails. Patch-apply is simpler but limits what the worker can do.

### 2. Wire run_structured_review into the state machine (task 2d)

Quick win — the `DISPATCHING_REVIEW` handler currently bridges to the legacy review path. `run_structured_review` exists but isn't called. Swap the bridge for the real thing. Small diff, already designed.

### 3. Named failure modes in prompts (task 5)

Quick win — add the self-audit vocabulary to coordinator and worker prompts. Pure prompt engineering, no code changes beyond the prompt strings. Already specified in next-tasks.md.

### 4. Propulsion principle in prompts (task 6)

Trivial — one paragraph per prompt. Already specified.

## If there's time after that

- Schema-retry observability (task 1 from next-tasks.md)
- HOW/WHAT instruction separation (task 7)
- Dogfood the structured review protocol end-to-end with the local harness

## What NOT to do tomorrow

- Don't expand to multi-agent. The single-worker harness must be solid first.
- Don't build a prompt-tuning framework. Test reviewer prompts manually.
- Don't over-engineer the harness. Start with the minimum that produces a valid WorkResult.
