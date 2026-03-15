# Velora CLI

## Commands

### `velora status`
Show active and recent tasks.

```bash
velora status
velora status --json
```

### `velora gc`
Mark old running-like tasks as `stale` (so `velora status` isn’t haunted by abandoned runs).

```bash
velora gc
velora gc --older-than-hours 6
velora gc --older-than-hours 6 --dry-run
velora gc --json
```

### `velora run <owner/repo> <verb> ...`
Run a Velora task.

`verb` is one of:
- `feature`
- `fix`
- `refactor`

#### Recommended (safe): JSON spec
Provide task text via a JSON spec file (or stdin) to avoid leaking prompts in your process list.

```bash
velora run octocat/hello-world feature --spec spec.json

# stdin
cat spec.json | velora run octocat/hello-world feature --spec -
```

#### Allowed but unsafe: `--unsafe-task`
This puts the task text directly in the command line.

```bash
velora run octocat/hello-world feature --unsafe-task "Add SPEED conversions with tests"
```

#### Mode A coordinator loop
Enable Mode A (coordinator → WorkItem → worker → CI + review → repeat):

```bash
velora run octocat/hello-world feature --spec spec.json --coordinator
```

Optional post-success review stage:
- Enable with `VELORA_MODE_A_REVIEW_ENABLED=true`.
- After CI + review pass, Velora runs a structured review stage that returns:
  - `approve`: coordinator should finalize success.
  - `repair`: coordinator should issue a repair follow-up WorkItem.

#### Common options

```bash
# Override runner for the worker (default: config/env)
velora run octocat/hello-world feature --spec spec.json --runner codex

# Local pilot default: direct coordinator + direct worker
VELORA_COORDINATOR_BACKEND=direct-claude VELORA_WORKER_BACKEND=direct-codex \
  velora run octocat/hello-world feature --spec spec.json --coordinator

# Target a non-default base branch (also resets the local checkout to origin/<base>)
velora run octocat/hello-world feature --spec spec.json --coordinator --base-branch release/1.2

# Write verbose debug logs to task_dir/debug.jsonl
velora run octocat/hello-world feature --spec spec.json --coordinator --debug
```

### `velora resume <task_id>`
Resume an existing task branch/PR and re-run the remaining gates (CI poll + review).

```bash
velora resume 20260304203339-b5a843aa
velora resume 20260304203339-b5a843aa --debug
```

### `velora audit inspect`
Inspect run-scoped audit artifacts from `.velora/runs/<run_id>/audit.jsonl`.

```bash
# latest run
velora audit inspect

# specific run
velora audit inspect --run 20260309041214-fa55112b
```

When the structured review protocol is active, audit output will include `review_requested` and `finding_dismissed` events where present.

## Useful environment variables (v0)

Velora also supports a JSON config file; see [Configuration](./config.md).

- `VELORA_ALLOWED_OWNERS` (comma-separated allowlist; default: **unset** → required)
- `VELORA_MAX_ATTEMPTS` (default: `3`)

Mode A policy defaults:
- `VELORA_MODE_A_MAX_TOKENS` (default: `200000`; primary token budget breaker)
- `VELORA_MODE_A_NO_PROGRESS_MAX` (default: `4`)
- `VELORA_MODE_A_MAX_WALL_SECONDS` (default: `1800`)
- `VELORA_MODE_A_REVIEW_ENABLED` (default: `false`; enables post-success review stage)
- `VELORA_USD_EQUIV_PER_1M_TOKENS` (default: unset/0; informational only)
- `VELORA_COORDINATOR_RUNNER` (default: `claude`; also supports `codex`)

- `VELORA_RUNNER` (default: `codex`; also supports `claude`)
- `VELORA_COORDINATOR_BACKEND` (`acp-claude` | `acp-codex` | `direct-claude`)
- `VELORA_WORKER_BACKEND` (`acp-claude` | `acp-codex` | `direct-claude` | `direct-codex`)
- `VELORA_CODEX_SESSION_PREFIX` (default: `velora-codex-`)
- `VELORA_CLAUDE_SESSION_PREFIX` (default: `velora-claude-`)

Local pilot default:
- `VELORA_COORDINATOR_BACKEND=direct-claude`
- `VELORA_WORKER_BACKEND=direct-codex`

Vault integration:
- `VELORA_VAULT_ADDR` (or `VAULT_ADDR`)
- `VELORA_VAULT_ROLE_ID_FILE`
- `VELORA_VAULT_SECRET_ID_FILE`
- `VELORA_VAULT_API_KEYS_PATH`

ACP-backed fallback only:
- `VELORA_ACPX_CMD`
- `VELORA_ACPX_FALLBACK`

Secrets (env-first; Vault fallback is optional):
- `OPENAI_API_KEY` (required for runner=codex)
- `ANTHROPIC_AUTH_TOKEN` or `ANTHROPIC_API_KEY` (required for runner=claude)
- `GEMINI_API_KEY` (required for Gemini review)
