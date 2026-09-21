#!/usr/bin/env python3
"""Per-launch flow state: what has been answered, and where ``Backspace`` should land.

Every step of the launch sequence is its own process, because that is what keeps a crash in a
picker from taking the launcher with it, and what lets ``start.sh`` stay the single owner of the
build. The cost of separate processes is that "go back one step" has no meaning inside any of
them, so this file is where the answer lives: an ordered list of steps, which ones have committed,
and the one step a back-navigation picked as its target.

Two properties are load-bearing.

*Replaying a committed step is free.* Going back to the model picker and forward again must not
re-fetch GitHub releases, must not re-ask for a token, and above all must not re-run a step that
mutates the workspace. A step that is committed and is not the current target exits immediately
with the answer it already gave, so Back costs one screen swap rather than one relaunch.

*Nothing secret is ever stored here.* The answers themselves stay in the runtime files that own
them — ``model-selection.json``, ``session.env``, ``prompt-context.json`` — and this file keeps
step names plus a short summary for the recap line. It is written 0600, atomically, and deleted at
the commit point.

Exit statuses are defined here rather than in ``app.py`` so that the shell, the engine, and this
state machine quote the same three numbers. ``130`` is deliberately not repurposed: Ctrl-C has to
reach ``start.sh``'s ``trap cleanup`` with its usual meaning intact, which is what releases the
launcher lock and removes the bootstrap temp file.

A step is asked three separate things here, and they have separate answers, because a step that has
an answer is not necessarily a step that shows a screen. ``status()`` asks whether it has an answer,
which is what decides replay. ``declare()`` says how many screens it will render and ``skip()`` says
it will render none — both feed ``rail()``, whose numbers are the only part of all this the user
sees. And ``previous()`` walks the sequence to find where Backspace goes, over the steps that
really showed something.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from typing import NoReturn

#: Accept and move on.
CONTINUE = 0
#: The user asked for the previous step. Its own code rather than a field on a result, because it
#: has to cross a process boundary and stay legible in the shell without parsing stdout.
GO_BACK = 3
#: Ctrl-C. Never reused for anything a user might do on purpose.
ABORTED = 130

#: Name of the state file inside the instance runtime directory.
FILE_NAME = "flow-state.json"

#: A launch that never entered the modal flow has no file, and that is not an error.
MISSING = "new"
#: Already answered, and not where this back-navigation is going: replay it, do not render it.
COMMITTED = "committed"
#: Already answered, but this is the step the user went back to: it renders.
TARGET = "target"
#: The screen count of a step that never declared one.
DEFAULT_SCREENS = 1

#: The interactive sequence, in the order the operator meets it. One name per screen the launcher
#: can know about in advance — the two model lanes are two steps because they are two screens, and
#: a module's version menu is one step however many modules ask for one, because the step counts
#: its own screens when it runs.
#:
#: This is the canonical list so the shell and the surfaces cannot disagree about the order, and so
#: ``--steps`` stays an override for tests rather than a second copy of the truth. The names are
#: constants because a surface typos a step name in silence: it simply numbers nothing and shows
#: nothing, which looks like a flow that was never entered.
MODEL = "model"
SUBAGENT = "subagent"
MODULES = "modules"
KIMI_VERSION = "kimi-version"
MODULE_VERSION = "module-version"
MODULE_VALUES = "module-values"
CONTEXT = "context"
CREDENTIALS = "credentials"
STEPS = (
    MODEL,
    SUBAGENT,
    MODULES,
    KIMI_VERSION,
    MODULE_VERSION,
    MODULE_VALUES,
    CONTEXT,
    CREDENTIALS,
)


class BackRequested(Exception):
    """The user asked for an earlier step, and the code running this one can actually provide it.

    Raised rather than returned because a picker whose answer is a model id has nothing to answer
    *with*, and an empty string would be read as a choice of the empty model. A step that is its own
    process has no earlier step in it, so it lets the status cross the boundary instead — see
    :data:`GO_BACK`.
    """

    def __init__(self, step: str = "") -> None:
        super().__init__(f"back from {step}" if step else "back requested")
        self.step = step


def path_for(runtime_dir: str | os.PathLike[str]) -> str:
    """Where the state lives for a given instance runtime directory."""
    return os.path.join(os.fspath(runtime_dir), FILE_NAME)


def is_live(runtime_dir: str | os.PathLike[str]) -> bool:
    """Whether a modal flow is in progress right now.

    The one consumer that needs the answer in-process is ``tools/modules.py``, which must not run
    ``docker image rm`` while a flow could still walk back over it. It reads the flag rather than
    shelling out, so the check cannot be skipped by a caller that forgot to.
    """
    state = _read(path_for(runtime_dir))
    return bool(state and state.get("live"))


def running(runtime_dir: str | os.PathLike[str]) -> Flow | None:
    """The flow that is live right now, or ``None`` when this launch has no sequence to join.

    Surfaces take their step-rail numbers from here rather than counting their own screens, and
    short-circuit to the numbers they would have shown alone when there is no flow — a picker run by
    hand, or a launch that never entered the modal sequence, still has to render.
    """
    return Flow(runtime_dir) if is_live(runtime_dir) else None


def back_from(state: Flow | None, step: str) -> NoReturn:
    """Hand a back-request to the flow, then leave the process the way the launcher reads one.

    A surface that cannot step back within itself has to cross the process boundary to do it, and
    the one thing it must not do is leave without telling the flow where the user wanted to go: the
    target is what makes that step render again rather than replay. Resolving it here rather than at
    each call site is what keeps a back-request from going quiet because one surface forgot the half
    of it that is not an exit code.
    """
    if state is not None:
        state.go_back(step)
    raise SystemExit(GO_BACK)


def _read(path: str) -> dict | None:
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (FileNotFoundError, NotADirectoryError, IsADirectoryError):
        return None
    except (OSError, ValueError):
        # A file we cannot parse is a file we know nothing about. Failing closed here would strand
        # a launch on a corrupt scratch file, and failing open would replay a step that never
        # committed, so the answer is to treat it as absent and let the next write replace it.
        return None
    return data if isinstance(data, dict) else None


def _write(path: str, state: dict) -> None:
    """Replace the state file, keeping it private to the user.

    ``tempfile`` in the same directory, then ``os.replace``: the launcher reads this between steps,
    and a half-written file would be read as "nothing committed" and re-prompt the whole flow.
    """
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=directory, prefix=".flow-", suffix=".tmp", delete=False
    )
    try:
        os.chmod(handle.name, 0o600)
        json.dump(state, handle, separators=(",", ":"), sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
        handle.close()
        os.replace(handle.name, path)
    except BaseException:
        handle.close()
        _unlink(handle.name)
        raise


def _unlink(name: str) -> None:
    try:
        os.unlink(name)
    except OSError:
        pass


def _screens(raw: object) -> dict[str, int]:
    """Coerce a stored screen map, because a rail cannot afford a non-integer."""
    return _counts(raw, floor=1)


def _counts(raw: object, *, floor: int = 0) -> dict[str, int]:
    """Coerce a stored name-to-screen map.

    ``floor`` is the whole difference between the two maps this reads. A *declared* count is one the
    surface gave while rendering, so a zero there is a corruption rather than an answer, and it is
    rounded up. A *surveyed* count is a forecast of what a step has not reached yet, and zero is its
    most useful value: it is how the launcher says "this step will not be on screen" before the step
    itself ever runs, which is what keeps the rail's total from changing under the user.
    """
    out: dict[str, int] = {}
    if isinstance(raw, dict):
        for name, count in raw.items():
            try:
                out[str(name)] = max(floor, int(count))
            except (TypeError, ValueError):
                continue
    return out


def parse_counts(text: str) -> dict[str, int]:
    """Read a ``name=screens`` list, which is how the shell hands the pass's forecast over.

    Anything unparseable is dropped rather than raised: the launcher's own ``|| true`` on this call
    says a forecast nobody could read is a rail with no forecast, and the step's own count is always
    still there to correct it. A launch must not fail because a progress bar was guessed at.
    """
    out: dict[str, int] = {}
    for piece in text.split(","):
        name, _, raw = piece.partition("=")
        try:
            out[name.strip()] = max(0, int(raw))
        except ValueError:
            continue
    return {name: count for name, count in out.items() if name}


class Flow:
    """The ordered step list, the answers, and the pending back target."""

    def __init__(self, runtime_dir: str | os.PathLike[str], steps: tuple[str, ...] = ()) -> None:
        self.path = path_for(runtime_dir)
        state = _read(self.path) or {}
        # The launcher passes the same list every call; a stored list that disagrees with it wins
        # only for steps it actually knows about, so an edit to the sequence mid-launch cannot make
        # an old committed step unrecognisable and silently re-prompt the user.
        declared = list(steps or state.get("steps") or ())
        stored = list(state.get("steps") or ())
        for name in stored:
            if name not in declared:
                declared.append(name)
        self.steps: tuple[str, ...] = tuple(declared)
        self.committed: dict[str, str] = dict(state.get("committed") or {})
        self.screens: dict[str, int] = _screens(state.get("screens"))
        #: The launcher's forecast, one count per step, taken once per pass before any step runs.
        self.forecast: dict[str, int] = _counts(state.get("forecast"))
        self.skipped: frozenset[str] = frozenset(str(name) for name in (state.get("skipped") or ()))
        self.target: str = str(state.get("target") or "")
        self.live: bool = bool(state.get("live"))

    # -- questions --------------------------------------------------------------------------

    def sequence(self) -> tuple[str, ...]:
        """The steps this launch will really show screens for, in order.

        ``steps`` keeps its skipped members, because the declared sequence is the shape of the
        launcher and a name is useful to the recap even when nothing rendered; it is only the
        screens that a skipped step has none of, and so the rail counts around it.
        """
        return tuple(name for name in self.steps if name not in self.skipped)

    def status(self, step: str) -> str:
        """What this step should do on arrival: render, replay, or prompt fresh."""
        if step not in self.committed:
            return MISSING
        return TARGET if step == self.target else COMMITTED

    def should_render(self, step: str) -> bool:
        """False only for a committed step that is not the back target.

        A step that was never asked (``--non-interactive``, or an override in the environment) still
        gets to commit itself, which is what keeps the recap honest: the flow records the answer,
        not the fact that a screen was shown.
        """
        return self.status(step) != COMMITTED

    def previous(self, step: str) -> str:
        """The step before ``step`` in the declared order, or ``""`` at the start of the flow.

        Only committed steps are candidates, and a skipped step is never one: landing on a screen
        that does not exist — a module's version menu when that module is deselected, say — would
        prompt for something the user is not currently choosing, and the answer would be thrown away
        at the next step.
        """
        order = self.sequence()
        if step not in order:
            return ""
        for name in reversed(order[: order.index(step)]):
            if name in self.committed:
                return name
        return ""

    def screens_for(self, step: str) -> int:
        """How many screens ``step`` contributes to the rail, which is none for an unseen step.

        Two sources answer, in order of authority. A step that has already drawn its screen counted
        itself while doing so, and nothing may overrule that — least of all a forecast made before
        it knew. A step the user has not reached is known only from this pass's forecast, and that
        is the only reason the rail can say "of 2" on the first screen of a two-screen launch: the
        alternative is to assume every declared step will be seen, which is what made the total
        fall as the user advanced and the bar ran backwards.
        """
        if step in self.skipped:
            return 0
        declared = self.screens.get(step)
        if declared is not None:
            return max(1, declared)
        return max(0, self.forecast.get(step, DEFAULT_SCREENS))

    def visible(self) -> tuple[str, ...]:
        """The steps the user will be shown this pass, in order.

        :meth:`sequence` answers a different question — which steps have not *yet* declined — and so
        still names every step that is going to refuse later in the pass. The rail numbers screens,
        not intentions, and a progress bar whose denominator moves is not a progress bar.
        """
        return tuple(name for name in self.steps if self.screens_for(name) > 0)

    def rail(self, step: str, index: int = 0) -> tuple[int, int]:
        """The ``N of M`` to show for the ``index``-th screen of ``step``.

        The launcher's sequence is the only thing that knows how many screens come *before* this
        surface, so the numbering has to be asked of the flow rather than counted locally: a picker
        that numbered itself would restart at one halfway through the run and the rail would stop
        being a map of where the user is.

        A step that is replayed instead of rendered still holds its numbers, which is the point —
        the rail describes the sequence, not the screens drawn so far. When a surface has more to
        ask than it declared, the position clamps to the total rather than printing "9 of 8", and a
        name the sequence does not contain lands at its end. A launch with nothing on screen at all
        says "1 of 1", because a rail that reads "0 of 0" answers no question anyone asked.
        """
        order = self.visible()
        total = sum(self.screens_for(name) for name in order)
        if total <= 0:
            return (1, 1)
        before = 0
        for name in order:
            if name == step:
                break
            before += self.screens_for(name)
        position = min(before + max(0, int(index)) + 1, total)
        return position, total

    # -- changes ----------------------------------------------------------------------------

    def begin(self) -> None:
        """Mark the flow live, which is what makes destructive steps refuse to run."""
        self.live = True
        self._save()

    def survey(self, counts: dict[str, int]) -> None:
        """Record, before the first step runs, how many screens each step is going to show.

        The rail's total is the one number on the screen the user cannot recompute, so it has to be
        settled once rather than revised at every step. It cannot be got from the declared sequence
        either, because most of that sequence usually has no question to ask — an environment that
        names its own model, or a launch with no modules — and a user who is shown "2 of 5" for the
        last screen of a two-screen launch has been told a lie about their own machine.

        So the launcher asks each surface, in one process and without side effects, whether it would
        prompt right now, and writes down the answer for the whole pass. A step that later counts
        itself differently has simply corrected the forecast with better information: its own number
        wins from then on, which is why the surveyed count is written only for a step that has not
        yet rendered anything. A step surveyed at zero is not on the rail at all — :meth:`skip` is
        still the step's own answer, and this is only the launcher's guess at it.
        """
        for name, count in counts.items():
            if name in self.screens:
                continue
            self.forecast[name] = max(0, int(count))
        self._save()

    def declare(self, step: str, screens: int = DEFAULT_SCREENS) -> None:
        """Say how many screens ``step`` will render, once it knows.

        Most surfaces are one screen and never call this. A version menu that has to ask per
        selected product, or a secret prompt that only learns how many keys are missing after it
        reads the plan, declares the real count so the rail's total is honest. Declaring counts as
        rendering, so it takes the step back out of the skipped set.
        """
        if step not in self.steps and step:
            self.steps = (*self.steps, step)
        self.screens[step] = max(1, int(screens))
        self.skipped = self.skipped - {step}
        self._save()

    def report(self, step: str, screens: int) -> None:
        """Say what this step has to draw, which is how a surface joins the rail.

        Every surface knows its own count only after it has worked out what it has to ask, and the
        two halves of that answer are the two halves of this call: a count to number, or a step with
        nothing in it to remove from the sequence.
        """
        if screens > 0:
            self.declare(step, screens)
        else:
            self.skip(step)

    def plan(self, step: str, screens: int = DEFAULT_SCREENS) -> bool:
        """Whether this pass renders ``step``, recording its screen count if it does.

        The question and the record are one call because they must not disagree. A step that is
        replaying draws nothing this pass and stays in the sequence — its screens were counted when
        it first ran. A step that has no question to ask at all draws nothing and leaves the
        sequence, which is what keeps "4 of 8" true. Asking a surface to tell those apart from the
        count alone is how a replay ends up deleting the answer it is replaying.

        Zero screens is therefore a refusal rather than a request: a step that reports none has
        nothing to show, so this answers ``False`` and the caller takes its un-prompted path. The
        launcher's own short-circuits depend on that — an unattended launch that found a stale
        ``live`` flag must print its record rather than try to take over a screen that is not a
        terminal, and the count it passes is the only thing distinguishing the two.
        """
        if not self.should_render(step):
            return False
        if screens < 1:
            self.skip(step)
            return False
        self.report(step, screens)
        return True

    def skip(self, step: str) -> None:
        """Take a step's screens out of the sequence, because it will render none.

        The answer goes with the screens. A step that has nothing to ask has nothing to have
        answered, and leaving a stale answer behind would let a later un-skip replay a value the
        user never re-confirmed — a module re-selected after backing up, whose token the previous
        pass wrote and the next pass did not.
        """
        self.skipped = self.skipped | {step}
        self.committed.pop(step, None)
        self.screens.pop(step, None)
        if self.target == step:
            self.target = ""
        self._save()

    def finish(self) -> None:
        """End the flow and delete the state, called at the launcher's commit point."""
        self.live = False
        self.target = ""
        _unlink(self.path)

    def commit(self, step: str, summary: str = "") -> None:
        """Record an answer. Idempotent, because a replayed step commits again on every pass."""
        if step not in self.steps and step:
            self.steps = (*self.steps, step)
        self.skipped = self.skipped - {step}
        self.committed[step] = summary
        if self.target == step:
            self.target = ""
        self._save()

    def go_back(self, step: str) -> str:
        """Resolve a back-request from ``step`` and return where to send the user.

        The target persists until that step commits again, so a step that is passed through twice
        still renders twice — it is leaving the target, not entering it, that clears it.
        """
        target = self.previous(step)
        self.target = target
        self._save()
        return target

    def forget(self, step: str) -> None:
        """Drop a step's answer, used when a change upstream invalidates it.

        Deleting the record is what makes the step prompt again on the next pass; nothing else has
        to know that it happened. Its screen count goes too, because a step that will render again
        will say how many screens it has when it gets there, and until then the pass's forecast is
        the only number anyone has — keeping the last pass's figure would number the rail with an
        answer that no longer belongs to anything on screen.
        """
        self.committed.pop(step, None)
        self.screens.pop(step, None)
        if self.target == step:
            self.target = ""
        self._save()

    def recap(self) -> tuple[tuple[str, str], ...]:
        """Committed steps in order, with their summaries — the non-interactive recap line."""
        return tuple((name, self.committed[name]) for name in self.steps if name in self.committed)

    def _save(self) -> None:
        _write(
            self.path,
            {
                "steps": list(self.steps),
                "committed": self.committed,
                "screens": self.screens,
                "forecast": self.forecast,
                "skipped": sorted(self.skipped),
                "target": self.target,
                "live": self.live,
            },
        )


