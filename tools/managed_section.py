#!/usr/bin/env python3
"""Replace one delimited section of the workspace ``AGENTS.md`` and nothing else.

Two producers write generated guidance into the workspace file - selected modules, and the
resolved model-policy envelope - into one file the operator also edits by hand. Both need the
same guarantees: only the marked region is rewritten, text outside it survives verbatim, a
corrupt or duplicated marker pair is refused rather than repaired, and the file is never
reached through a symlink or written by anyone but its owner.

Each producer passes its own marker pair, so the sections coexist and a partial run of one
cannot erase the other.
"""

from __future__ import annotations

import os
import stat

if __package__:
    from .safe_workspace_init import OPEN_DIR, UnsafeWorkspace, require_directory
else:
    from safe_workspace_init import OPEN_DIR, UnsafeWorkspace, require_directory

MAX_BYTES = 4 * 1024 * 1024


def _outside(existing: str, begin: str, end: str) -> str:
    """Remove this producer's section, rejecting a marker pair that is not exactly well-formed."""
    if begin not in existing and end not in existing:
        return existing
    if (
        existing.count(begin) != 1
        or existing.count(end) != 1
        or existing.index(end) < existing.index(begin)
    ):
        raise UnsafeWorkspace(
            f"invalid guidance markers {begin!r} / {end!r}; preserve and repair AGENTS.md"
        )
    head, rest = existing.split(begin, 1)
    _, tail = rest.split(end, 1)
    return head + tail


def render(existing: str, begin: str, end: str, text: str) -> str:
    """Compose the new file, idempotently however many times the launcher runs."""
    outside = _outside(existing, begin, end).rstrip("\n")
    if not text:
        return f"{outside}\n" if outside else ""
    head = f"{outside}\n\n" if outside else ""
    body = text.strip("\n")
    return f"{head}{begin}\n{body}\n{end}\n"


def replace_section(workspace, begin: str, end: str, text: str) -> None:
    """Atomically rewrite one marked section of ``<workspace>/AGENTS.md``."""
    from pathlib import Path

    directory = Path(workspace)
    fd = os.open(directory, OPEN_DIR)
    try:
        require_directory(fd, str(workspace), os.getuid())
        try:
            handle = os.open(
                "AGENTS.md",
                os.O_RDWR | os.O_CREAT | os.O_NONBLOCK | os.O_NOFOLLOW,
                0o600,
                dir_fd=fd,
            )
        except OSError as exc:
            # O_NOFOLLOW already refuses an AGENTS.md that is a link; report it as the
            # operator problem it is rather than as an unexplained traceback.
            raise UnsafeWorkspace(f"cannot open workspace AGENTS.md: {exc}") from exc
        try:
            info = os.fstat(handle)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
                raise UnsafeWorkspace("unsafe workspace AGENTS.md")
            if info.st_size > MAX_BYTES:
                raise UnsafeWorkspace("workspace AGENTS.md exceeds 4 MiB")
            existing = os.read(handle, info.st_size).decode()
            content = render(existing, begin, end, text)
            if content != existing:
                encoded = content.encode()
                os.lseek(handle, 0, 0)
                os.write(handle, encoded)
                os.ftruncate(handle, len(encoded))
                os.fsync(handle)
        finally:
            os.close(handle)
    finally:
        os.close(fd)
