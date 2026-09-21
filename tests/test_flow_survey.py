"""Forecast the launch sequence: which steps will really show a screen, and how many.

The rail's total is the one number on the modal the user cannot check, so these tests are about the
two ways it can lie. A step that will not ask may not be enumerated (a two-question launch that
opens with "1 of 5" is the defect this exists to fix), and a step that is left off the line must be
left off *because nothing here knows*, not because a guess was rounded. Where the forecaster has to
answer for a choice the user has not made yet it does so over every possible outcome at once, and
that is the interesting part of the contract: an answer here is trusted by a screen drawn before
the question it forecasts.

The launcher's predicates are the real ones - :func:`tools.models.prompts`, the module environment's
de-duplication, the credential count - so the doubles stop at the boundary where a fact about the
terminal, an operator file, or a provider plan would have to be invented.
"""

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools import flow_survey
from tools.tui import flow

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

#: Two candidates for one lane and one for the other, which is the smallest catalog that tells the
#: two lanes apart: a lane with nothing to choose between asks nothing.
MODELS = [
    {
        "id": "fast",
        "label": "Fast Little Model",
        "provider": "acme",
        "lanes": ["primary", "subagent"],
    },
    {"id": "big", "label": "Big Serious Model", "provider": "acme", "lanes": ["primary"]},
]
#: The key the plan double hands back for a lane unless a test says otherwise. Two models sharing
#: one variable is the normal case, and the one that makes the credential step one screen rather
#: than one screen per model.
SHARED_KEY = "ACME_KEY"


def module(identifier, *names):
    """A module manifest, in the shape ``tools/modules.py`` hands the environment questions to."""
    return {
        "id": identifier,
        "label": identifier.title(),
        "environment": [{"name": name, "prompt": f"{name}?"} for name in names],
    }


def boom(*args, **kwargs):
    raise ValueError("nothing here works")


