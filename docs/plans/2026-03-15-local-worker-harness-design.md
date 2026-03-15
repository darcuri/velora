# Local Worker Harness Design Spec

_Written: 2026-03-15_

## Problem

The `direct-local` backend works for the coordinator (JSON in, JSON out via
`run_local_llm`). But the worker needs to edit files, run tests, and commit.
A raw chat completion cannot do that.

The local worker harness is a multi-turn execution loop that translates
structured LLM actions into real operations, enforces scope and safety
constraints, and assembles a validated WorkResult from mechanical observations.

## Architecture

Two new files:

- **`velora/local_worker.py`** — the loop: manages conversation history, calls
  the LLM, parses action responses, dispatches to the action executor, enforces
  caps/gates, runs the endgame, assembles WorkResult.
- **`velora/worker_actions.py`** — action registry: each action is a validation
  function + executor function. Adding a new action means adding one dataclass +
  one function; no loop changes needed.

The harness replaces the current `run_local_llm()` call in `runners.py` for the
`direct-local` worker backend path.

## Design Principles

- **The LLM is an executor, not a planner.** The coordinator already decided
  what to do. The worker does it.
- **One action per turn.** The LLM emits a single typed action, the harness
  validates and executes it, returns the result. This makes every step auditable.
- **Hard scope boundaries.** The LLM can only touch files the coordinator
  specified. If scope is insufficient, the worker signals blocked and the
  coordinator decides.
- **The harness owns git and WorkResult assembly.** The LLM never runs git
  commands, never writes the WorkResult. The harness commits from observed
  diffs and assembles the WorkResult from mechanical facts.
- **The LLM is unaware of meta-state.** It does not know iteration counts,
  retry counts, or context budget. It just does its job.

## Integration

### Runner gate bypass

`_state_dispatching_worker` in `run.py` currently hard-rejects any
`selected_specialist.runner` not in `{"codex", "claude"}`. When
`VELORA_WORKER_BACKEND=direct-local`, the orchestrator must skip the runner
validation and pass directly to `run_worker` with the backend override. The
coordinator's `selected_specialist.runner` value is irrelevant in this mode.

### WorkResult delivery

The harness writes the WorkResult file directly to the exchange path
(`result.json`, `block.json`, etc.) — the same paths used by Codex/Claude
workers. The harness receives the exchange paths as input parameters.

The `run_worker` signature in `runners.py` for `direct-local` changes from:

```python
return run_local_llm(prompt, cwd=cwd)
```

to a call to `local_worker.run_local_worker()` which receives:
- `work_item: WorkItem`
- `repo_root: Path`
- `work_branch: str`
- `exchange_dir: Path` (where to write result.json / block.json)
- `repo_ref: str`
- `run_id: str`
- `verb: str`
- `objective: str`
- `iteration: int`

The function writes the WorkResult file and returns a `CmdResult` with
`returncode=0` on success (the orchestrator reads from the exchange file, not
from stdout).

### Specialist role

v1 ignores `selected_specialist.role` entirely. The system prompt is the same
regardless of whether the coordinator selected `implementer`, `refactor`, or
`investigator`. Future versions may vary the prompt based on role.

## Action Protocol

### LLM Response Format

Every LLM response must be exactly one JSON object:

```json
{
  "action": "<action_type>",
  "params": { ... }
}
```

No markdown, no prose, no commentary. The harness rejects anything that is not
valid JSON with a known action type.

### Parse Failure Handling

If the LLM response is not valid JSON or has an unknown action type, the harness
sends an error back and the LLM gets another turn:

```json
{"status": "error", "result": "Invalid response. Emit exactly one JSON object with action and params."}
```

A consecutive parse failure counter tracks this. After 3 consecutive parse
failures, the harness terminates with `PARSE_FAILURES`. A successful parse
resets the counter to 0.

### Harness Response Format

After executing an action, the harness returns:

```json
{
  "status": "ok" | "error",
  "result": "...the output..."
}
```

### Action Types

#### `read_file`

Read a file within scope.

```json
{"action": "read_file", "params": {"path": "relative/path"}}
```

- Path must resolve within `allowed_files` or `allowed_dirs`.
- Returns file contents as string.
- Error if file does not exist.

#### `list_files`

List files in a directory within scope.

```json
{"action": "list_files", "params": {"path": "relative/dir"}}
```

- Path must be in `allowed_dirs` or a subdirectory of one.
- Returns list of filenames (one level, not recursive).
- Error if directory does not exist.

#### `write_file`

Write full file contents to a file in scope.

```json
{"action": "write_file", "params": {"path": "relative/path", "content": "..."}}
```

