#!/usr/bin/env python3
"""How many modal screens each step of this launch is going to show.

The step rail counts screens, and a total that moves is not a progress bar. Most of the declared
sequence usually has no question to ask — an environment that already names its model, a host with
no compatible module, provider keys already sitting in ``.env`` — so a rail that assumes every
declared step will be seen tells the first screen of a two-question launch that it is the second of
five. The only way to know better before anything is drawn is to ask the same questions the steps
will ask, which is what this does, for the whole sequence, in one process.

Every answer comes from the launcher's own predicate rather than a restatement of it, because a
forecast that drifts from the real short-circuit is worse than no forecast at all: the rail would
be wrong in a way that looks deliberate. :func:`tools.models.prompts` decides a model lane,
``HARNESS_MODULES`` and the compatibility probe decide the module list, and
:func:`tools.policy.resolve` plus :func:`tools.used_credentials` decide whether a key is missing.

Three rules make this safe to call from the launcher on every launch.

*Read-only.* Nothing is written and no request is made. The one command run is a module's own
``compatible`` probe, which the module step runs anyway moments later.

*Absent, not guessed.* A step this cannot answer is left off the line rather than rounded up or
down, which leaves :data:`tools.tui.flow.DEFAULT_SCREENS` in charge — the figure every launch used
before there was a forecast. Several steps are answered from *every* outcome at once rather than
from the one the user will pick: keys that already satisfy every candidate model promise away the
credential screen even while the model question is still open.

*Never fatal.* Any failure is one fewer answer and a zero exit status. A progress bar is not a
reason to stop a launch, and the caller wraps this in ``|| true`` and means it.
"""

from __future__ import annotations

import argparse
import os
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

if __package__:
    from . import models as model_step
    from . import modules as module_step
    from . import policy
    from .definitions import load_definitions
    from .env_values import read_env_values
    from .tui import flow
else:
    import policy
    from definitions import load_definitions
    from env_values import read_env_values
    from tui import flow

    import models as model_step
    import modules as module_step

#: The variable that answers the Kimi Code version question without asking it, named in
#: ``scripts/select_versions.py`` beside the menu it short-circuits.
KIMI_VERSION_ENV = "KIMI_CODE_VERSION"

#: A read that has not been attempted yet, which is not the same fact as a read that failed.
_UNTRIED = object()


class Survey:
    """The reads several answers share, each taken at most once and each allowed to fail."""

    def __init__(self, root: Path, runtime: Path) -> None:
        self.root = root
        self.runtime = runtime
        self._memo: dict[str, Any] = {}

    def _once(self, name: str, produce: Callable[[], Any]) -> Any:
        """Produce a shared fact once, and remember ``None`` as its own answer.

        A catalog that will not load, a ``.env`` that is absent, and a compatibility probe that
        could not run are all the same thing to a rail: this step is unknown. Letting the error
        out would take every other step's answer down with it.
        """
        if name not in self._memo:
            try:
                self._memo[name] = produce()
            except (OSError, ValueError, subprocess.SubprocessError):
                self._memo[name] = None
        return self._memo[name]

    @property
    def definitions(self) -> tuple[dict, list[dict]] | None:
        """The operator's provider and model catalog, or ``None`` when it will not load."""
        return self._once("definitions", lambda: load_definitions(self.root))

    @property
    def values(self) -> dict[str, str] | None:
        """The resolved launcher environment: which names have a value, never what it is."""
        return self._once("values", lambda: model_step.bootstrap_values(self.root))

    @property
    def declared(self) -> set[str] | None:
        """Variable names the workspace's own ``.env`` mentions, as the module step reads them."""
        return self._once("declared", lambda: set(read_env_values(self.root / ".env")))

    @property
    def previous_selection(self) -> dict[str, str]:
        """Last launch's model ids, which are only ever the defaults this launch offers.

        Never ``None``: an absent or unreadable selection leaves the picker on its first candidate,
        which is a fact the forecast can still use.
        """
        stored = self._once(
            "previous",
            lambda: model_step.read_json(self.runtime / model_step.PREVIOUS_SELECTION, {}),
        )
        return stored if isinstance(stored, dict) else {}

    @property
    def modules(self) -> list[dict] | None:
        """The loadable modules on this host, which is the list the module step would show."""
        return self._once(
            "modules",
            lambda: [
                module
                for module in module_step.discover(self.root)
                if module_step.compatible(module)
            ],
        )


