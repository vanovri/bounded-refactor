"""CLI for the bounded refactor runner."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .core import RefactorError, run


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bounded-refactor", description="Apply a bounded, allowlisted refactor to a local copy and verify it.")
    sub = parser.add_subparsers(dest="command", required=True)
    command = sub.add_parser("run")
    command.add_argument("--project", required=True, type=Path)
    command.add_argument("--config", required=True, type=Path)
    command.add_argument("--candidate", required=True, type=Path)
    command.add_argument("--out", required=True, type=Path)
    command.add_argument("--repairgate-report", type=Path)
    arguments = parser.parse_args(argv)
    try:
        report = run(arguments.project, arguments.config, arguments.candidate, arguments.out, arguments.repairgate_report)
    except RefactorError as exc:
        print(f"bounded-refactor: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({
        "accepted": report["accepted"],
        "attempts": len(report["attempts"]),
        "changed_files": report["changed_files"],
        "diff": str(arguments.out / report["diff"]),
        "report": str(arguments.out / "report.json"),
    }, indent=2))
    return 0 if report["accepted"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
