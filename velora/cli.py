from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict

from .coordinator import run_coordinator_v1
from .orchestrator import build_initial_coordinator_request, coordinator_session_name
from .constants import VERBS
from .run import resume_task, run_task
from .spec import RunSpec, load_run_spec
from .state import get_status_view, prune_stale_tasks


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="velora", description="VELORA CLI")
    sub = parser.add_subparsers(dest="cmd", required=True)

    status_p = sub.add_parser("status", help="Show active and recent tasks")
    status_p.add_argument("--json", action="store_true", help="Emit machine-readable JSON")

    gc_p = sub.add_parser("gc", help="Mark old running-like tasks as stale")
    gc_p.add_argument("--older-than-hours", type=int, default=24, help="Mark tasks older than this as stale")
    gc_p.add_argument("--dry-run", action="store_true", help="Report what would change without writing")
    gc_p.add_argument("--json", action="store_true", help="Emit machine-readable JSON")

    run_p = sub.add_parser("run", help="Run a VELORA task")
    run_p.add_argument("repo", help="GitHub repo in owner/repo format")
    run_p.add_argument("verb", choices=VERBS, help="Task kind")

    src = run_p.add_mutually_exclusive_group(required=True)
    src.add_argument(
        "--spec",
        help="Path to JSON run spec (recommended). Use '-' to read JSON from stdin.",
    )
    src.add_argument(
        "--unsafe-task",
        help="Task description as a CLI arg (UNSAFE: visible in process list).",
    )

    run_p.add_argument(
        "--runner",
        choices=("codex", "claude"),
        help="Which ACPX coding agent to use (default: config/env).",
    )
    run_p.add_argument("--coordinator", action="store_true", help="Use Mode A coordinator loop")
    run_p.add_argument("--json", action="store_true", help="Emit machine-readable JSON")

    resume_p = sub.add_parser("resume", help="Resume a VELORA task by task_id")
    resume_p.add_argument("task_id", help="Task id (from velora status)")
    resume_p.add_argument("--json", action="store_true", help="Emit machine-readable JSON")

    coord_p = sub.add_parser("coord", help="Coordinator utilities (Mode A scaffolding)")
    coord_sub = coord_p.add_subparsers(dest="coord_cmd", required=True)

    coord_req = coord_sub.add_parser("request", help="Emit the initial CoordinatorRequest JSON for a task")
    coord_req.add_argument("repo", help="GitHub repo in owner/repo format")
    coord_req.add_argument("verb", choices=VERBS, help="Task kind")
    coord_req_src = coord_req.add_mutually_exclusive_group(required=True)
    coord_req_src.add_argument("--spec", help="Path to JSON run spec (recommended). Use '-' for stdin.")
    coord_req_src.add_argument("--unsafe-task", help="Task description as a CLI arg (UNSAFE)")
    coord_req.add_argument("--json", action="store_true", help="Emit compact machine-readable JSON")

    coord_run = coord_sub.add_parser("run", help="Run coordinator once and emit validated CoordinatorResponse JSON")
    coord_run.add_argument("repo", help="GitHub repo in owner/repo format")
    coord_run.add_argument("verb", choices=VERBS, help="Task kind")
    coord_run_src = coord_run.add_mutually_exclusive_group(required=True)
    coord_run_src.add_argument("--spec", help="Path to JSON run spec (recommended). Use '-' for stdin.")
    coord_run_src.add_argument("--unsafe-task", help="Task description as a CLI arg (UNSAFE)")
    coord_run.add_argument("--json", action="store_true", help="Emit compact machine-readable JSON")

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


def _print_gc_result(result: dict[str, object], json_mode: bool) -> int:
    if json_mode:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        count = int(result.get("count") or 0)
        dry = bool(result.get("dry_run"))
        hrs = result.get("older_than_hours")
        print(f"gc: marked {count} task(s) stale (older_than_hours={hrs}, dry_run={dry})")
        for tid in result.get("stale_marked") or []:
            print(f"- {tid}")
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


def _load_spec_from_args(args: argparse.Namespace) -> RunSpec:
    if getattr(args, "spec", None):
        return load_run_spec(args.spec)
    return RunSpec(task=str(getattr(args, "unsafe_task")))


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        if args.cmd == "status":
            return _print_status(args.json)

        if args.cmd == "gc":
            result = prune_stale_tasks(
                older_than_hours=int(args.older_than_hours),
                dry_run=bool(args.dry_run),
            )
            return _print_gc_result(result, args.json)

        if args.cmd == "run":
            spec = _load_spec_from_args(args)
            result = run_task(
                args.repo,
                args.verb,
                spec,
                runner=getattr(args, "runner", None),
                use_coordinator=bool(getattr(args, "coordinator", False)),
            )
            return _print_run_result(result, args.json)

        if args.cmd == "resume":
            result = resume_task(str(args.task_id))
            return _print_run_result(result, args.json)

        if args.cmd == "coord":
            spec = _load_spec_from_args(args)
            request, repo_path = build_initial_coordinator_request(args.repo, args.verb, spec)

            if args.coord_cmd == "request":
                if args.json:
                    print(json.dumps(request, sort_keys=True))
                else:
                    print(json.dumps(request, indent=2, sort_keys=True))
                return 0

            if args.coord_cmd == "run":
                session = coordinator_session_name(request["repo"]["owner"], request["repo"]["name"])
                coord_runner = os.environ.get("VELORA_COORDINATOR_RUNNER", "claude")
                resp = run_coordinator_v1(session_name=session, cwd=repo_path, request=request, runner=str(coord_runner))
                payload = asdict(resp)
                if args.json:
                    print(json.dumps(payload, sort_keys=True))
                else:
                    print(json.dumps(payload, indent=2, sort_keys=True))
                return 0

    except Exception as exc:  # noqa: BLE001
        if getattr(args, "json", False):
            print(json.dumps({"status": "error", "error": str(exc)}))
        else:
            print(f"error: {exc}", file=sys.stderr)
        return 1

    return 2
