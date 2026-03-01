import argparse
import json
import sys

from .run import run_task
from .state import get_status_view

VERBS = ("feature", "fix", "refactor")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="velora", description="VELORA CLI")
    sub = parser.add_subparsers(dest="cmd", required=True)

    status_p = sub.add_parser("status", help="Show active and recent tasks")
    status_p.add_argument("--json", action="store_true", help="Emit machine-readable JSON")

    run_p = sub.add_parser("run", help="Run a VELORA task")
    run_p.add_argument("repo", help="GitHub repo in owner/repo format")
    run_p.add_argument("verb", choices=VERBS, help="Task kind")
    run_p.add_argument("task", help="Task description")
    run_p.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    return parser


def _print_status(json_mode: bool) -> int:
    payload = get_status_view()
    if json_mode:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print("active:")
        if payload["active"]:
            for item in payload["active"]:
                print(f"- {item['task_id']} [{item['status']}] {item['repo']} {item['verb']} {item['task']}")
        else:
            print("- none")
        print("recent:")
        if payload["recent"]:
            for item in payload["recent"]:
                line = f"- {item['task_id']} [{item['status']}] {item['repo']} {item['verb']} {item['task']}"
                if item.get("pr_url"):
                    line += f" ({item['pr_url']})"
                print(line)
        else:
            print("- none")
    return 0


def _print_run_result(result: dict[str, object], json_mode: bool) -> int:
    if json_mode:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print(f"task_id: {result['task_id']}")
        print(f"status: {result['status']}")
        if result.get("pr_url"):
            print(f"pr_url: {result['pr_url']}")
        if result.get("summary"):
            print(f"summary: {result['summary']}")
    return 0 if result["status"] in {"ready", "not-ready"} else 1


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        if args.cmd == "status":
            return _print_status(args.json)
        if args.cmd == "run":
            result = run_task(args.repo, args.verb, args.task)
            return _print_run_result(result, args.json)
    except Exception as exc:  # noqa: BLE001
        if getattr(args, "json", False):
            print(json.dumps({"status": "error", "error": str(exc)}))
        else:
            print(f"error: {exc}", file=sys.stderr)
        return 1

    return 2
