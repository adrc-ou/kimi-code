#!/usr/bin/env python3
"""Publish only completed reports from this launch and pass, never a stale container file.

The service_*.py namespace is reserved for module probes discovered by check_services.py.
This host-side report publisher must remain outside it.
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

if __package__:
    from .private_file import write_private_json
else:
    from private_file import write_private_json

MAX_REPORT_BYTES = 8 * 1024 * 1024


def pending(path, run_id, mode):
    write_private_json(path, {
        "run_id": run_id, "mode": mode, "state": "pending", "exit_code": None,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "counts": {}, "checks": [],
    })


def collect(source, target, run_id, mode):
    try:
        with source.open("rb") as handle:
            raw = handle.read(MAX_REPORT_BYTES + 1)
        if len(raw) > MAX_REPORT_BYTES:
            return False
        report = json.loads(raw)
        if (report.get("run_id") != run_id or report.get("mode") != mode
                or report.get("state") != "complete" or type(report.get("exit_code")) is not int
                or report.get("exit_code") not in (0, 1, 2)
                or not isinstance(report.get("checks"), list)
                or not isinstance(report.get("counts"), dict)):
            return False
    except (OSError, ValueError, AttributeError, RecursionError):
        return False
    write_private_json(target, report)
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("pending", "collect"))
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--mode", required=True, choices=("quick", "full"))
    args = parser.parse_args()
    if args.command == "pending":
        pending(args.report, args.run_id, args.mode)
        return 0
    if args.source is None:
        parser.error("collect requires --source")
    return 0 if collect(args.source, args.report, args.run_id, args.mode) else 1


if __name__ == "__main__":
    raise SystemExit(main())
