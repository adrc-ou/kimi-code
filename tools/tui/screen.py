#!/usr/bin/env python3
"""The alternate screen as a resource the launcher owns for a whole run.

Each interactive step is its own process -- that is what keeps a crash in a picker from taking the
launcher with it -- so when every step borrowed the alternate screen itself the terminal left it and
re-entered it once per question. Between two steps the user watched the launcher's ordinary printout
scroll past, which reads as the interface falling over even though the next screen arrives a moment
later. A modal that vanishes to prove it can come back is not a modal.

So the borrowing moves up a level: ``start.sh`` runs ``enter`` before the flow and ``leave`` the
moment the questions are over, and every step in between runs in *held* mode, where the terminal it
touches is already the alternate screen. :class:`~tui.term.Terminal` reads :data:`HELD` and stops
writing the two escapes that would hand the window back.

Holding is not free, and the two costs are both handled here rather than in the launcher:

``enter`` only claims a screen it can give back. It is a no-op unless standard output is a terminal,
and it says so by its exit status, so the launcher can fall back to per-step borrowing instead of
painting escape codes into a log file.

``leave`` runs from the launcher's ``cleanup`` trap as well as from the happy path, so it must be
idempotent and must survive a step that died. ``enter`` records the borrow in a file for exactly
that reason: the leave at the end of the loop and the leave in the trap are the same call, and only
the first of them should write anything.
"""

from __future__ import annotations

import argparse
import os
import sys

if __package__ in (None, ""):  # pragma: no cover - launched as a script, not imported
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    from tui.cells import ALT_OFF, ALT_ON, CLEAR, HIDE_CURSOR, RESET, SHOW_CURSOR
else:
    # Imported as ``tools.tui.screen`` by the surfaces that ask whether the screen is held, where
    # the repository root -- not ``tools/`` -- is what sits on the path.
    from .cells import ALT_OFF, ALT_ON, CLEAR, HIDE_CURSOR, RESET, SHOW_CURSOR

#: The environment variable a launcher sets for the children that share its borrowed screen.
#: :class:`~tui.term.Terminal` reads it, so this module and that one agree on held mode without
#: either importing the other's lifecycle logic.
HELD = "HARNESS_TUI_SCREEN"
#: The value that means "the screen is already borrowed; do not hand it back".
HELD_VALUE = "held"
#: Recorded by ``enter`` so a later ``leave`` knows whether there is anything to give back.
#: Deliberately inside the instance runtime directory, which the launcher deletes wholesale.
CLAIM = "screen-claim"
#: Sentences said while the window belongs to the modal, kept for the launcher to read out when it
#: hands the window back. Same directory and same reasoning as :data:`CLAIM`.
NOTES = "launch-notes.log"


def held(environ: dict[str, str] | None = None) -> bool:
    """Whether this process is drawing into a screen the launcher borrowed."""
    env = os.environ if environ is None else environ
    return env.get(HELD, "").strip() == HELD_VALUE


def note(*lines: str) -> None:
    """Say one sentence, wherever the window happens to be.

    A step that prints an ordinary line while the launcher holds the alternate screen is scrolling
    the questions away, and the next frame paints over only part of it -- the launch ends up with
    the operator's answers and the previous step's summary both on screen at once. So while the
    screen is borrowed the sentence waits in :data:`NOTES` and the launcher reads it out after the
    window comes back, which is where it used to land anyway.

    Anything that cannot be spooled is printed instead. Losing the notice is bad; losing it twice,
    because the file holding it was unwritable, is worse.
    """
    directory = os.environ.get("HARNESS_RUNTIME_DIR", "").strip()
    if held() and directory:
        try:
            with open(os.path.join(directory, NOTES), "a", encoding="utf-8") as spool:
                for line in lines:
                    spool.write(line + "\n")
            return
        except OSError:
            pass
    for line in lines:
        print(line)


def claim_path(runtime_dir: str) -> str:
    return os.path.join(runtime_dir, CLAIM)


def _write(text: str) -> None:
    sys.stdout.write(text)
    sys.stdout.flush()


def _claimed(runtime_dir: str) -> bool:
    return bool(runtime_dir) and os.path.exists(claim_path(runtime_dir))


def _claim(runtime_dir: str, on: bool) -> None:
    """Record or clear the borrow, best effort.

    The claim is a hint about whether a give-back is owed, not a lock: a runtime directory that
    cannot be written means ``leave`` will not find a claim, and the launcher's own trap still
    runs, which is the only place the screen is guaranteed back.
    """
    if not runtime_dir:
        return
    try:
        if on:
            with open(claim_path(runtime_dir), "w", encoding="ascii"):
                pass
        else:
            os.unlink(claim_path(runtime_dir))
    except OSError:
        pass


def main(argv: list[str] | None = None) -> int:
    """Borrow the alternate screen, or hand it back."""
    parser = argparse.ArgumentParser(prog="screen.py", description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=("enter", "leave", "held"))
    parser.add_argument("--runtime-dir", default="")
    args = parser.parse_args(argv)

    if args.command == "held":
        # The launcher asks rather than guesses, because a terminal that cannot do the alternate
        # screen must keep its existing per-step behaviour rather than hold a plain one open.
        return 0 if held() else 1

    if not sys.stdout.isatty():
        # Piped, redirected, or a --non-interactive launch: there is no window to borrow, and
        # writing the escapes would leave control codes in whatever file captured the output.
        return 1

    if args.command == "enter":
        _write(ALT_ON + CLEAR + HIDE_CURSOR)
        _claim(args.runtime_dir, True)
        return 0

    if not _claimed(args.runtime_dir):
        # Either nothing was ever borrowed or an earlier give-back already returned it. Writing
        # ALT_OFF blindly would be harmless here and wrong for one case: a launch that never
        # entered the alternate screen but is exiting through the same trap.
        return 0
    _claim(args.runtime_dir, False)
    _write(RESET + ALT_OFF + SHOW_CURSOR)
    return 0


if __name__ == "__main__":  # pragma: no cover - a terminal is not a test fixture
    sys.exit(main())
