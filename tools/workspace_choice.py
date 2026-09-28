#!/usr/bin/env python3
"""Ask which directory this launch should work in, and print it.

Runs ahead of everything else the launcher does, because the answer is what the rest of the launch
is keyed on: the instance identity digests the canonical workspace path, so the runtime directory,
the image tags, the Compose project and the state volumes all follow from it. Nothing that needs
those can run first, which is why this is a step of its own rather than a member of the modal flow
— and why the list of previously used directories cannot live in the instance directory it is about
to choose.

One promise to :file:`start.sh`: standard output carries exactly one line, the chosen path, and
nothing else. That is why what the operator is told goes to standard error. Every other step can
spool its sentences through :mod:`tui.screen`, because the launcher owns the window they are said
in; this one runs before the launcher owns anything, and its stdout is a pipe the launcher is
already reading.

The answer is also written to the registry, which is how the read-only entry points
(``./shell.sh``, ``./extensions.sh``, ``./prompts.sh``) and ``--non-interactive`` know what the
last interactive launch picked.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

if __package__:
    from . import workspace_registry as registry
    from .tui import flow
    from .tui.app import View, run
    from .tui.workspaces import WorkspaceStep
else:  # run as a script by the launcher, which puts ``tools/`` alone on the path
    import workspace_registry as registry
    from tui import flow
    from tui.app import View, run
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


def protected(root: Path) -> tuple[str, ...]:
    """The directories this workspace must not swallow.

    The checkout holds :file:`.env` and the whole generated runtime tree, and the home directory
    holds whatever the account keeps there. Both are already refused by the launcher after this
    screen closes, so the screen is given the same two names and refuses them where the operator
    can still do something about it.
    """
    return (str(root), str(Path.home()))


def _ask(root: Path, recent: list[str]) -> str:
    """Render the screen and return the directory it settled on.

    ``on_remove`` goes straight to the registry, so a row leaves the persistent list in the same
    keystroke that takes it off the screen. The two going out of step is the failure the question
    on the screen exists to prevent.
    """

    def forget(path: str) -> None:
        registry.remove(root, path)

    step = WorkspaceStep(
        recent=recent, head=(CAUTION,), on_remove=forget, reserved=protected(root)
    )
    result = run(step, View())
    if not result.accepted:
        raise SystemExit(result.status)
    return str(result.value)


def choose(root: Path, *, interactive: bool) -> str:
    """The workspace for this launch: the one it is asked for, or the one it remembers.

    An unattended launch takes the head of the remembered list, which is the only answer that means
    something without a keyboard — it is the directory the operator chose the last time they were
    asked. With nothing remembered there is nothing to guess at, and the launch stops and says so.

    Only an answer that was actually given is recorded. An unattended launch re-uses a choice
    instead of making one, so it leaves the history alone rather than pushing its own date to the
    top of it.
    """
    if not interactive:
        remembered = registry.newest(root)
        if not remembered:
            raise SystemExit(EMPTY)
        return remembered
    chosen = _ask(root, [entry.path for entry in registry.load(root)])
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
    # The screen needs both ends of the pipe it is drawn into, and a launch with only one of them
    # is a launch that piped its log. It takes the remembered answer, exactly as
    # ``--non-interactive`` would have, and says so once — on standard error, because standard
    # output is the launcher's.
    interactive = not args.non_interactive and sys.stdin.isatty() and sys.stdout.isatty()
    if not interactive and not args.non_interactive:
        print("No terminal to ask with; using the remembered workspace.", file=sys.stderr)
    try:
        path = choose(args.root, interactive=interactive)
    except KeyboardInterrupt:
        return flow.ABORTED
    print(path)
    return flow.CONTINUE


if __name__ == "__main__":  # pragma: no cover - a screen is not a test fixture
    sys.exit(main())
