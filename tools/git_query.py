#!/usr/bin/env python3
"""Ask a repository a read-only question without trusting its own configuration.

The launcher queries the workspace repository to bind an approval manifest to a revision,
and to locate that repository's ``info/exclude`` file. The workspace is agent-authored, so
its ``.git/config`` is not trusted input: a repository can otherwise name an external
transport, a filesystem-monitor hook, or a path to execute. These settings only ever read,
so every one of those vectors is switched off, and no optional lock file is written into a
tree the agent is actively modifying.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

ISOLATION = (
    "--no-optional-locks",
    "-c",
    "protocol.ext.allow=never",
    "-c",
    "core.fsmonitor=false",
    "-c",
    "core.hooksPath=/dev/null",
)


def git_text(root: Path, *arguments: str) -> str | None:
    """Return trimmed stdout from a read-only Git query, or None if it did not succeed."""
    result = subprocess.run(
        ["git", "-C", str(root), *ISOLATION, *arguments],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip()