- Path must be exactly in `allowed_files`.
- Creates or overwrites the file.
- Returns confirmation with byte count.
- New files: the coordinator must include new file paths in
  `scope_hints.likely_files`. If a file needs to be created but is not in scope,
  the worker should emit `work_blocked` with `SCOPE_INSUFFICIENT`.

#### `patch_file`

Apply a search/replace edit to a file in scope.

```json
{"action": "patch_file", "params": {"path": "relative/path", "old": "...", "new": "..."}}
```

- Path must be exactly in `allowed_files`.
- Reads current content, finds `old` string, replaces with `new`.
- Fails if `old` is not found or is not unique.
- Returns confirmation.

#### `search_files`

Search for a pattern across files in scope.

```json
{"action": "search_files", "params": {"pattern": "search term"}}
```

- Searches all files within `allowed_dirs` (not just `allowed_files`).
- Pattern is a literal substring or simple regex.
- Returns list of `{file, line_number, line_content}` matches.
- Capped at 50 results to prevent context bloat.
- Implemented in pure Python (pathlib + re), no shell.

#### `run_tests`

Run a test command from the allowlist.

```json
{"action": "run_tests", "params": {"command": "python -m pytest -q"}}
```

- Command string is compared against the allowlist by joining the allowlist
  entry to a string (e.g. `["python", "-m", "pytest", "-q"]` becomes
  `"python -m pytest -q"`). Must match exactly.
- Executed via `subprocess.run` with `cwd=repo_root`, timeout (default 120s),
  captured output.
- Returns stdout+stderr and pass/fail status.
- This is the only action that touches the shell.

#### `work_complete`

Signal that the task is done. Terminates the action loop and enters the endgame.

```json
{"action": "work_complete", "params": {"summary": "what you did"}}
```

#### `work_blocked`

Signal that the task cannot be completed. Terminates the action loop and
produces a blocked WorkResult.

```json
{"action": "work_blocked", "params": {"reason": "SCOPE_INSUFFICIENT", "blockers": ["need access to velora/config.py"]}}
```

`reason` must be one of:
- `SCOPE_INSUFFICIENT` — task requires files not in scope.
- `TASK_UNCLEAR` — instructions are ambiguous or contradictory.
- `CANNOT_RESOLVE` — worker understands the task but cannot solve it.

Unknown reason values are treated as `CANNOT_RESOLVE`.

## Scope Enforcement

### WorkerScope

```python
@dataclass
class WorkerScope:
    repo_root: Path          # absolute path to worktree
    allowed_files: set[str]  # from work_item.scope_hints.likely_files (repo-relative)
    allowed_dirs: set[str]   # parent dirs of allowed_files
    test_commands: list[str] # from acceptance gates mapped to real commands
    work_branch: str         # the branch we're operating on
```

### Coordinator Contract

The coordinator is responsible for providing thorough `scope_hints.likely_files`.
This includes:
- Existing files that need modification.
- New files that need creation (e.g. test files).
- Files needed for context (e.g. imports, config).

If `likely_files` is insufficient, the worker signals `SCOPE_INSUFFICIENT` and
the coordinator reissues with expanded scope. The harness does not attempt to
guess or expand scope on its own.

### `allowed_dirs` Derivation

`allowed_dirs` is the set of parent directories of all `allowed_files`. This is
intentionally narrow. If `likely_files` is `["velora/run.py", "velora/protocol.py"]`,
then `allowed_dirs` = `{"velora"}`. The worker cannot read project-root files
like `README.md` unless the coordinator includes a root-level file in
`likely_files`.

### Path Resolution

All paths go through one function that:
- Resolves relative to `repo_root`
- Rejects `..` traversal
- Rejects symlinks pointing outside the repo
- Rejects absolute paths

Every action uses this same gate.

### Per-Action Scope Rules

| Action | Scope constraint |
|--------|-----------------|
| `read_file` | `allowed_files` or files within `allowed_dirs` |
| `list_files` | `allowed_dirs` or subdirectory of one |
| `write_file` | exactly `allowed_files` |
| `patch_file` | exactly `allowed_files` |
| `search_files` | all files within `allowed_dirs` |
| `run_tests` | command must be in `test_commands` allowlist |
| `work_complete` | always allowed |
| `work_blocked` | always allowed |

## Harness Loop Lifecycle

### Phase 0: Pre-flight

Before starting the action loop, the harness:
- Verifies the working tree is clean (`git status --porcelain` is empty).
  If not, abort with `COMMIT_FAILED` — the harness requires a clean starting
  state.
- Checks out the work branch (create if needed).

### Phase 1: Setup

- Receive WorkItem from orchestrator.
- Build system prompt from WorkItem fields.
- Initialize message list with system prompt.
- Initialize counters: `iteration=0`, `test_retry=0`, `context_bytes=0`,
  `parse_failures=0`.
