# Velora CLI

## Commands

### `velora status`
Show active and recent tasks.

```bash
velora status
velora status --json
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
velora run darcuri/tiny-lab feature --spec spec.json

# stdin
cat spec.json | velora run darcuri/tiny-lab feature --spec -
```

#### Allowed but unsafe: `--unsafe-task`
This puts the task text directly in the command line.

```bash
velora run darcuri/tiny-lab feature --unsafe-task "Add SPEED conversions with tests"
```

## Useful environment variables (v0)

- `VELORA_ALLOWED_OWNERS` (comma-separated; default: `darcuri`)
- `VELORA_MAX_ATTEMPTS` (default: `3`)
- `VELORA_CODEX_SESSION_PREFIX` (default: `velora-codex-`)

Vault integration:
- `VELORA_VAULT_ADDR` (or `VAULT_ADDR`)
- `VELORA_VAULT_ROLE_ID_FILE`
- `VELORA_VAULT_SECRET_ID_FILE`
- `VELORA_VAULT_API_KEYS_PATH`

ACPX:
- `VELORA_ACPX_CMD`
- `VELORA_ACPX_FALLBACK`
