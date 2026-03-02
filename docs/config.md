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
  "codex_session_prefix": "velora-codex-",

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
- `VELORA_CODEX_SESSION_PREFIX`
- `VELORA_VAULT_ADDR` (or `VAULT_ADDR`)
- `VELORA_VAULT_ROLE_ID_FILE`
- `VELORA_VAULT_SECRET_ID_FILE`
- `VELORA_VAULT_API_KEYS_PATH`
- `VELORA_ACPX_CMD`
- `VELORA_ACPX_FALLBACK`