class SurveyTests(unittest.TestCase):
    def setUp(self):
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.root = Path(holder.name) / "project"
        self.runtime = self.root / "runtime"
        self.runtime.mkdir(parents=True)
        self.write(".env", "")
        self.environment = {
            key: value for key, value in os.environ.items() if not key.startswith("HARNESS_")
        }
        self.values = {}
        self.keys = {}
        self.selections = []
        self.patch(flow_survey.model_step, "reserved_context_size", lambda root: 1000)
        self.patch(flow_survey.model_step, "bootstrap_values", lambda root: self.values)
        self.patch(flow_survey.model_step, "used_credentials", self.used)
        self.patch(flow_survey.policy, "resolve", self.resolve)
        self.catalog(MODELS)
        self.shelf([])

    # -- fixtures ----------------------------------------------------------------------------

    def patch(self, target, name, replacement):
        patcher = mock.patch.object(target, name, replacement)
        patcher.start()
        self.addCleanup(patcher.stop)

    def catalog(self, models):
        self.patch(flow_survey, "load_definitions", lambda root: ({}, models))

    def shelf(self, modules, *, compatible=True):
        self.patch(flow_survey.module_step, "discover", lambda root: modules)
        self.patch(flow_survey.module_step, "compatible", lambda item: compatible)

    def write(self, relative, text):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def resolve(self, providers, models, selection, **kwargs):
        """Stand in for a policy plan by remembering the selection it was asked about.

        Which credential a selection ends up needing is ``tools/policy.py``'s business and is tested
        there; what matters here is *which selections the forecaster was willing to promise away*,
        because that is the part that has to be right about a choice nobody has made yet.
        """
        self.selections.append(dict(selection))
        return dict(selection)

    def used(self, plan):
        return [
            {"env": self.keys.get((lane, plan[lane]), SHARED_KEY)}
            for lane in ("primary", "subagent")
        ]

    def key_for(self, lane, identifier, name):
        self.keys[(lane, identifier)] = name

    def survey(self, *, values=None, previous=None, **overrides):
        """Answer the whole sequence against these fixtures, with the environment pinned."""
        self.values = {} if values is None else values
        if previous is not None:
            self.write(
                "runtime/" + flow_survey.model_step.PREVIOUS_SELECTION,
                json.dumps(previous),
            )
        with mock.patch.dict(os.environ, {**self.environment, **overrides}, clear=True):
            return flow_survey.counts(self.root, self.runtime, flow.STEPS)

    def asked(self, step, **kwargs):
        return self.survey(**kwargs).get(step)

    # -- the two model lanes -----------------------------------------------------------------

    def test_a_lane_with_something_to_choose_is_one_screen(self):
        self.assertEqual(self.asked(flow.MODEL), 1)

    def test_a_lane_with_nothing_to_choose_between_asks_nothing(self):
        # One candidate is not a question, and a step that asks nothing is not a screen - which is
        # how a launch begins at "1 of" rather than at the number of its name in the sequence.
        self.catalog([MODELS[0]])
        self.assertEqual(self.asked(flow.MODEL), 0)

    def test_the_two_lanes_are_answered_apart(self):
        # One process asks both, but the user meets them as two screens, and only one of them has
        # anything to choose here.
        answers = self.survey()
        self.assertEqual((answers[flow.MODEL], answers[flow.SUBAGENT]), (1, 0))

    def test_a_model_named_by_the_environment_never_takes_the_screen(self):
        self.assertEqual(self.asked(flow.MODEL, HARNESS_PRIMARY_MODEL="fast"), 0)

    def test_a_lane_answered_for_the_user_still_supplies_its_answer(self):
        # The credential forecast needs both lanes settled, so an override has to feed the
        # projection rather than only switching a screen off.
        answers = self.survey(HARNESS_PRIMARY_MODEL="fast")
        self.assertEqual(self.selections, [{"primary": "fast", "subagent": "fast"}])
        self.assertEqual(answers[flow.CREDENTIALS], 1)

    def test_a_lane_with_nothing_in_it_says_nothing_about_the_last_step(self):
        # An empty lane is a broken catalog, and the model step refuses on it rather than asking.
        # Nothing downstream of that is knowable, so the credential step stays a guess.
        self.catalog([{"id": "solo", "label": "Solo", "provider": "acme", "lanes": ["subagent"]}])
        answers = self.survey()
        self.assertEqual(answers[flow.MODEL], 0)
        self.assertNotIn(flow.CREDENTIALS, answers)

    def test_last_launchs_answer_is_the_one_the_forecast_projects(self):
        # The picker opens on the remembered model, so the remembered model is the one whose key
        # the last step needs if the user never touches anything.
        self.key_for("primary", "fast", "OTHER_KEY")
        answers = self.survey(values={SHARED_KEY: "x"}, previous={"primary": "fast"})
        self.assertIn({"primary": "fast", "subagent": "fast"}, self.selections)
        # ...and the candidate it did not open on still has to be looked at, because that one is
        # one keystroke away and needs a key nobody has supplied.
        self.assertNotIn(flow.CREDENTIALS, answers)
        self.assertIn({"primary": "big", "subagent": "fast"}, self.selections)

    # -- the module list, its versions, and its values ---------------------------------------

    def test_the_module_step_asks_only_when_there_is_something_to_choose(self):
        self.assertEqual(self.asked(flow.MODULES), 0)
        self.shelf([module("comfyui")])
        self.assertEqual(self.asked(flow.MODULES), 1)

    def test_a_host_that_cannot_load_a_module_has_nothing_to_ask_about_one(self):
        # Three steps of the sequence exist only because of modules, and on a machine without any
        # the rail has to know that before the first screen is drawn rather than subtracting from
        # the total twice as it went.
        self.shelf([module("comfyui")], compatible=False)
        answers = self.survey()
        self.assertEqual(
            (answers[flow.MODULES], answers[flow.MODULE_VERSION], answers[flow.MODULE_VALUES]),
            (0, 0, 0),
        )

    def test_a_module_list_the_operator_answered_asks_nothing(self):
        self.shelf([module("comfyui", "COMFYUI_REPOSITORY")])
        self.assertEqual(self.asked(flow.MODULES, HARNESS_MODULES="comfyui"), 0)

    def test_the_version_menu_stays_a_guess_while_the_choice_is_still_open(self):
        # Which modules will be loaded is the user's next answer, and both remaining module steps
        # are asked per selected module, so nothing known here can say how many screens follow --
        # not even for the one module on the shelf, whose question only gets asked if the user
        # leaves it ticked. Silence keeps the launch's own honest default of one; a guess would be
        # a number nobody could check.
        self.shelf([module("comfyui", "ONE")])
        answers = self.survey()
        self.assertEqual(answers[flow.MODULES], 1)
        self.assertNotIn(flow.MODULE_VERSION, answers)
        self.assertNotIn(flow.MODULE_VALUES, answers)

    def test_an_answered_module_list_says_whether_its_values_need_asking(self):
        self.shelf([module("comfyui", "COMFYUI_REPOSITORY")])
        self.write(".env", "COMFYUI_REPOSITORY=https://example.invalid\n")
        answers = self.survey(
            HARNESS_MODULES="comfyui", values={"COMFYUI_REPOSITORY": "https://example.invalid"}
        )
        self.assertEqual(answers[flow.MODULE_VALUES], 0)
        # Declared in the file but left empty is not an answer, exactly as the step reads it.
        self.assertEqual(self.asked(flow.MODULE_VALUES, HARNESS_MODULES="comfyui"), 1)

    def test_the_value_step_is_counted_in_screens_not_in_declarations(self):
        # Two unset variables are two screens, and the rail has to say so before the first one.
        self.shelf([module("comfyui", "ONE", "TWO")])
        self.assertEqual(self.asked(flow.MODULE_VALUES, HARNESS_MODULES="comfyui"), 2)

    def test_two_modules_naming_one_variable_ask_it_once(self):
        self.shelf([module("one", "SHARED"), module("two", "SHARED", "OWN")])
        self.assertEqual(self.asked(flow.MODULE_VALUES, HARNESS_MODULES="one,two"), 2)

    def test_a_value_no_module_could_have_declared_keeps_its_step_off_the_rail(self):
        # The choice is still open, but every candidate on this host is already answered, so no
        # answer the user can give will produce a question. That much is knowable without asking.
        self.shelf([module("one", "SHARED"), module("two")])
        self.write(".env", "SHARED=x\n")
        answers = self.survey(values={"SHARED": "x"})
        self.assertEqual(answers[flow.MODULE_VALUES], 0)
        self.assertEqual(answers[flow.MODULES], 1)

    def test_a_module_override_naming_nothing_this_host_can_load_answers_no_screens(self):
        # The step itself refuses on that, so what the rail says about it is moot - but a moot
        # answer is still an answer this process has to give without raising.
        self.shelf([module("comfyui", "ONE")])
        answers = self.survey(HARNESS_MODULES="other")
        self.assertEqual(
            (answers[flow.MODULES], answers[flow.MODULE_VERSION], answers[flow.MODULE_VALUES]),
            (0, 0, 0),
        )

    # -- the steps that are the same on every machine ----------------------------------------

    def test_the_kimi_version_menu_is_a_screen_unless_a_version_is_already_named(self):
        self.assertEqual(self.asked(flow.KIMI_VERSION), 1)
        self.assertEqual(self.asked(flow.KIMI_VERSION, KIMI_CODE_VERSION="1.2.3"), 0)

    def test_the_context_panel_always_asks_when_a_flow_is_running(self):
        # It prints instead of prompting only for an unattended launch, and an unattended launch
        # starts no flow and so runs no survey.
        self.assertEqual(self.asked(flow.CONTEXT), 1)

    # -- the last step: the keys the chosen models will still need ----------------------------

    def test_a_key_the_environment_already_has_leaves_the_last_step_off_the_rail(self):
        # The common case on a host that has been launched before, and the reason the total would
        # otherwise be one too high on every screen above it.
        self.assertEqual(self.asked(flow.CREDENTIALS, values={SHARED_KEY: "already set"}), 0)

    def test_a_missing_key_is_counted_before_anything_upstream_of_it_is_asked(self):
        # Both lanes here have nothing to choose, so the plan is settled and the count is a fact
        # rather than a floor.
        self.catalog([MODELS[0]])
        self.assertEqual(self.asked(flow.CREDENTIALS), 1)

    def test_two_lanes_naming_one_key_ask_once(self):
        self.catalog([MODELS[0]])
        self.assertEqual(self.asked(flow.CREDENTIALS), 1)

    def test_two_lanes_naming_two_keys_are_two_screens(self):
        self.catalog([MODELS[0]])
        self.key_for("subagent", "fast", "OTHER_KEY")
        self.assertEqual(self.asked(flow.CREDENTIALS), 2)

    def test_a_model_that_still_has_to_be_chosen_keeps_the_screen_it_might_need(self):
        # The primary lane is about to ask, and one of its candidates needs a key nobody supplied.
        # The answer now depends on a keystroke, so it is not given here.
        self.key_for("primary", "big", "BIG_KEY")
        answers = self.survey(values={SHARED_KEY: "already set"})
        self.assertEqual(answers[flow.MODEL], 1)
        self.assertNotIn(flow.CREDENTIALS, answers)
        self.assertIn({"primary": "big", "subagent": "fast"}, self.selections)

    def test_a_key_that_satisfies_every_candidate_promises_the_screen_away(self):
        # The inverse, and the case that makes the last step vanish on a configured host even
        # though the model question above it is still open.
        answers = self.survey(values={SHARED_KEY: "already set"})
        self.assertEqual(answers[flow.MODEL], 1)
        self.assertEqual(answers[flow.CREDENTIALS], 0)

    # -- the shape of the contract -----------------------------------------------------------

    def test_a_step_nothing_can_answer_is_left_off_rather_than_guessed(self):
        self.patch(flow_survey, "load_definitions", boom)
        self.shelf([module("comfyui")])
        answers = self.survey()
        for step in (flow.MODEL, flow.SUBAGENT, flow.CREDENTIALS):
            self.assertNotIn(step, answers)
        # A catalog that will not load says nothing about the module list or the context panel, so
        # the steps that could still be answered are answered, and the rest fall back one at a
        # time rather than all at once.
        self.assertEqual(answers[flow.MODULES], 1)
        self.assertEqual(answers[flow.CONTEXT], 1)

    def test_an_answerer_that_raises_costs_only_its_own_answer(self):
        # The guard is per step rather than around the whole pass, so a forecaster with a bug in
        # one answer still tells the rail about the seven it does not.
        with mock.patch.dict(flow_survey.ANSWERERS, {flow.KIMI_VERSION: boom}):
            answers = self.survey()
        self.assertNotIn(flow.KIMI_VERSION, answers)
        self.assertEqual(answers[flow.CONTEXT], 1)

    def test_the_line_names_the_steps_in_the_order_the_operator_meets_them(self):
        self.shelf([module("comfyui")])
        self.assertEqual(
            flow_survey.format_counts(self.survey(values={SHARED_KEY: "already set"}), flow.STEPS),
            "model=1,subagent=0,modules=1,kimi-version=1,module-values=0,context=1,credentials=0",
        )

    def test_a_forecast_of_nothing_prints_nothing(self):
        self.patch(flow_survey, "counts", lambda root, runtime, steps: {})
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = flow_survey.main(["--root", str(self.root)])
        self.assertEqual((code, buffer.getvalue()), (0, ""))

    def test_the_forecast_leaves_the_tree_it_read_exactly_as_it_found_it(self):
        # Read-only is not a manner of speaking. This runs against the operator's own directory
        # before any step has, and a stray file here is a stray file in the next launch too.
        self.shelf([module("comfyui", "ONE")])
        before = sorted(p.relative_to(self.root) for p in self.root.rglob("*"))
        self.survey(HARNESS_MODULES="comfyui")
        self.assertEqual(before, sorted(p.relative_to(self.root) for p in self.root.rglob("*")))

    def test_the_script_cannot_fail_a_launch_however_broken_the_tree_is(self):
        # Run the way ``start.sh`` runs it - the real file, its own interpreter, a directory that
        # is not a harness at all - because the launcher's ``|| true`` is written to tolerate a
        # nothing, not to tolerate a traceback in the user's way.
        missing = self.root / "not-a-harness"
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "tools" / "flow_survey.py"),
                "--root",
                str(missing),
                "--runtime-dir",
                str(missing),
            ],
            capture_output=True,
            text=True,
            check=False,
            env=self.environment,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertIn("context=1", result.stdout)
        self.assertEqual(result.stdout.count("\n"), 1)

    def test_a_real_launch_is_forecast_without_touching_a_real_launch(self):
        # Same thing against this repository's own catalog and modules, which is the only way to
        # catch the forecaster and the steps drifting apart: a fixture keeps both honest about the
        # fixture, and this harness ships real definitions worth reading.
        runtime = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, runtime, True)
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "tools" / "flow_survey.py"),
                "--root",
                str(ROOT),
                "--runtime-dir",
                str(runtime),
            ],
            capture_output=True,
            text=True,
            check=False,
            env={**self.environment, "HARNESS_MODULES": ""},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(list(runtime.iterdir()), [])
        self.assertTrue(result.stdout.strip(), result.stderr)
        pairs = dict(piece.split("=") for piece in result.stdout.strip().split(","))
        self.assertEqual(set(pairs) & set(flow.STEPS), set(pairs))
        self.assertTrue(all(value.isdigit() for value in pairs.values()))

    # -- and what the rail does with it ------------------------------------------------------

    def test_a_two_question_launch_numbers_its_two_screens_from_the_first(self):
        # The defect, end to end: a host with one model, no loadable module and no key to ask for
        # has two screens in it, and the first one has to say so. Before there was a forecast this
        # same launch opened on "1 of 8" and counted down as it went.
        self.catalog([MODELS[0]])
        answers = self.survey(values={SHARED_KEY: "already set"})
        self.assertEqual(answers[flow.CREDENTIALS], 0)
        state = flow.Flow(self.runtime, flow.STEPS)
        state.begin()
        state.survey(answers)
        self.assertEqual(state.visible(), (flow.KIMI_VERSION, flow.CONTEXT))
        self.assertEqual(state.rail(flow.KIMI_VERSION), (1, 2))
        self.assertEqual(state.rail(flow.CONTEXT), (2, 2))


if __name__ == "__main__":
    unittest.main()