def main(argv: list[str] | None = None) -> int:
    """The shell's view of the state machine.

    One word per call and a printed answer, rather than a long-lived server: the launcher already
    owns the sequence, and a step that can query the flow in one line of shell stays readable in the
    file where the flow actually is.

    ``rail`` is the one that returns two numbers, since "N of M" is one question; ``declare`` and
    ``skip`` let the shell adjust the counts once a surface knows how many screens it has; and
    ``survey`` takes the whole pass's forecast in one line, which is what the launcher calls before
    the first step runs so the rail's total never has a reason to move.
    """
    parser = argparse.ArgumentParser(prog="flow.py", description=__doc__.splitlines()[0])
    parser.add_argument("--runtime-dir", required=True)
    parser.add_argument("--steps", default="", help="Comma-separated ordered step names")
    parser.add_argument(
        "command",
        choices=(
            "status",
            "commit",
            "back",
            "begin",
            "end",
            "live",
            "recap",
            "rail",
            "declare",
            "skip",
            "survey",
        ),
    )
    parser.add_argument("step", nargs="?", default="")
    parser.add_argument("--summary", default="")
    parser.add_argument("--screens", type=int, default=DEFAULT_SCREENS)
    parser.add_argument(
        "--counts",
        default="",
        help="Comma-separated name=screens forecast, e.g. model=0,context=1",
    )
    args = parser.parse_args(argv)

    steps = tuple(name for name in args.steps.split(",") if name)
    flow = Flow(args.runtime_dir, steps)
    if args.command == "status":
        print(flow.status(args.step))
    elif args.command == "commit":
        flow.commit(args.step, args.summary)
    elif args.command == "back":
        print(flow.go_back(args.step))
    elif args.command == "begin":
        flow.begin()
    elif args.command == "end":
        flow.finish()
    elif args.command == "live":
        return 0 if flow.live else 1
    elif args.command == "rail":
        print(" ".join(str(value) for value in flow.rail(args.step)))
    elif args.command == "declare":
        flow.declare(args.step, args.screens)
    elif args.command == "skip":
        flow.skip(args.step)
    elif args.command == "survey":
        flow.survey(parse_counts(args.counts))
    else:
        for name, summary in flow.recap():
            print(f"{name}\t{summary}")
    return CONTINUE


if __name__ == "__main__":  # pragma: no cover - exercised through the launcher, not the suite
    sys.exit(main())
