# Velora Security

Velora is an automation tool that can:
- run coding agents,
- create branches/PRs,
- post comments,
- poll CI,
- and (eventually) help drive merges.

That means mistakes can be expensive. The defaults should be conservative.

## Threat model (v0)

Primary risks:
- **Prompt/task leakage** via shell history, `ps`/process lists, CI logs, or chat logs.
- **Secret leakage** (API keys, Vault tokens) into stdout/stderr, PR comments, or exceptions.
- **Accidental destructive Git/GitHub actions** (closing the wrong PR, deleting branches, force-push).
- **Scope creep** (running against repos/orgs that weren’t intended).

## Prompt privacy: do not pass long tasks on the command line

Command-line arguments are visible to other local users via process listing tools.

### Recommended
Provide the task via a JSON spec file or stdin:

```bash
# file
velora run owner/repo feature --spec spec.json

# stdin
cat spec.json | velora run owner/repo feature --spec -
```

JSON spec (v0):

```json
{
  "task": "Add SPEED unit conversions (m/s, km/h, mph) ...",
  "title": "Optional PR title override",
  "body": "Optional extra PR body text",
  "max_attempts": 3
}
```

### Allowed but unsafe

```bash
velora run owner/repo feature --unsafe-task "..."
```

## Secrets

- Prefer pulling API keys from the environment first.
- If not present, Velora may pull keys from Vault.
- Never print Vault URLs that embed API keys.
- Avoid including secrets in exception messages.

Vault configuration overrides:
- `VELORA_VAULT_ADDR` (or `VAULT_ADDR`)
- `VELORA_VAULT_ROLE_ID_FILE`
- `VELORA_VAULT_SECRET_ID_FILE`
- `VELORA_VAULT_API_KEYS_PATH` (defaults to `/v1/secret/data/openclaw/api-keys`)

## Repo allowlist

By default Velora only runs against a conservative owner allowlist.

- `VELORA_ALLOWED_OWNERS` (comma-separated, default: `darcuri`)

## Safe defaults / future work

- No self-merge in v0.
- No branch deletion in v0.
- Any “cleanup” automation should be opt-in and carefully scoped.
