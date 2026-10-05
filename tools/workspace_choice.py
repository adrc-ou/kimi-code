#!/usr/bin/env python3
"""Ask which directory this launch should work in, and print it.

Runs ahead of everything else the launcher does, because the answer is what the rest of the launch
is keyed on: the instance identity digests the canonical workspace path, so the runtime directory,
the image tags, the Compose project and the state volumes all follow from it. Nothing that needs
those can run first, which is why this is a step of its own rather than a member of the modal flow
— and why the list of previously used directories cannot live in the instance directory it is about
to choose.

One promise to :file:`start.sh`: standard output carries exactly one line, the chosen path, and
nothing else, because the launcher reads it with a command substitution and anything else on that
stream would become part of the directory name. The two channels are therefore deliberately
different places during a launch: what the operator is told goes to standard error, the question
is drawn on the terminal the keyboard belongs to, and only the answer is printed. Standard input
decides whether asking is possible at all, since it is a terminal exactly when someone is there to
type into one, while standard output is a pipe for the whole of an ordinary launch — so gating the
question on *it* would mean never asking. Every other step can spool its sentences through
:mod:`tui.screen`, because the launcher owns the window they are said in; this one runs before the
launcher owns anything.

The answer is also written to the registry, which is how the read-only entry points
(``./shell.sh``, ``./extensions.sh``, ``./prompts.sh``) and ``--non-interactive`` know what the
last interactive launch picked.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

if __package__:
    from . import workspace_registry as registry
    from .tui import flow
    from .tui.app import View, run
    from .tui.term import Terminal
    from .tui.workspaces import WorkspaceStep
else:  # run as a script by the launcher, which puts ``tools/`` alone on the path
    import workspace_registry as registry
    from tui import flow
    from tui.app import View, run
    from tui.term import Terminal
    from tui.workspaces import WorkspaceStep

#: Said once above the list, because what the answer costs is not obvious from a path.
CAUTION = "The session reads and writes everything inside this directory, and nothing else here."

#: What an unattended launch has to be told when there is nothing to fall back on. The remedy is
#: one interactive run, and saying so is the whole of the guidance: a remembered list is a record
#: of having been asked, and there is no way to make one from here without the question.
EMPTY = (
    "No workspace has been chosen for this checkout yet. Run ./start.sh once without "
    "--non-interactive to pick one."
)

#: The same refusal for a launch that was never asked because it had no terminal to ask with. Here
#: ``--non-interactive`` is not the reason there is no answer, so naming it would send the operator
#: to run the very command they ran; what they need is a keyboard and a screen.
NO_TERMINAL = (
    "No workspace has been chosen for this checkout yet, and this launch has no terminal to ask "
    "with. Run ./start.sh from a terminal to pick one."
)

#: Said on standard error when a launch could not be asked but still has a remembered answer to use.
REMEMBERED = "No terminal to ask with; using the remembered workspace."

#: The controlling terminal, which is the fallback drawing surface when standard input's own device
#: name cannot be recovered — see :func:`_terminal_paths`. A process attached to no controlling
#: terminal simply fails to open it, and the launch takes the remembered answer.
TTY_PATH = "/dev/tty"


def protected(root: Path) -> tuple[str, ...]:
    """The directories this workspace must not swallow.

    The checkout holds :file:`.env` and the whole generated runtime tree, and the home directory
    holds whatever the account keeps there. Both are already refused by the launcher after this
    screen closes, so the screen is given the same two names and refuses them where the operator
    can still do something about it.
    """
    return (str(root), str(Path.home()))


def _terminal_paths() -> tuple[str, ...]:
    """The devices worth drawing on, best answer first.

    Standard input's own terminal leads, because that is where the keystrokes answering the question
    come from: it is the screen the operator is looking at, and it is reachable even for a process
    with no controlling terminal at all. :data:`TTY_PATH` backs it up for a stdin whose device name
    cannot be recovered.
    """
    paths: list[str] = []
    try:
        paths.append(os.ttyname(sys.stdin.fileno()))
    except (OSError, ValueError, AttributeError):
        pass
    paths.append(TTY_PATH)
    return tuple(dict.fromkeys(paths))


def _screen_stream():
    """The stream the question is drawn on, or ``None`` when there is no question to draw.

    Asking needs both a keyboard and a window. The keyboard is standard input, which the launcher
    never redirects, and a launch without it has nobody to answer however the output is plumbed.
    The window is standard output when that is a terminal — the case for anyone running this by
    hand — and otherwise the terminal the keyboard belongs to, which is the case for every launch
    through :file:`start.sh`, whose command substitution has already made a pipe of standard output.

    A launch with a keyboard and no window at all — a stdin that is a terminal nobody can write to —
    takes the remembered answer rather than blocking on keystrokes nobody can see a prompt for.
    """
    if not sys.stdin.isatty():
        return None
    if sys.stdout.isatty():
        return sys.stdout
    for path in _terminal_paths():
        try:
            return open(path, "w", encoding="utf-8", buffering=1)
        except OSError:
            continue
    return None


def _ask(root: Path, recent: list[str], stream) -> str:
    """Render the screen on ``stream`` and return the directory it settled on.

    ``on_remove`` goes straight to the registry, so a row leaves the persistent list in the same
    keystroke that takes it off the screen. The two going out of step is the failure the question
    on the screen exists to prevent.
    """

    def forget(path: str) -> None:
        registry.remove(root, path)

    step = WorkspaceStep(
        recent=recent, head=(CAUTION,), on_remove=forget, reserved=protected(root)
    )
    result = run(step, View(), terminal=Terminal(stream=stream, title=step.title))
    if not result.accepted:
        raise SystemExit(result.status)
    return str(result.value)


def choose(root: Path, *, stream=None, remedy: str = EMPTY) -> str:
    """The workspace for this launch: the one it is asked for, or the one it remembers.

    ``stream`` is the window the question is drawn on, and ``None`` means nobody is asked: such a
    launch takes the head of the remembered list, which is the only answer that means something
    without a keyboard — it is the directory the operator chose the last time they were asked. With
    nothing remembered there is nothing to guess at, and the launch stops and says why in the words
    :data:`remedy` calls for.

    Only an answer that was actually given is recorded. A launch that re-used a choice instead of
    making one leaves the history alone rather than pushing its own date to the top of it.
    """
    if stream is None:
        remembered = registry.newest(root)
        if not remembered:
            raise SystemExit(remedy)
        return remembered
    chosen = _ask(root, [entry.path for entry in registry.load(root)], stream)
    try:
        return registry.touch(root, chosen).path
    except ValueError as error:  # pragma: no cover - the screen rejects these before answering
        raise SystemExit(str(error)) from None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="workspace_choice.py", description=__doc__.splitlines()[0]
    )
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="take the remembered workspace instead of asking for one",
    )
    args = parser.parse_args(argv)
    # The two ways a launch goes unasked want two different remedies: one refused the question on
    # purpose, and can be answered by not refusing it next time; the other has no terminal to
    # answer it with, and no flag of ours will produce one.
    stream = None if args.non_interactive else _screen_stream()
    remedy = EMPTY if args.non_interactive else NO_TERMINAL
    # Everything the operator is told goes to standard error, because standard output is the
    # launcher's. A launch that was not asked and has an answer to use says so; one with no answer
    # gets the remedy instead, from :func:`choose`, without this sentence on top of it.
    if stream is None and not args.non_interactive and registry.newest(args.root):
        print(REMEMBERED, file=sys.stderr)
    try:
        path = choose(args.root, stream=stream, remedy=remedy)
    except KeyboardInterrupt:
        return flow.ABORTED
    finally:
        # A window opened here is closed here: standard output was never the screen during a launch,
        # so nothing else in the process will ever see that handle.
        if stream is not None and stream is not sys.stdout:
            stream.close()
    print(path)
    return flow.CONTINUE


if __name__ == "__main__":  # pragma: no cover - a screen is not a test fixture
    sys.exit(main())
