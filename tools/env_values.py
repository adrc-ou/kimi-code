#!/usr/bin/env python3
"""Read the harness's own ``KEY=VALUE`` files.

Several launcher steps need operator configuration or a generated environment file parsed the
same way: optional ``#`` comments, one assignment per line, and one layer of matching quotes
removed. This is deliberately *not* a shell evaluator - the launcher only ever reads files it
wrote itself or that Docker Compose resolved, and evaluating them would let a value execute.
"""

from __future__ import annotations

from pathlib import Path


def read_env_values(path: Path) -> dict[str, str]:
    """Return the last assignment for each key, as the later lines win in both shell and Compose."""
    values: dict[str, str] = {}
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values
