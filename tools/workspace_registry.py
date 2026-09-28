#!/usr/bin/env python3
"""The workspaces this checkout has been pointed at, newest first.

The launch sequence asks "which directory is this session's workspace?" before it can know which
instance it is launching, because the instance identity digests the workspace path. So the answer
cannot live in the instance directory it selects: a file under ``.local/runtime/<instance>/`` would
be orphaned by the very choice that writes it. It sits at ``.local/workspaces.json`` instead,
beside the runtime tree and below every sweep that knows about generated state, and it is
deliberately *not* session material — the whole point is that it outlives the launch that updated
it.

Three rules keep the file trustworthy rather than merely convenient.

*Paths are stored canonical.* Every entry is the ``realpath`` of an absolute path, so a symlink, a
``..``, a doubled slash and a relocated mount all collapse onto the one name the launcher will
mount. A list of near-duplicates is a list the operator cannot read.

*Newest first, and never more than* :data:`MAX_RECENT`. The screen shows the list in this order and
the launcher reads the head of it when nobody is asked, so the order is load-bearing in two places
and is decided once, here.

*Unreadable is empty.* A missing, half-written, or nonsensical file costs the operator their
history and nothing else. Every reader returns what it could trust and drops the rest, because a
corrupt preference file must not be able to stop a launch.

Nothing here is secret — the paths are the ones the agent will be shown in its own mount table —
but the file is written 0600 anyway, like everything else in ``.local``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

if __package__:
    from .private_file import write_private_json
else:  # run as a script by the launcher, which puts ``tools/`` alone on the path
    from private_file import write_private_json

#: The document's shape tag, so a later format can be told apart from a stale one rather than read
#: as an empty registry.
SCHEMA = 1
#: Where the list lives, relative to the harness checkout.
FILE_NAME = "workspaces.json"
#: How many workspaces the screen may list, and how many the file may hold. One number for both,
#: because a list the interface truncates is a list whose tail is invisible forever.
MAX_RECENT = 10


@dataclass(frozen=True)
class Entry:
    """One remembered workspace: the canonical directory, and when it was last chosen."""

    path: str
    last_used: str

    @property
    def name(self) -> str:
        """The final component, which is what the screen prints in bold."""
        return Path(self.path).name or self.path


def registry_path(root: Path) -> Path:
    """The file that holds the list for a given harness checkout."""
    return Path(root) / ".local" / FILE_NAME


def canonical(path: str | os.PathLike[str]) -> str:
    """The one spelling of ``path`` the registry stores.

    ``realpath`` is used rather than ``abspath`` because it resolves the symlinks the picker is
    told to resolve, and because it does not require the result to exist: a workspace the operator
    is about to create canonicalises against its parent, which is the same answer the launcher's
    ``mkdir -p`` will produce.

    Raises :class:`ValueError` for anything that cannot name a directory, which the callers read as
    "not a workspace" rather than as an error to report.
    """
    text = os.fspath(path)
    if not text.strip() or any(character in text for character in ("\n", "\r", "\x00")):
        raise ValueError("a workspace path must be a single line of text")
    resolved = os.path.realpath(os.path.abspath(text))
    if not resolved.startswith("/") or resolved == "/":
        raise ValueError(f"{text!r} does not name a directory below the filesystem root")
    return resolved


def _stamp(when: datetime | None) -> str:
    """One timestamp format for the whole file, so sorting needs no parser.

    Microseconds, not seconds: two workspaces chosen inside one second is ordinary (a launch that
    backs up and re-picks), and a tie would otherwise be settled alphabetically by the sort below —
    which could hand ``--non-interactive`` the older of the two.
    """
    moment = when or datetime.now(UTC)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).isoformat()


def _seen(entry: Entry) -> datetime:
    """When ``entry`` was last chosen, with an unparseable stamp read as oldest.

    A row whose timestamp this file cannot explain is still a workspace the operator used. Dropping
    it to preserve an ordering nobody can see would be the worse answer, so it sorts to the bottom
    instead.
    """
    try:
        moment = datetime.fromisoformat(entry.last_used)
    except ValueError:
        return datetime.fromtimestamp(0, UTC)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment


def _ordered(entries: list[Entry]) -> tuple[Entry, ...]:
    """Newest first, with ties broken by path so a rewrite of the same list is byte-identical."""
    return tuple(sorted(entries, key=lambda entry: (_seen(entry), entry.path), reverse=True))


def load(root: Path) -> tuple[Entry, ...]:
    """The remembered workspaces, newest first, dropping whatever cannot be trusted."""
    try:
        document = json.loads(registry_path(Path(root)).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ()
    if not isinstance(document, dict) or document.get("schema") != SCHEMA:
        return ()
    rows = document.get("workspaces")
    if not isinstance(rows, list):
        return ()
    entries: list[Entry] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        path = row.get("path")
        seen = row.get("last_used")
        if not isinstance(path, str) or not isinstance(seen, str):
            continue
        try:
            entries.append(Entry(canonical(path), seen))
        except ValueError:
            continue
    return _ordered(_deduplicate(entries))


def _deduplicate(entries: list[Entry]) -> list[Entry]:
    """One row per directory, keeping the most recent mention of each."""
    by_path: dict[str, Entry] = {}
    for entry in entries:
        keep = by_path.get(entry.path)
        if keep is None or _seen(entry) > _seen(keep):
            by_path[entry.path] = entry
    return list(by_path.values())


def save(root: Path, entries: tuple[Entry, ...] | list[Entry]) -> tuple[Entry, ...]:
    """Publish the list, capped at :data:`MAX_RECENT`, and return what was written."""
    ordered = _ordered(list(entries))[:MAX_RECENT]
    write_private_json(
        registry_path(Path(root)),
        {
            "schema": SCHEMA,
            "workspaces": [{"path": e.path, "last_used": e.last_used} for e in ordered],
        },
    )
    return ordered


def touch(root: Path, path: str | os.PathLike[str], *, when: datetime | None = None) -> Entry:
    """Record that ``path`` is this launch's workspace, ahead of everything else.

    An existing entry is moved rather than duplicated, so choosing the third row puts it first and
    the list stays a history of use rather than an append-only log.
    """
    chosen = Entry(canonical(path), _stamp(when))
    rest = [entry for entry in load(Path(root)) if entry.path != chosen.path]
    save(Path(root), [chosen, *rest])
    return chosen


def remove(root: Path, path: str | os.PathLike[str]) -> tuple[Entry, ...]:
    """Drop one workspace from the list, which is what the screen's ``Remove From List`` means.

    Quiet about a name it has never heard: the caller asked for a state, and this delivers it.
    """
    try:
        target = canonical(path)
    except ValueError:
        return load(Path(root))
    return save(Path(root), [entry for entry in load(Path(root)) if entry.path != target])


def newest(root: Path) -> str:
    """The workspace to use when nobody is asked, or ``""`` when there is nothing to remember.

    This is the whole of ``--non-interactive`` and every read-only entry point (``./shell.sh``,
    ``./extensions.sh``, ``./prompts.sh``): they run against the workspace the last interactive
    launch chose, which is the only answer they could act on.
    """
    entries = load(Path(root))
    return entries[0].path if entries else ""


def main(argv: list[str] | None = None) -> int:
    """The shell's one question about the list.

    ``newest`` prints the head, or nothing at all when the list is empty — the caller distinguishes
    those from its own exit status rather than from a string a script would have to recognise.
    """
    parser = argparse.ArgumentParser(
        prog="workspace_registry.py", description=__doc__.splitlines()[0]
    )
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("command", choices=("newest",))
    args = parser.parse_args(argv)
    path = newest(args.root)
    if not path:
        return 1
    print(path)
    return 0


if __name__ == "__main__":  # pragma: no cover - a one-line shell helper
    sys.exit(main())
