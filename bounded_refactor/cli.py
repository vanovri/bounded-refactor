"""CLI for the bounded refactor runner."""
from __future__ import annotations

import argparse
import json
import logging
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .core import RefactorError, run

logger = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bounded-refactor",
        description="Apply a bounded, allowlisted refactor to a local copy and verify it.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    run_cmd = subparsers.add_parser("run")
    run_cmd.add_argument("--project", required=True, type=Path)
    run_cmd.add_argument("--config", required=True, type=Path)
    run_cmd.add_argument("--candidate", required=True, type=Path)
    run_cmd.add_argument("--out", required=True, type=Path)
    run_cmd.add_argument("--repairgate-report", type=Path)
    return parser


def format_cli_summary(report: Mapping[str, Any], output_dir: Path) -> str:
    return json.dumps(
        {
            "accepted": report["accepted"],
            "attempts": len(report["attempts"]),
            "changed_files": report["changed_files"],
            "diff": str(output_dir / str(report["diff"])),
            "report": str(output_dir / "report.json"),
        },
        indent=2,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    arguments = parser.parse_args(list(argv) if argv is not None else None)
    try:
        report = run(
            arguments.project,
            arguments.config,
            arguments.candidate,
            arguments.out,
            arguments.repairgate_report,
        )
    except RefactorError as exc:
        logger.debug("Bounded refactor failed with RefactorError: %s", exc)
        print(f"bounded-refactor: {exc}", file=sys.stderr)
        return 2
    print(format_cli_summary(report, arguments.out))
    return 0 if report["accepted"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
