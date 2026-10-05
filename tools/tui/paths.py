"""What counts as a workspace directory, and what ``Tab`` means here.

This is the *policy* half of the workspace screen, kept apart from the surface so that the rules
can be read — and argued with — without a terminal. Nothing here paints or reads a keypress;
everything here answers a question about the filesystem as it is right now, which is the whole
reason the launch sequence asks again about a directory it has seen before: the disk may have moved
since.

Three ideas carry the module.

*A verdict, not a boolean.* :func:`usable` returns the canonical path, whether it exists yet,
whether it could be created, and the sentence to say when it cannot be used. The screen needs all
of it — it prints the error, and it decides between "Create it?" and "Remove it from the list?" —
and a function returning ``True``/``False`` would force the caller to stat the disk a second time
to find out *which* no it got, against a state that may have changed in between.

*Spaces are tried both ways.* A path with a space in it has two spellings and only one of them is
usually the directory: the literal one, and the shell-escaped one the operator is reaching for out
of habit. :func:`usable` settles which to believe, and the rule is narrow on purpose — see its
docstring.

*Completion is readline's, not an invention.* :func:`completions` follows ``complete -d``: one
directory is inserted, several insert only what they agree on and are then listed, a dot-prefixed
name has to be asked for by typing the dot, and a directory is marked with its trailing slash. What
it does not offer is files, because a workspace is a directory or nothing.
"""

from __future__ import annotations

import os
import re
from collections.abc import Sequence
from dataclasses import dataclass

#: What ``complete -d`` appends to a directory name, and what an insertion appends here too. It is
#: a marker as much as a convenience: the next ``Tab`` then lists *inside* the name just completed,
#: which is the behaviour anyone typing a path expects.
DIRECTORY_MARKER = "/"

#: A space no backslash has claimed. A shell needs the escape because it splits words on it; an
#: operating system does not, which is precisely why the two spellings are worth trying in order.
_UNESCAPED_SPACE = re.compile(r"(?<!\\) ")
#: The same habit from the other side: a space that *was* escaped, which is how a shell would have
#: to be told about a directory whose real name has a space in it.
ESCAPED_SPACE = "\\ "
#: A Windows-style drive root, with or without a trailing separator: a root, not a path.
_DRIVE_ROOT = re.compile(r"^[A-Za-z]:[\\/]?$")
#: Redundant separators, folded before anything is compared.
_DOUBLED = re.compile(r"//+")
#: Anything that is not a name a directory can be created under.
_NAMELESS = {"", ".", ".."}


@dataclass(frozen=True)
class Verdict:
    """One answer about one path, stated as fully as the screen needs.

    ``error`` is written for the operator and is the only sentence the surface prints; ``path`` is
    written for the launcher and is the only one it acts on. Carrying both together is what stops a
    caller acting on a path that failed a check, or printing a failure it never made.
    """

    #: The canonical directory — absolute, links resolved, no trailing slash.
    path: str = ""
    #: The spelling that was judged, so an error can quote what the operator actually typed.
    typed: str = ""
    #: Whether something is already there at the end of this path.
    exists: bool = False
    #: Whether nothing is there yet but the parent would let it be created.
    creatable: bool = False
    #: Why this answer cannot be taken, or ``""`` when it can.
    error: str = ""
    #: Whether links were followed to get here. A workspace whose name is an alias for somewhere
    #: else is worth knowing about, so the screen says so rather than silently renaming it.
    resolved: bool = False

    @property
    def usable(self) -> bool:
        """Whether the launcher may take this path as its workspace."""
        return not self.error and bool(self.path)


def is_root(value: str) -> bool:
    """Whether ``value`` names a filesystem root rather than a directory inside one.

    Trailing separators are what make ``/`` and ``//`` the same answer, and the drive spellings are
    here because an operator copying a path out of a Windows tool arrives with them: the sentence
    they need is "that is a root", not a permission error about a path this host cannot have.
    """
    return value.rstrip("/") in ("", ".", "..") or bool(_DRIVE_ROOT.match(value))