- Build `WorkerScope` from WorkItem.

### Phase 2: Action Loop

Repeats until a terminal condition:

1. Check context cap — if exceeded, produce `CONTEXT_OVERFLOW` result.
2. Call LLM with current message list.
3. Parse response as action JSON.
   - On parse failure: send error back, increment `parse_failures`. If
     `parse_failures >= 3`, produce `PARSE_FAILURES` result. A successful
     parse resets `parse_failures` to 0.
4. Validate action type and params.
5. Enforce scope/allowlist.
6. Execute action via action registry.
7. Append result to message list.
8. Summarize older results (messages older than last 4 turns with results
   exceeding 2KB are truncated to first+last 40 lines with `[truncated]`
   marker). Summarization thresholds are tunable — smaller-context models
   may need more aggressive truncation.
9. Update `context_bytes`.
10. Increment `iteration` counter.
11. Check iteration cap — if exceeded, produce `ITERATION_LIMIT` result.
12. If action was `work_complete` or `work_blocked`, exit loop.

### Phase 3: Endgame (on `work_complete`)

No more LLM turns after this point. All steps are mechanical.

**Step 1 — Diff audit:**
- `git diff --stat` against work branch base.
- Every changed file must be in `allowed_files`. Violation → `SCOPE_VIOLATION`.
- Count diff lines against `work_item.limits.max_diff_lines`. Over limit →
  `DIFF_LIMIT`.
- No changes at all → `NO_CHANGES`.
- Reject binary files.

**Step 2 — Run tests:**
- Run every gate in `work_item.acceptance.gates`, mapped via `GATE_COMMANDS`.
- Gates not in `GATE_COMMANDS` are handled as follows:
  - `ci`: skipped — CI polling is the orchestrator's domain.
  - `docs`: skipped in v1 — no local docs gate command.
  - Unknown gates: skipped with a warning in evidence.
- Each mapped command runs with `cwd=repo_root`, timeout (default 120s),
  captured output.
- Record results as `WorkResultTestRun` objects.
- On failure: if `test_retry < test_retry_cap`, feed failure back to LLM and
  re-enter action loop. Otherwise → `TESTS_EXHAUSTED`.

**Step 3 — Commit:**
- `git add` on exactly the files from the diff audit.
- Construct commit message from `work_item.commit` (message + footer).
- `git commit`. Failure → `COMMIT_FAILED`.
- `git rev-parse HEAD` to capture head SHA.

**Step 4 — Assemble WorkResult:**

```python
WorkResult(
    protocol_version=1,
    work_item_id=work_item.id,
    status="completed",
    summary=llm_summary,        # from work_complete params — only LLM-sourced field
    branch=work_branch,         # known from setup
    head_sha=head_sha,          # from git rev-parse
    files_touched=changed_files,# from diff audit
    tests_run=test_results,     # from step 2
    blockers=[],                # empty — we passed
    follow_up=[],               # empty for v1
    evidence=evidence_lines,    # test output snippets
)
```

Validated through existing `validate_work_result()` before writing to disk.

## Caps

| Cap | Default | LLM Awareness | Outcome |
|-----|---------|---------------|---------|
| Iteration cap | 20 turns | None | `ITERATION_LIMIT` |
| Test retry cap | 3 attempts | None (just sees "tests failed, fix it") | `TESTS_EXHAUSTED` |
| Context cap | 128KB | None | `CONTEXT_OVERFLOW` |
| Parse failure cap | 3 consecutive | None (just sees error message) | `PARSE_FAILURES` |

The LLM is never told about caps. It just does its work. The harness silently
enforces limits. All cap defaults are overridable via environment variables.

## Outcome Model

### HarnessReason Enum

Binary outcome (success/fail) with a typed reason code:

```python
class HarnessReason(enum.Enum):
    SUCCESS            = "SUCCESS"
    SCOPE_VIOLATION    = "SCOPE_VIOLATION"
    SCOPE_INSUFFICIENT = "SCOPE_INSUFFICIENT"
    DIFF_LIMIT         = "DIFF_LIMIT"
    NO_CHANGES         = "NO_CHANGES"
    TESTS_EXHAUSTED    = "TESTS_EXHAUSTED"
    ITERATION_LIMIT    = "ITERATION_LIMIT"
    CONTEXT_OVERFLOW   = "CONTEXT_OVERFLOW"
    PARSE_FAILURES     = "PARSE_FAILURES"
    WORKER_BLOCKED     = "WORKER_BLOCKED"
    GATE_TIMEOUT       = "GATE_TIMEOUT"
    COMMIT_FAILED      = "COMMIT_FAILED"
```

### HarnessOutcome

