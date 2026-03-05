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
- `VELORA_ALLOWED_OWNERS` (comma-separated owner allowlist; default: **unset** → required)

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

## Internal fault injection

This repo includes a test-only crash hook for resume-path dogfooding. It is intentionally dangerous and must not be treated as a user feature.

- Primary gate: `VELORA_INTERNAL_DANGEROUS_FAULT_INJECTION_ENABLE=I_UNDERSTAND_THIS_WILL_CRASH_VELORA`
- Checkpoint selector: `VELORA_INTERNAL_DANGEROUS_FAULT_INJECTION_CHECKPOINT=<checkpoint>[,<checkpoint>...]`
- Current internal checkpoints include `after_pr_created`, `after_ci_success_before_review`, and `after_review_resolution`.

The hook persists task state before raising, so it is suitable only for local testing of interruption and `velora resume` recovery. Do not set these env vars in normal usage, shared shells, CI, or documentation aimed at end users.