def canonical(value: str) -> tuple[str, bool]:
    """The directory ``value`` names once links are followed, and whether any link was followed.

    ``realpath`` is the spec's "if the path is a symlink, or a similar thing like a shortcut or an
    alias, identify the target path and replace the entered value with that". On macOS it also
    folds ``/tmp`` onto ``/private/tmp``, which is the same phenomenon one level up: the launcher
    mounts the target, so the target is the name worth showing. A failure to resolve comes back as
    the input, and the caller's own checks are what reject it.

    Redundant separators are folded before the comparison, so a path typed with ``//`` in it is not
    reported as having resolved to something else. It has not: it was always the same directory,
    and a screen that says "resolves to" about a doubled slash teaches the operator to ignore it.
    """
    text = _DOUBLED.sub("/", value.rstrip("/") or "/")
    try:
        resolved = os.path.realpath(text)
    except OSError:
        return text, False
    return (resolved, resolved != text) if resolved else (text, False)


def _readwrite(path: str) -> bool:
    """Whether this user may both read and write ``path``.

    Both bits at once, because the screen asks for both at once: a directory that can be listed but
    not written is not a workspace, it is where the session fails its first write and blames the
    model for it.
    """
    return os.access(path, os.R_OK | os.W_OK)


def _absolute(value: str) -> str:
    """The error for a path this launcher cannot mount, or ``""`` when it can."""
    if value.startswith("/"):
        return ""
    if _DRIVE_ROOT.match(value):
        return "that is a filesystem root — choose a directory inside it"
    if len(value) > 1 and value[1] == ":":
        return "that is not a path on this machine — use an absolute path starting with /"
    return "use an absolute path starting with /"


def _judge(candidate: str, *, allow_create: bool) -> Verdict:
    """Judge one exact spelling, with no space-retry left to make.

    Split out of :func:`usable` because the retry has to compare two of these, and threading one
    error string through the recursion would end up reporting the second spelling for a mistake the
    operator made in the first.
    """
    blank = Verdict(typed=candidate, error="enter a directory path")
    if not candidate.strip() or candidate in _NAMELESS:
        return blank
    relative = _absolute(candidate)
    if relative:
        return Verdict(typed=candidate, error=relative)
    if is_root(candidate):
        return Verdict(typed=candidate, error="that is the filesystem root — choose a directory")

    path, followed = canonical(candidate)
    if is_root(path):
        return Verdict(typed=candidate, error="that is the filesystem root", resolved=followed)
    if followed and not os.path.exists(path):
        # The name typed is a link and what it points at is not there. Following it was the right
        # thing to do — that is what a link is — but offering to create the *target* would start a
        # workspace two names away from the one that was asked for, and a broken link is worth
        # saying out loud rather than papering over with a directory nobody named.
        return Verdict(
            typed=candidate,
            resolved=True,
            error=f"{candidate} is a link to {path}, which is not there",
        )

    if os.path.isdir(path):
        if _readwrite(path):
            return Verdict(path=path, typed=candidate, exists=True, resolved=followed)
        return Verdict(
            path=path,
            typed=candidate,
            exists=True,
            resolved=followed,
            error=f"you cannot read and write {path}",
        )

    if os.path.exists(path):
        return Verdict(
            path=path,
            typed=candidate,
            exists=True,
            resolved=followed,
            error=f"{path} is a file, not a directory",
        )
    if not allow_create:
        return Verdict(typed=candidate, resolved=followed, error=f"{path} does not exist")

    # Only the final component may be missing. That is the difference between "I am about to start
    # a project called new-thing" and a typo three directories deep, and the parent is what says
    # which.
    parent = os.path.dirname(path) or "/"
    missing = os.path.basename(path)
    if missing in _NAMELESS:
        return Verdict(typed=candidate, error="that path has no directory name to create")
    if not os.path.isdir(parent):
        return Verdict(
            typed=candidate,
            resolved=followed,
            error=f"{parent} does not exist, so {missing} cannot be created",
        )
    if not _readwrite(parent):
        return Verdict(
            typed=candidate,
            resolved=followed,
            error=f"you cannot create {missing} inside {parent}",
        )
    return Verdict(path=path, typed=candidate, creatable=True, resolved=followed)


