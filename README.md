# velora

VELORA (VEry LOng Running Agent): a Python CLI orchestrator that runs coding agents (via ACPX), creates PRs, gates on CI, and applies FIRE (Fix-and-Retry).

Status: bootstrapping.

## Usage (v0)

Prefer passing long task text via a JSON spec file (or stdin) so it doesn’t show up in your process list:

```bash
cat > spec.json <<'JSON'
{
  "task": "Add SPEED unit conversions (m/s, km/h, mph) with tests"
}
JSON

velora run darcuri/tiny-lab feature --spec spec.json
```

You *can* pass a task directly, but it’s unsafe (visible via `ps`):

```bash
velora run darcuri/tiny-lab feature --unsafe-task "Add SPEED unit conversions (m/s, km/h, mph) with tests"
```
