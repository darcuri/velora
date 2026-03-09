# Configuration

Velora supports configuration via a JSON config file and environment variables.

## Resolution order

1. Environment variables
2. Config file (first existing path wins)
3. Defaults

## Config file locations

- If set, `VELORA_CONFIG_PATH` is used exclusively.
- Otherwise Velora checks (in order):
  1) `${XDG_CONFIG_HOME:-~/.config}/velora/config.json`
  2) `~/.velora/config.json`

## Example config

```json
{
  "allowed_owners": ["octocat"],
  "max_attempts": 3,

  "mode_a_max_tokens": 200000,
  "mode_a_max_cost_usd": 20,
  "mode_a_no_progress_max": 4,
  "mode_a_max_wall_seconds": 1800,

  "runner": "codex",
  "codex_session_prefix": "velora-codex-",
  "claude_session_prefix": "velora-claude-",

  "vault_addr": "https://vault.example:8200",
  "vault_role_id_file": "~/.velora/vault-role-id",
  "vault_secret_id_file": "~/.velora/vault-secret-id",
  "vault_api_keys_path": "/v1/secret/data/velora/api-keys",

  "acpx_cmd": null,
  "acpx_fallback": "/path/to/acpx"
}
```

## Environment variables

All of these override config file values:

- `VELORA_ALLOWED_OWNERS`
- `VELORA_MAX_ATTEMPTS`

Mode A:
- `VELORA_MODE_A_MAX_TOKENS`
- `VELORA_MODE_A_MAX_COST_USD`
- `VELORA_MODE_A_NO_PROGRESS_MAX`
- `VELORA_MODE_A_MAX_WALL_SECONDS`
- `VELORA_COORDINATOR_RUNNER`
- `VELORA_USD_EQUIV_PER_1M_TOKENS`

- `VELORA_RUNNER` (codex | claude)
- `VELORA_COORDINATOR_BACKEND` (`acp-claude` | `acp-codex` | `direct-claude`)
- `VELORA_WORKER_BACKEND` (`acp-claude` | `acp-codex` | `direct-claude` | `direct-codex`)
- `VELORA_CODEX_SESSION_PREFIX`
- `VELORA_CLAUDE_SESSION_PREFIX`
- `VELORA_VAULT_ADDR` (or `VAULT_ADDR`)
- `VELORA_VAULT_ROLE_ID_FILE`
- `VELORA_VAULT_SECRET_ID_FILE`
- `VELORA_VAULT_API_KEYS_PATH`
- `VELORA_ACPX_CMD` (ACP-backed fallback only)
- `VELORA_ACPX_FALLBACK` (ACP-backed fallback only)