def contains(inner: str, outer: str) -> bool:
    """Whether ``outer`` is ``inner`` or a directory above it.

    Both sides go through the same folding as :func:`canonical`, so a trailing slash and a doubled
    separator cannot make containment deniable. ``/`` is deliberately not special-cased: it
    contains every absolute path, which is exactly why it is already refused as a root.
    """
    inside = _DOUBLED.sub("/", inner.rstrip("/") or "/")
    outside = _DOUBLED.sub("/", outer.rstrip("/") or "/")
    return inside == outside or inside.startswith(outside + "/")


def usable(
    value: str, *, allow_create: bool = True, reserved: Sequence[str] = ()
) -> Verdict:
    """Judge what the operator typed, with spaces tried in both spellings.

    The value as written is believed first, because that is the one the filesystem uses. Only when
    it is unusable *and* contains an unescaped space is the escaped spelling tried, and only ever
    for a directory that already exists — the retry creates nothing, because a path nobody has
    written down is the one case where the two spellings cannot be told apart from the disk, and
    guessing there would mint a directory with a literal backslash in its name.

    Whichever spelling wins, the launcher is given the canonical path the kernel accepts. The
    screen keeps showing what was typed: an answer that silently rewrites itself under the operator
    is the thing this retry is closest to becoming, and it is bounded by never touching the
    display.

    There is a second, quieter half to the same habit. An operator who types ``/work/my\\ project``
    means ``/work/my project``, but the escaped spelling is a perfectly legal *name*, so judging it
    on its own would report it as a directory to create and the launcher would oblige — minting a
    directory whose name contains a backslash beside the one that already exists. So a value that
    is not yet there, and that carries an escaped space, is also tried with the escapes taken back
    out, and the existing directory is believed. This direction never creates either.

    ``allow_create=False`` is what a remembered row is judged with when nothing is going to be
    created yet — it turns "missing" into an error rather than into a promise.
    """
    raw = (value or "").strip()
    if not raw:
        return Verdict(typed=value or "", error="enter a directory path")
    winner = _judge(raw, allow_create=allow_create)
    if not winner.usable and _UNESCAPED_SPACE.search(raw):
        tried = _judge(_UNESCAPED_SPACE.sub(r"\\ ", raw), allow_create=False)
        winner = tried if tried.usable else winner
    elif winner.creatable and ESCAPED_SPACE in raw:
        plain = _judge(raw.replace(ESCAPED_SPACE, " "), allow_create=False)
        winner = plain if plain.usable else winner
    return _undoes(winner, reserved)


def _undoes(verdict: Verdict, reserved: Sequence[str]) -> Verdict:
    """Refuse a workspace that would swallow one of the protected directories.

    Only a verdict that was otherwise usable is touched: a path that already failed for a plainer
    reason should be reported for that reason, not for what it would have contained.
    """
    if not verdict.usable:
        return verdict
    for held in reserved:
        if held and contains(held, verdict.path):
            return Verdict(
                typed=verdict.typed,
                exists=verdict.exists,
                resolved=verdict.resolved,
                error=f"{verdict.path} contains {held}, which a workspace may not hold",
            )
    return verdict


@dataclass(frozen=True)
class Completion:
    """One ``Tab`` press, answered.

    ``text`` and ``caret`` are the field's new contents and where its insertion point goes, which
    is how the caller applies an insertion without knowing anything about what matched.

    ``candidates`` is what the panel lists, and is empty whenever the insertion already said
    everything — one match, or a no-op where the candidates disagree from the first character. That
    single absence is what lets the surface treat "inserted it for you" and "nothing to show" as
    the same non-event.
    """

    text: str = ""
    caret: int = 0
    candidates: tuple[str, ...] = ()
    #: The name that was inserted, when one was. This is the difference between a ``Tab`` that
    #: completed something and one that found nothing, and a screen that cannot tell them apart
    #: cannot say which one it just did.
    completed: str = ""
    #: Where the completed word begins in ``text``, so a caller can say what changed.
    start: int = 0
    #: The directory the candidates live in, quoted when the panel has to explain itself.
    parent: str = ""
    #: Whether ``parent`` could not be read at all — no such directory, or no permission — which is
    #: a different sentence from "nothing inside it matches what you typed".
    unreadable: bool = False