def lane_forecast(survey: Survey, lane: str) -> tuple[bool | None, str]:
    """Whether this model lane will take a screen, and the id it would have defaulted to.

    The default is :func:`tools.models.choose`'s own rule — last launch's answer while it is still
    offered, otherwise the first candidate — because a lane that is not going to ask still has an
    answer, and the credential forecast needs it.
    """
    definitions = survey.definitions
    if not definitions:
        return (None, "")
    _, model_list = definitions
    choices = model_step.selectable(model_list, lane)
    override = os.environ.get(f"HARNESS_{lane.upper()}_MODEL", "").strip()
    # An unattended launch is not on the other side of this call: the launcher surveys only when
    # it has started a flow, and it starts a flow only when it means to ask.
    asking = model_step.prompts(override, False, choices)
    if override:
        return (asking, override)
    by_id = {model["id"] for model in choices}
    previous = str(survey.previous_selection.get(lane, ""))
    return (asking, previous if previous in by_id else (choices[0]["id"] if choices else ""))


def selected_modules(survey: Survey) -> list[dict] | None:
    """Which modules will be loaded, or ``None`` while that is still a question."""
    candidates = survey.modules
    if candidates is None:
        return None
    if not candidates:
        return []
    override = os.environ.get("HARNESS_MODULES")
    if override is None:
        return None
    wanted = set(filter(None, override.split(",")))
    return [module for module in candidates if module["id"] in wanted]


def pending_module_values(survey: Survey, modules: list[dict]) -> int | None:
    """How many environment questions these modules still need answered.

    ``tools/modules.py`` counts them from the declarations and the two layers of operator
    configuration together, and a step that is asked three questions is three screens, so the
    count is the answer rather than a yes or no.
    """
    values, declared = survey.values, survey.declared
    if values is None or declared is None:
        return None
    pending = 0
    seen: set[str] = set()
    for module in modules:
        for item in module.get("environment", []):
            name = item["name"]
            if name in seen:
                continue
            seen.add(name)
            if name in declared and values.get(name, ""):
                continue
            pending += 1
    return pending


def unanswered_credentials(survey: Survey, selections: list[dict[str, str]]) -> set[str] | None:
    """Every key none of these selections could avoid asking for.

    Resolving a plan is the only way to know which credential a model authenticates with, since a
    model that named its own variable and found nothing in it gets a key of its own. So this does
    resolve plans, in memory, against the same values ``tools/models.py resolve`` will read.
    ``None`` means no plan could be built and nothing is known.
    """
    definitions = survey.definitions
    values = survey.values
    if not definitions or values is None:
        return None
    providers, model_list = definitions
    models = {model["id"]: model for model in model_list}
    try:
        reserved = model_step.reserved_context_size(survey.root)
    except (OSError, ValueError):
        return None
    missing: set[str] = set()
    for selection in selections:
        if not all(selection.values()):
            return None
        try:
            plan = policy.resolve(
                model_step.apply_endpoint_overrides(providers, values),
                models,
                selection,
                reserved_context_size=reserved,
                key_values=values,
            )
            used = model_step.used_credentials(plan)
        except (OSError, ValueError):
            return None
        missing.update(item["env"] for item in used if not values.get(item["env"], "").strip())
    return missing


def answer_model(survey: Survey) -> int | None:
    return _answer_lane(survey, "primary")


def answer_subagent(survey: Survey) -> int | None:
    return _answer_lane(survey, "subagent")


def _answer_lane(survey: Survey, lane: str) -> int | None:
    asking, _ = lane_forecast(survey, lane)
    return None if asking is None else int(asking)


def answer_modules(survey: Survey) -> int | None:
    """The module list is on screen unless there is nothing to list or the operator answered it."""
    if os.environ.get("HARNESS_MODULES") is not None:
        return 0
    candidates = survey.modules
    return None if candidates is None else int(bool(candidates))


def answer_module_version(survey: Survey) -> int | None:
    """No module loaded means no version asked of it; anything else is a question not yet asked.

    A module's version menu is a shell hook run per selected module, and the only honest way to
    know whether one exists is to source the shell that looks for it. Guessing from filenames here
    is how a forecast starts lying, so a launch that still has modules to choose keeps the default.
    """
    chosen = selected_modules(survey)
    if chosen is None or chosen:
        return None
    return 0


