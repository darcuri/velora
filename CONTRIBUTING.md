# Contributing to Velora

Velora is intentionally small and conservative. The goal is to reduce human toil without introducing new footguns.

## Development setup

Requirements:
- Python 3.12+
- `git`
- `gh` (GitHub CLI) authenticated (`gh auth status`)

Run unit tests:

```bash
python -m unittest discover -s tests -p 'test_*.py'
```

## Running Velora locally

Velora stores state under `~/.velora/` by default.

Common env vars:
- `VELORA_HOME` (override state dir)
- `VELORA_ALLOWED_OWNERS` (comma-separated owner allowlist; default: `darcuri`)

To avoid leaking prompts in your process list, prefer JSON run specs:

```bash
cat > spec.json <<'JSON'
{ "task": "..." }
JSON

velora run owner/repo feature --spec spec.json
```

### Agent requirements (for `velora run`)

Velora uses ACPX to run Codex for implementation. Ensure you have:
- ACPX installed/available, or set `VELORA_ACPX_CMD`
- OpenAI key available as `OPENAI_API_KEY` (or Vault integration configured)

## Project principles

- **Prefer safe defaults.** Nothing destructive by default.
- **No secret leaks.** Be careful with logs/exceptions.
- **Keep it boring.** Avoid unnecessary complexity; build only what reduces toil.

## CI behavior

- Docs-only changes (`docs/**`, `README.md`) run the lightweight `docs` workflow.
- Code changes run the `test` workflow.
- Mixed changes run both.