def _list(parent: str, stub: str) -> tuple[tuple[str, ...], bool]:
    """The directories inside ``parent`` whose names begin with ``stub``.

    ``complete -d``'s rule about hidden names is kept: a directory whose name begins with a dot is
    offered only once the operator has typed the dot, so a panel never opens on ``.cache`` — and a
    dot typed on purpose is answered with the hidden names it was asking for. ``.`` and ``..`` are
    offered at neither prefix, because the current directory and the one above it are not answers
    to "which workspace?", whatever bash would have done with them.
    """
    try:
        rows = os.scandir(parent)
    except OSError:
        return ((), True)
    asking = stub.startswith(".")
    with rows:
        names = [
            entry.name + DIRECTORY_MARKER
            for entry in rows
            if entry.name.startswith(stub)
            and entry.name not in (".", "..")
            and (asking or not entry.name.startswith("."))
            and _is_directory(entry)
        ]
    return (tuple(sorted(names, key=str.casefold)), False)


def _is_directory(entry: os.DirEntry) -> bool:
    """Whether a directory entry is a directory, links included, without falling over.

    ``is_dir()`` follows the link, which is what ``complete -d`` does — a symlinked directory is a
    directory you can be placed inside. A dangling link raises, and a dangling link is not a
    workspace, so the exception is the answer.
    """
    try:
        return entry.is_dir()
    except OSError:
        return False


def completions(text: str, caret: int | None = None) -> Completion:
    """What ``Tab`` means at this position in this field.

    The word being completed is the whole run of characters around ``caret`` that lies between two
    ``/``s — the boundary readline finds, since ``/`` separates one filename from the next and is
    not itself a word break. So the characters to the right of the caret count as what was typed,
    and are replaced rather than kept: completing in the middle of a long path repairs it instead
    of doubling part of it back into the value.

    The rules, in order, are bash's:

    * nothing to say about it — the parent is missing or unreadable — and the panel says so; *
    exactly one match — insert it, with its trailing slash, and list nothing; * several matches —
    insert only what they agree on, then list all of them, which is ``complete`` followed by
    ``show-all-if-ambiguous``: the prefix is free information, and the list is what distinguishes
    the rest.
    """
    at = len(text) if caret is None else max(0, min(int(caret), len(text)))
    start = text.rfind("/", 0, at) + 1
    found = text.find("/", at)
    end = len(text) if found < 0 else found
    parent = text[:start] or "/"
    stub = text[start:end]
    names, unreadable = _list(parent, stub)
    if unreadable or not names:
        return Completion(
            text=text,
            caret=at,
            candidates=names,
            start=start,
            parent=parent,
            unreadable=unreadable,
        )
    if len(names) == 1:
        # The word is settled, so the marker goes in with it and the next Tab lists inside it. One
        # candidate is never listed: a panel holding the single line the insertion has just
        # consumed says nothing the field does not already say.
        return _insert(text, start, end, parent, names[0], remaining=())
    # Only what every candidate agrees on, which is never a whole name and never ends in a marker:
    # several directories cannot share a trailing slash they did not both type.
    agreed = common_prefix(names)
    if len(agreed) <= len(stub):
        # Nothing to add that the candidates do not already disagree about — the list is the
        # answer.
        return Completion(text=text, caret=at, candidates=names, start=start, parent=parent)
    return _insert(text, start, end, parent, agreed, remaining=names)


def _insert(
    text: str, start: int, end: int, parent: str, inserted: str, *, remaining: tuple[str, ...]
) -> Completion:
    """Rebuild the field with one word replaced, everything outside that word untouched.

    The marker is dropped when the field already has one. A completed word that ran up to an
    existing ``/`` would otherwise leave two behind, and a doubled separator is the one thing in a
    path that every tool accepts silently and no tool reads the same way twice.
    """
    if inserted.endswith(DIRECTORY_MARKER) and text[end : end + 1] == DIRECTORY_MARKER:
        inserted = inserted[: -len(DIRECTORY_MARKER)]
    return Completion(
        text=text[:start] + inserted + text[end:],
        caret=start + len(inserted),
        candidates=remaining,
        completed=inserted,
        start=start,
        parent=parent,
    )


def common_prefix(names: tuple[str, ...]) -> str:
    """What several candidates agree on, which is what bash inserts before it lists them."""
    if not names:
        return ""
    first, last = min(names), max(names)
    for index, character in enumerate(first):
        if character != last[index]:
            return first[:index]
    return first
