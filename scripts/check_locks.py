#!/usr/bin/env python3
"""Validate immutable dependency and compatibility metadata."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    lock = json.loads((ROOT / "dependencies.lock.json").read_text())
    for value in lock["images"].values():
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", value):
            raise SystemExit(f"invalid image digest: {value}")
    for value in lock["sources"].values():
        if not re.fullmatch(r"[0-9a-f]{40}", value):
            raise SystemExit(f"invalid source commit: {value}")
    for relative, expected in lock.get("files", {}).items():
        if expected != f"sha256:{digest(ROOT / relative)}":
            raise SystemExit(f"locked file digest is stale: {relative}")
    for module in sorted((ROOT / "modules").glob("*/check_locks.py")):
        subprocess.run(["python3", str(module)], check=True)
    exceptions = json.loads((ROOT / "vulnerability-exceptions.json").read_text())
    required = {"package", "vulnerability", "owner", "reason", "expires"}
    for item in exceptions["exceptions"]:
        if not required.issubset(item):
            raise SystemExit("vulnerability exception is missing required fields")
        if date.fromisoformat(item["expires"]) < date.today():
            raise SystemExit(f"expired vulnerability exception: {item['vulnerability']}")


if __name__ == "__main__":
    main()