```python
@dataclass
class HarnessOutcome:
    success: bool              # True = completed, False = failed
    reason: HarnessReason      # always set, SUCCESS for happy path
    evidence: list[str]        # supporting detail (test output, filenames, etc.)
```

### Mapping to WorkResult

| HarnessReason | WorkResult status |
|---------------|-------------------|
| `SUCCESS` | `completed` |
| `CONTEXT_OVERFLOW` | `blocked` |
| `SCOPE_INSUFFICIENT` | `blocked` |
| `WORKER_BLOCKED` | `blocked` |
| All others | `failed` |

The reason enum name is placed in `blockers[0]` for failed/blocked outcomes.
The coordinator routes on the reason code — no prose parsing.

## System Prompt

Tight, blunt, propulsion-oriented. Target: ~800 tokens.

```
You are a code execution tool. You receive a task, you execute it, you return
the result.

Do not ask questions. Do not propose alternatives. Do not explain your reasoning.
Do not narrate what you are about to do. Do not summarize what you did.
Emit one action per response. JSON only. No markdown. No prose.

If you cannot complete the task, emit work_blocked. Otherwise, execute and emit
work_complete.

## Your task
Repo: {repo_ref}
Branch: {work_branch}
Work item: {work_item.id} ({work_item.kind})
Rationale: {work_item.rationale}

## Instructions
{numbered list from work_item.instructions}

## Files in scope
{work_item.scope_hints.likely_files, one per line}

## Test commands available
{mapped gate commands, one per line}

## Acceptance criteria
Must: {list}
Must not: {list}

## Available actions
{"action": "read_file", "params": {"path": "relative/path"}}
{"action": "list_files", "params": {"path": "relative/dir"}}
{"action": "write_file", "params": {"path": "relative/path", "content": "..."}}
{"action": "patch_file", "params": {"path": "relative/path", "old": "...", "new": "..."}}
{"action": "search_files", "params": {"pattern": "search term"}}
{"action": "run_tests", "params": {"command": "python -m pytest -q"}}
{"action": "work_complete", "params": {"summary": "what you did"}}
{"action": "work_blocked", "params": {"reason": "SCOPE_INSUFFICIENT|TASK_UNCLEAR|CANNOT_RESOLVE", "blockers": ["..."]}}

## Rules
- You may only read/write files listed in scope.
- You may only run test commands listed above.
- Emit one action per response. JSON only.
- Start by reading the files you need, then make changes, then signal completion.
- If you cannot complete the task with the files in scope, use work_blocked.
```

## Conversation Format

OpenAI-compatible chat messages:

```json
[
  {"role": "system", "content": "<system prompt>"},
  {"role": "assistant", "content": "{\"action\": \"read_file\", \"params\": {\"path\": \"velora/run.py\"}}"},
  {"role": "user", "content": "{\"status\": \"ok\", \"result\": \"<file contents>\"}"},
  ...
]
```

"User" messages are always harness action results. The LLM never sees actual
human input after the system prompt.

### Summarization (Option B)

After each action result is appended, the harness checks if any message older
than the last 4 turns has a result exceeding 2KB. If so, it truncates to
first+last 40 lines with a `[truncated]` marker. The system prompt is never
touched. Thresholds (2KB, 40 lines, 4-turn recency window) are tunable via
constants for model-specific adjustment.

### Test Failure Re-entry

When endgame tests fail and retries remain, the harness appends:

```json
{"role": "user", "content": "{\"status\": \"error\", \"result\": \"Tests failed. Fix the issue.\\n\\n<truncated output>\"}"}
```

The LLM re-enters the action loop and can read files, patch, and emit
`work_complete` again to restart the endgame.

## Test Command Allowlist

Gate names from `work_item.acceptance.gates` map to real commands:

```python
GATE_COMMANDS = {
    "tests":    ["python", "-m", "pytest", "-q"],
    "lint":     ["python", "-m", "flake8"],
    "security": ["python", "-m", "bandit", "-r", ".", "-q"],
}
```

Only commands in this map are executable. The LLM's `run_tests` command string
is compared against allowlist entries joined to strings (e.g. the list
`["python", "-m", "pytest", "-q"]` becomes `"python -m pytest -q"`). Must match
exactly — no shell interpolation, no argument injection.

Gates not in `GATE_COMMANDS`:
- `ci`: skipped — CI polling is the orchestrator's domain.
- `docs`: skipped in v1 — no local docs gate command.
- Unknown: skipped with a warning recorded in evidence.

## What This Does NOT Do

- No git push (harness commits but the orchestrator owns publishing).
- No network access from the LLM.
- No package installation.
- No arbitrary shell commands.
- No WorkResult authoring by the LLM.
- No awareness of orchestrator state, iteration counts, or retry budgets.
