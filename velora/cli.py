import argparse


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="velora", description="VELORA CLI (bootstrapping)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="Show task status (stub)")

    run_p = sub.add_parser("run", help="Run a task (stub)")
    run_p.add_argument("repo")
    run_p.add_argument("verb")
    run_p.add_argument("task")

    args = parser.parse_args(argv)

    if args.cmd == "status":
        print("VELORA status: (stub)")
        return 0

    if args.cmd == "run":
        print(f"VELORA run: repo={args.repo} verb={args.verb} task={args.task} (stub)")
        return 0

    return 2
