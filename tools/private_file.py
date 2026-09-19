#!/usr/bin/env python3
"""Publish one file so a reader can never see half of it.

Every session artifact the launcher stages - the resolved plan, the credential fragment, the
module environment, the panel's preferences - is written through here. The module deliberately
imports nothing from ``tools/`` so it can be dropped into the minimal trees the launcher tests
build, and it deliberately takes text rather than bytes so the encoding decision is made once.
"""

from __future__ import annotations

import json
import os
import secrets
from pathlib import Path


def write_private(path: Path, text: str) -> None:
    """Write ``path`` at mode 0600 and rename it into place.

    Three properties matter and none of them is obvious from a ``write_text`` call:

    * the name is unique per attempt, so a temp orphaned by a killed process cannot make every
      later launch die on a collision the way a fixed ``<name>.tmp`` with ``O_EXCL`` does;
    * the mode is set at create time, so there is no window where a secret is world-readable;
    * the rename is the publish, and the directory is fsynced after it, so a reader either sees
      the old file or the whole new one across a crash rather than a partial write.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    name = f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
    descriptor = os.open(
        path.parent / name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    temporary = path.parent / name
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    directory_fd = os.open(path.parent, os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    # A file created by another uid's umask can still come back narrower; state the mode.
    path.chmod(0o600)


def write_private_json(path: Path, value: object) -> None:
    """The same publish for the JSON documents the launcher hands to the proxy and compose."""
    write_private(path, json.dumps(value, indent=2) + "\n")
