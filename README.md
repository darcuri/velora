# velora

<p align="center">
  <img src="assets/velora-icon.png" alt="Velora" width="220" />
</p>

VELORA (VEry LOng Running Agent): a Python CLI orchestrator that runs coding agents via direct backends or ACP-backed fallback, creates PRs, gates on CI, and applies FIRE (Fix-and-Retry).

Status: bootstrapping.

## Contributing

See [CONTRIBUTING.md](./CONTRIBUTING.md).

## Usage (v0)

Velora currently supports two execution modes:

- **Legacy (default):** direct worker prompt + FIRE retry loop.
- **Mode A (`--coordinator`):** coordinator emits one WorkItem per iteration; worker executes; Velora gates on CI + review.

### Recommended (safe): JSON spec
Prefer passing long task text via a JSON spec file (or stdin) so it doesn’t show up in your process list:

```bash
cat > spec.json <<'JSON'
{
  "task": "Add SPEED unit conversions (m/s, km/h, mph) with tests"
}
JSON

# Legacy mode
velora run octocat/hello-world feature --spec spec.json

# Mode A coordinator loop
velora run octocat/hello-world feature --spec spec.json --coordinator
```

### Useful options

```bash
# Target a non-default base branch (PR will be opened against this base)
velora run octocat/hello-world feature --spec spec.json --coordinator --base-branch release/1.2

# Extra-verbose troubleshooting logs (writes task_dir/debug.jsonl)
velora run octocat/hello-world feature --spec spec.json --coordinator --debug
```

### Local pilot default: direct/direct

For local dogfooding, prefer direct coordinator/worker invocation and keep ACP-backed execution as fallback:

```bash
export VELORA_COORDINATOR_BACKEND=direct-claude
export VELORA_WORKER_BACKEND=direct-codex
```

These env vars select the transport backend. `--runner` / `VELORA_RUNNER` still choose the worker runner when ACP-backed execution is in play.

Token budget (Mode A):

```bash
export VELORA_MODE_A_MAX_TOKENS=200000
```

### Audit inspection

Mode A now writes per-run audit events to `.velora/runs/<run_id>/audit.jsonl` in the checked-out repo. Use `velora audit inspect` to print a summary of the most recent run, or pass a run id for a specific run. The summary includes objective snippet, iterations, coordinator decisions, and final status.

```bash
velora audit inspect --run 20260309041214-fa55112b
```

### Allowed but unsafe: `--unsafe-task`
You *can* pass a task directly, but it’s unsafe (visible via `ps`):

```bash
velora run octocat/hello-world feature --unsafe-task "Add SPEED unit conversions (m/s, km/h, mph) with tests"
```

## Docs

See [`docs/`](./docs/) (start at [`docs/README.md`](./docs/README.md)).

For the coordinator/worker non-negotiables, see [`docs/mode-a-safety-rails.md`](./docs/mode-a-safety-rails.md).