def answer_module_values(survey: Survey) -> int | None:
    chosen = selected_modules(survey)
    if chosen is not None:
        return pending_module_values(survey, chosen)
    every = pending_module_values(survey, survey.modules or [])
    return 0 if every == 0 else None


def answer_kimi_version(survey: Survey) -> int | None:
    """One screen, unless a version has already been named, which is the whole of the rule."""
    del survey
    return 0 if os.environ.get(KIMI_VERSION_ENV, "").strip() else 1


def answer_context(survey: Survey) -> int | None:
    """The context panel always asks when a flow is running.

    It prints instead of prompting for an unattended launch or an explicit ``--show``, and this
    survey is reached by neither: the launcher runs it only after it has begun a flow.
    """
    del survey
    return 1


def answer_credentials(survey: Survey) -> int | None:
    """The keys the chosen models will still need, which is one screen per unanswered name.

    With both lanes settled the plan is knowable exactly, so the count is exact. With a lane still
    to be asked it is answered the other way round instead: resolve every candidate model in turn
    and, if none of them could ask for anything, promise the step away. Anything else stays silent,
    because a credential screen is the last thing in a launch and a forecast that missed it would
    move the total under the user's eyes.
    """
    lanes: dict[str, tuple[bool, str]] = {}
    for lane in model_step.SELECTABLE:
        asking, chosen = lane_forecast(survey, lane)
        if asking is None:
            return None
        lanes[lane] = (asking, chosen)
    projection = {lane: chosen for lane, (_, chosen) in lanes.items()}
    if not any(asking for asking, _ in lanes.values()):
        missing = unanswered_credentials(survey, [projection])
        return None if missing is None else len(missing)
    candidates = []
    for lane in model_step.SELECTABLE:
        for model in _candidates(survey, lane):
            candidates.append({**projection, lane: model})
    missing = unanswered_credentials(survey, candidates)
    return 0 if missing == set() else None


def _candidates(survey: Survey, lane: str) -> list[str]:
    definitions = survey.definitions
    if not definitions:
        return []
    _, model_list = definitions
    return [model["id"] for model in model_step.selectable(model_list, lane)]


#: One answer per step name, keyed by the flow's own constants so a rename cannot leave a step
#: quietly unforecast. A name the sequence gains later needs an entry here to be counted.
ANSWERERS: dict[str, Callable[[Survey], int | None]] = {
    flow.MODEL: answer_model,
    flow.SUBAGENT: answer_subagent,
    flow.MODULES: answer_modules,
    flow.KIMI_VERSION: answer_kimi_version,
    flow.MODULE_VERSION: answer_module_version,
    flow.MODULE_VALUES: answer_module_values,
    flow.CONTEXT: answer_context,
    flow.CREDENTIALS: answer_credentials,
}


def counts(root: Path, runtime: Path, steps: tuple[str, ...]) -> dict[str, int]:
    """Forecast every step in ``steps``, dropping the ones nothing here can answer."""
    survey = Survey(root, runtime)
    out: dict[str, int] = {}
    for name in steps:
        answer = ANSWERERS.get(name)
        if answer is None:
            continue
        try:
            value = answer(survey)
        except Exception:  # noqa: BLE001 - a missing answer is the contract, whatever went wrong
            value = None
        if value is not None:
            out[name] = max(0, int(value))
    return out


def format_counts(values: dict[str, int], steps: tuple[str, ...]) -> str:
    """Render a forecast as one line, in the order the operator will meet the steps."""
    return ",".join(f"{name}={values[name]}" for name in steps if name in values)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="flow_survey.py", description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path)
    parser.add_argument("--runtime-dir", type=Path)
    parser.add_argument("--steps", default=",".join(flow.STEPS))
    args = parser.parse_args(argv)
    root = args.root or Path(os.environ.get("HARNESS_ROOT") or Path.cwd())
    runtime = args.runtime_dir or Path(os.environ.get("HARNESS_RUNTIME_DIR") or ".")
    steps = tuple(name for name in args.steps.split(",") if name)
    line = format_counts(counts(root, runtime, steps), steps)
    if line:
        print(line)
    return 0


if __name__ == "__main__":  # pragma: no cover - the launcher's call, not the suite's
    try:
        raise SystemExit(main())
    except Exception:  # noqa: BLE001 - see the module docstring: this call cannot fail a launch
        raise SystemExit(0) from None
