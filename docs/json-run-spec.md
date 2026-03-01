# JSON Run Spec (v0)

Velora can read a “run spec” from JSON, either from a file or stdin.

This is primarily a **privacy/safety feature**: passing long task prompts as CLI args makes them visible via process listings.

## Example

```json
{
  "task": "Add SPEED unit conversions (m/s, km/h, mph) with tests",
  "title": "Optional PR title override",
  "body": "Optional extra PR body text",
  "max_attempts": 3
}
```

## Fields

- `task` (string, required)
  - The task description.

- `title` (string, optional)
  - Overrides the PR title.

- `body` (string, optional)
  - Extra text appended to the PR body.

- `max_attempts` (int, optional; 1–10)
  - Overrides FIRE retry attempts for this run.

## Usage

```bash
velora run darcuri/tiny-lab feature --spec spec.json

# stdin
cat spec.json | velora run darcuri/tiny-lab feature --spec -
```

## Unsafe alternative

```bash
velora run darcuri/tiny-lab feature --unsafe-task "..."
```

Use this only for short throwaway tasks where prompt privacy doesn’t matter.
