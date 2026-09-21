"""Exercise the two model lanes: what they ask, what they answer, and how Back moves between them.

The lanes share the modal engine, so these tests are about the launcher's half of that contract —
which lane takes the screen at all, what a row says about a model, and what going back costs. The
engine's own behaviour (decoding, scrolling, the generated legend, Backspace itself) is
``test_tui.py``'s problem; a pty run of the engine once is already covered there, and what is
specific to these two lanes is the *sequence*, which is a property of this module, not of the
terminal.
"""

import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools import models
from tools.tui import flow
from tools.tui.app import Result, View
from tools.tui.input import FieldStep
from tools.tui.menu import SINGLE

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

MODELS = [
    {
        "id": "fast",
        "label": "Fast Little Model",
        "provider": "acme",
        "lanes": ["primary", "subagent"],
    },
    {"id": "big", "label": "Big Serious Model", "provider": "acme", "lanes": ["primary"]},
    {"id": "tiny", "label": "Tiny Cheap Model", "provider": "globex", "lanes": ["subagent"]},
    {
        "id": "wide",
        "label": "Wide Context Model",
        "provider": "globex",
        "lanes": ["primary", "subagent"],
    },
]
PRIMARY = "Primary agent model"
SUBAGENT = "Subagent model"


def answer(lane, model_id):
    """A scripted result for a lane, with the summary its step would really have produced."""
    model = next(item for item in MODELS if item["id"] == model_id)
    return Result(value=model_id, summary=model["label"])


class Screen(io.StringIO):
    """A stream that claims to be a terminal and keeps what was printed on it."""

    def isatty(self) -> bool:
        return True


class ScriptedRun:
    """Stand in for ``run``: hand back the next prepared answer, and remember the asking."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.asks = []

    def __call__(self, step, view=None, **kwargs):
        self.asks.append((step, view))
        if not self.answers:
            raise AssertionError(f"the script ran out of answers at {step.title!r}")
        return self.answers.pop(0)

    @property
    def views(self):
        return [view for _, view in self.asks]

    @property
    def lanes(self):
        return [step.title for step, _ in self.asks]


class LaneTests(unittest.TestCase):
    """Whether a lane asks at all, and what its rows are made of."""

    def setUp(self):
        self.primary = models.selectable(MODELS, "primary")
        self.subagent = models.selectable(MODELS, "subagent")

    def test_a_lane_only_takes_the_screen_when_it_has_a_question_to_ask(self):
        # Every False here is a lane that answers itself, so a later step cannot offer to come back
        # to it. A True is a screen the user actually saw.
        self.assertFalse(models.prompts("fast", False, self.primary))
        self.assertFalse(models.prompts("", True, self.primary))
        self.assertFalse(models.prompts("", False, self.subagent[:1]))
        self.assertTrue(models.prompts("", False, self.primary))

    def test_the_rows_are_still_in_the_order_the_user_saw_them_before(self):
        # Cosmetic-only: the redesign moved the provider and the marks from the end of the name into
        # the hint column, but reordering the options would change what the user reads.
        self.assertEqual([model["id"] for model in self.primary], ["big", "fast", "wide"])
        self.assertEqual([model["id"] for model in self.subagent], ["fast", "tiny", "wide"])

    def test_a_row_carries_its_provider_and_at_most_one_mark(self):
        self.assertEqual(models._option(MODELS[1], "big", "big").hint, "[acme] (last used)")
        self.assertEqual(models._option(MODELS[0], "big", "fast").hint, "[acme] (default)")
        self.assertEqual(models._option(MODELS[1], "", "wide").hint, "[acme]")
        # A remembered row that is also the default is not said twice: "last used" is the news.
        row = models._option(MODELS[0], "fast", "fast")
        self.assertEqual(row.id, "fast")
        self.assertEqual(row.label, "Fast Little Model")
        self.assertEqual(row.hint, "[acme] (last used)")

    def test_an_answered_lane_never_touches_the_terminal(self):
        # No tty is faked here, which is the point: reaching the modal would raise, and a lane the
        # user cannot change must not need one.
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(
                models.choose("primary", self.primary, "big", non_interactive=True, override=""),
                "big",
            )
            self.assertEqual(
                models.choose("primary", self.primary, "", non_interactive=False, override="wide"),
                "wide",
            )
            self.assertEqual(
                models.choose(
                    "subagent", self.subagent[:1], "", non_interactive=False, override=""
                ),
                "fast",
            )

    def test_an_override_that_names_nothing_says_which_models_were_possible(self):
        with self.assertRaises(models.DefinitionError) as caught:
            models.choose("primary", self.primary, "", non_interactive=False, override="nope")
        self.assertIn("HARNESS_PRIMARY_MODEL='nope'", str(caught.exception))
        self.assertIn("big, fast, wide", str(caught.exception))

    def test_a_lane_with_nothing_in_it_is_refused_before_anything_is_drawn(self):
        with self.assertRaises(models.DefinitionError) as caught:
            models.choose("primary", [], "", non_interactive=False, override="")
        self.assertIn("no available model declares a primary lane", str(caught.exception))

    def test_no_terminal_and_no_way_to_answer_is_refused_rather_than_hanging(self):
        # --non-interactive and an override are both absent, so this is a real user at a pipe.
        with mock.patch.object(sys, "stdin", io.StringIO()):
            with mock.patch.object(sys, "stdout", io.StringIO()):
                with self.assertRaises(models.DefinitionError) as caught:
                    models.choose("primary", self.primary, "", non_interactive=False, override="")
        self.assertIn("needs a terminal", str(caught.exception))
        self.assertIn("HARNESS_PRIMARY_MODEL", str(caught.exception))
        self.assertIn("--non-interactive", str(caught.exception))


class AskTests(unittest.TestCase):
    """What one lane puts on the screen, as the step and view it hands to ``run``."""

    def setUp(self):
        self.primary = models.selectable(MODELS, "primary")

    def ask(self, *, previous="", view=None, result=None):
        script = ScriptedRun(result or answer("primary", "big"))
        with mock.patch.object(sys, "stdin", Screen()):
            with mock.patch.object(sys, "stdout", Screen()):
                with mock.patch.object(models, "run", script):
                    reply = models.choose(
                        "primary",
                        self.primary,
                        previous,
                        non_interactive=False,
                        override="",
                        view=view,
                    )
        self.assertEqual(len(script.asks), 1)
        step, given = script.asks[0][0], script.asks[0][1]
        return step, reply, given

    def test_the_step_asks_the_same_question_the_old_picker_printed(self):
        step, reply, _ = self.ask()
        self.assertEqual(reply, "big")
        self.assertEqual(step.title, PRIMARY)
        self.assertEqual(step.prompt, f"Choose the {PRIMARY}")

    def test_the_step_is_a_radio_step_that_selects_with_space(self):
        # One answer per list, so Space moves the mark instead of ticking a box, and the footer says
        # which of the two the key does. A radio that offered no key at all would leave its own mark
        # as decoration.
        step, _, _ = self.ask()
        self.assertEqual(step.mode, SINGLE)
        self.assertTrue(step.toggleable)
        self.assertEqual(step.toggle_hint, "select")

    def test_the_rows_are_the_lanes_candidates_in_the_lanes_order(self):
        step, _, _ = self.ask()
        self.assertEqual([choice.id for choice in step.choices], ["big", "fast", "wide"])
        self.assertTrue(all(choice.enabled for choice in step.choices))

    def test_the_cursor_opens_on_the_remembered_answer_and_otherwise_on_the_first_row(self):
        step, _, _ = self.ask(previous="wide")
        state = step.initial()
        self.assertEqual(state.chosen, ("wide",))
        self.assertEqual(step.option(state).id, "wide")
        step, _, _ = self.ask(previous="withdrawn-since-last-launch")
        self.assertEqual(step.initial().chosen, ("big",))

    def test_the_launcher_is_what_supplies_the_rail(self):
        wanted = View(position=2, total=9, label=PRIMARY, can_go_back=True)
        _, _, given = self.ask(view=wanted)
        self.assertIs(given, wanted)
        _, _, given = self.ask()
        self.assertEqual(given, View())

    def test_going_back_raises_rather_than_answering_with_nothing(self):
        # A model id has no sensible empty value, so Back is an exception, not "".
        script = ScriptedRun(Result(status=flow.GO_BACK))
        with mock.patch.object(sys, "stdin", Screen()):
            with mock.patch.object(sys, "stdout", Screen()):
                with mock.patch.object(models, "run", script):
                    with self.assertRaises(flow.BackRequested) as caught:
                        models.choose(
                            "primary", self.primary, "", non_interactive=False, override=""
                        )
        self.assertEqual(caught.exception.step, "primary")

    def test_ctrl_c_exits_with_the_engines_number_instead_of_going_back(self):
        script = ScriptedRun(Result(status=flow.ABORTED))
        with mock.patch.object(sys, "stdin", Screen()):
            with mock.patch.object(sys, "stdout", Screen()):
                with mock.patch.object(models, "run", script):
                    with self.assertRaises(SystemExit) as caught:
                        models.choose(
                            "primary", self.primary, "", non_interactive=False, override=""
                        )
        self.assertEqual(caught.exception.code, flow.ABORTED)


def credential(**overrides):
    """One entry of :func:`models.used_credentials`, as the resolver would have built it."""
    item = {
        "secret": "acme__default",
        "env": "ACME_API_KEY",
        "prompt": "Acme API key",
        "label": "Acme platform key",
        "key_url": "https://acme.example/keys",
        "provider": "acme",
        "provider_label": "Acme",
        "fallback_env": "",
    }
    item.update(overrides)
    return item


class CredentialTests(unittest.TestCase):
    """The key prompts: which ones are asked, in what order, and what backing up re-opens.

    These are the steps that replaced ``getpass``, and a credential is the one answer in the
    launcher that must not end up on screen, in the scrollback, or in a file the agent container
    can read. So the assertions are about the whole sequence rather than about the field: an item
    already filled in from ``.env`` costs the step rail nothing, two credentials naming one variable
    ask once, and going back re-opens the question the user asked to return to instead of sliding
    past it on the strength of the answer they had already given.
    """

    def setUp(self):
        self.screen = Screen()
        for patcher in (
            mock.patch.object(sys, "stdin", self.screen),
            mock.patch.object(sys, "stdout", self.screen),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def asked(self, *answers, items, values=None):
        """Walk ``items`` with a scripted screen, and report both what it asked and what it got."""
        script = ScriptedRun(*answers)
        with mock.patch.object(models, "run", script):
            given = models.credential_values(list(items), dict(values or {}))
        return script, given

    def written(self):
        return self.screen.getvalue()

    def test_only_a_credential_with_no_value_takes_a_screen(self):
        second = credential(
            secret="globex__default",
            env="GLOBEX_API_KEY",
            prompt="Globex API key",
            label="Globex key",
            provider="globex",
            provider_label="Globex",
        )
        script, given = self.asked(
            Result(value="one"), items=[credential(), second], values={"GLOBEX_API_KEY": "kept"}
        )
        self.assertEqual(script.lanes, ["Acme key"])
        self.assertEqual(script.views[0].total, 1)
        self.assertFalse(script.views[0].can_go_back)
        self.assertEqual(given, {"ACME_API_KEY": "one"})

    def test_the_rail_numbers_the_credentials_that_still_have_to_be_answered(self):
        items = [credential(), credential(secret="acme__second", env="ACME_SECOND_KEY")]
        script, given = self.asked(Result(value="a"), Result(value="b"), items=items)
        self.assertEqual([view.position for view in script.views], [1, 2])
        self.assertEqual([view.total for view in script.views], [2, 2])
        self.assertEqual([view.can_go_back for view in script.views], [False, True])
        self.assertEqual([view.label for view in script.views], ["Acme key", "Acme key"])
        self.assertEqual(given, {"ACME_API_KEY": "a", "ACME_SECOND_KEY": "b"})

    def test_backing_up_reopens_the_question_the_user_came_back_to(self):
        items = [credential(), credential(secret="acme__second", env="ACME_SECOND_KEY")]
        script, given = self.asked(
            Result(value="first"),
            Result(status=flow.GO_BACK),
            Result(value="first again"),
            Result(value="second"),
            items=items,
        )
        # The second screen asked to go back, so the first question is open again — asked a second
        # time rather than skipped because an answer for it was still lying around.
        self.assertEqual([view.position for view in script.views], [1, 2, 1, 2])
        self.assertEqual([view.can_go_back for view in script.views], [False, True, False, True])
        self.assertEqual(given, {"ACME_API_KEY": "first again", "ACME_SECOND_KEY": "second"})

    def test_two_credentials_naming_one_variable_ask_once(self):
        items = [credential(), credential(secret="acme__other", env="ACME_API_KEY")]
        script, given = self.asked(Result(value="one"), items=items)
        self.assertEqual(len(script.asks), 1)
        self.assertEqual(script.views[0].total, 1)
        self.assertEqual(given, {"ACME_API_KEY": "one"})

    def test_the_credential_screen_is_a_hidden_field_naming_where_the_key_is_issued(self):
        script = ScriptedRun(Result(value="  secret  "))
        with mock.patch.object(models, "run", script):
            value = models.ask_credential(credential(), View(position=1, total=1, label="Acme key"))
        step = script.asks[0][0]
        self.assertIsInstance(step, FieldStep)
        self.assertTrue(step.masked)
        # A credential is stripped before it is stored, so an answer of only spaces is no answer.
        self.assertFalse(step.whitespace_is_value)
        self.assertEqual(value, "secret")
        self.assertEqual(step.title, "Acme key")
        self.assertEqual(step.prompt, "Acme API key (ACME_API_KEY)")
        self.assertEqual(
            step.head[0], "Acme: Acme platform key (issued at https://acme.example/keys)."
        )
        # Where to persist it is still said in the scrollback, in the words the old prompt used.
        self.assertIn(
            "Set ACME_API_KEY in .env to persist it; this value is for this session only.",
            self.written(),
        )

    def test_a_model_scoped_key_names_both_variables_that_would_satisfy_it(self):
        script = ScriptedRun(Result(value="x"))
        with mock.patch.object(models, "run", script):
            models.ask_credential(credential(fallback_env="ACME_ANY_KEY"), View())
        self.assertIn(
            "Set ACME_API_KEY for this model alone, or ACME_ANY_KEY for every model of Acme "
            "in .env to persist it",
            self.written(),
        )

    def test_a_key_is_refused_rather_than_asked_when_no_terminal_is_attached(self):
        with mock.patch.object(Screen, "isatty", return_value=False):
            with self.assertRaises(models.DefinitionError) as caught:
                models.ask_credential(credential(fallback_env="ACME_ANY_KEY"), View())
        self.assertIn(
            "Acme needs a key for Acme platform key (issued at https://acme.example/keys): "
            "set ACME_API_KEY for this model alone, or ACME_ANY_KEY for every model of Acme "
            "in .env for non-interactive startup",
            str(caught.exception),
        )

    def test_a_prompted_value_lands_in_the_secret_file_and_nowhere_else(self):
        directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory, True)
        runtime = directory / "runtime"
        declaration = {
            "env": "ACME_API_KEY",
            "prompt": "Acme API key",
            "label": "Acme platform key",
            "key_url": "",
        }
        plan = {
            "lanes": {"primary": {"provider": "acme", "credential": "default"}},
            "providers": {"acme": {"label": "Acme", "credentials": {"default": declaration}}},
        }
        script = ScriptedRun(Result(value="from the screen"))
        with mock.patch.object(models, "run", script):
            models.materialise_credentials(plan, runtime, {})
        target = runtime / models.CREDENTIALS_DIR / "acme__default"
        self.assertEqual(target.read_text(), "from the screen")
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        self.assertNotIn("from the screen", self.written())


class SelectTests(unittest.TestCase):
    """The two lanes as one sequence: rails, Back, and what survives going back."""

    def setUp(self):
        directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory, True)
        self.root = directory / "project"
        self.runtime = self.root / "runtime"
        self.runtime.mkdir(parents=True)
        self.screen = Screen()
        self.stdin = mock.patch.object(sys, "stdin", Screen())
        self.stdout = mock.patch.object(sys, "stdout", self.screen)
        self.definitions = mock.patch.object(
            models, "load_definitions", lambda root: (None, MODELS)
        )
        for patcher in (self.stdin, self.stdout, self.definitions):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.environment = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("HARNESS_", "COLORTERM", "NO_COLOR"))
        }

    def select(self, *answers, **overrides):
        script = ScriptedRun(*answers)
        with mock.patch.dict(os.environ, {**self.environment, **overrides}, clear=True):
            with mock.patch.object(models, "run", script):
                code = models.cmd_select(self.root, self.runtime, non_interactive=False)
        return code, script

    def written(self):
        return self.screen.getvalue()

    def selection(self):
        return json.loads((self.runtime / models.SELECTION).read_text())

    def test_the_two_lanes_are_one_step_each_and_only_the_second_can_go_back(self):
        code, script = self.select(answer("primary", "big"), answer("subagent", "fast"))
        self.assertEqual(code, 0)
        self.assertEqual(script.lanes, [PRIMARY, SUBAGENT])
        self.assertEqual([view.position for view in script.views], [1, 2])
        self.assertEqual([view.total for view in script.views], [2, 2])
        self.assertEqual([view.can_go_back for view in script.views], [False, True])
        self.assertEqual([view.label for view in script.views], [PRIMARY, SUBAGENT])

    def test_each_answer_is_still_written_out_in_the_scrollback(self):
        # start.sh reads these lines in its non-interactive printout, so the modal must not be the
        # only place an answer appears.
        self.select(answer("primary", "big"), answer("subagent", "wide"))
        self.assertIn("Primary agent model: Big Serious Model [acme]", self.written())
        self.assertIn("Subagent model: Wide Context Model [globex]", self.written())

    def test_a_remembered_answer_is_still_said_to_be_remembered(self):
        (self.runtime / models.PREVIOUS_SELECTION).write_text(json.dumps({"primary": "wide"}))
        self.select(answer("primary", "wide"), answer("subagent", "fast"))
        self.assertIn(
            "Primary agent model: Wide Context Model [globex] (last used)", self.written()
        )
        # The other lane was never remembered, so it must not borrow the suffix.
        self.assertNotIn("(last used)", self.written().split("Subagent model:")[1])

    def test_a_broken_previous_selection_is_treated_as_no_previous_selection(self):
        (self.runtime / models.PREVIOUS_SELECTION).write_text(json.dumps(["not", "a", "mapping"]))
        code, _ = self.select(answer("primary", "big"), answer("subagent", "fast"))
        self.assertEqual(code, 0)
        self.assertEqual(self.selection(), {"primary": "big", "subagent": "fast"})

    def test_back_re_asks_the_first_lane_and_the_second_answer_is_not_kept(self):
        code, script = self.select(
            answer("primary", "big"),
            Result(status=flow.GO_BACK),
            answer("primary", "wide"),
            answer("subagent", "tiny"),
        )
        self.assertEqual(code, 0)
        self.assertEqual(script.lanes, [PRIMARY, SUBAGENT, PRIMARY, SUBAGENT])
        self.assertEqual([view.position for view in script.views], [1, 2, 1, 2])
        selection = self.selection()
        self.assertEqual(selection, {"primary": "wide", "subagent": "tiny"})
        # Lane order, not answer order: the resolver and the proxy read this document.
        self.assertEqual(list(selection), ["primary", "subagent"])

    def test_going_back_leaves_one_answer_line_per_lane_and_it_names_the_final_answer(self):
        # Two lines that cannot both be true is worse than a line late: the superseded answer must
        # not reach the scrollback at all.
        self.select(
            answer("primary", "big"),
            Result(status=flow.GO_BACK),
            answer("primary", "wide"),
            answer("subagent", "tiny"),
        )
        self.assertEqual(self.written().count("Primary agent model:"), 1)
        self.assertIn("Primary agent model: Wide Context Model [globex]", self.written())
        self.assertNotIn("Big Serious Model", self.written())
        self.assertEqual(
            self.written().splitlines(),
            [
                "Primary agent model: Wide Context Model [globex]",
                "Subagent model: Tiny Cheap Model [globex]",
            ],
        )

    def test_back_is_not_offered_when_the_earlier_lane_was_answered_for_the_user(self):
        # The point of prompts(): stepping "back" to a lane that shows the same screen twice is a
        # dead key, and a dead key is what the brief forbids.
        code, script = self.select(answer("subagent", "fast"), HARNESS_PRIMARY_MODEL="wide")
        self.assertEqual(code, 0)
        self.assertEqual(script.lanes, [SUBAGENT])
        self.assertIs(script.views[0].can_go_back, False)
        self.assertEqual(self.selection(), {"primary": "wide", "subagent": "fast"})
        self.assertIn("Primary agent model: Wide Context Model [globex]", self.written())

    def test_an_overridden_lane_never_takes_the_screen(self):
        code, script = self.select(answer("primary", "big"), HARNESS_SUBAGENT_MODEL="tiny")
        self.assertEqual(code, 0)
        self.assertEqual(script.lanes, [PRIMARY])
        self.assertEqual(self.selection(), {"primary": "big", "subagent": "tiny"})

    def test_back_from_the_first_lane_cannot_loop(self):
        # can_go_back is False there, so the engine cannot produce GO_BACK. If a step ever did,
        # cmd_select re-asks the same lane rather than sliding off the front of the sequence.
        code, script = self.select(
            Result(status=flow.GO_BACK),
            answer("primary", "big"),
            answer("subagent", "fast"),
        )
        self.assertEqual(code, 0)
        self.assertEqual([view.position for view in script.views], [1, 1, 2])

    def test_a_lane_with_no_candidates_is_refused_before_the_first_screen(self):
        only_subagent = [model for model in MODELS if model["lanes"] == ["subagent"]]
        with mock.patch.object(models, "load_definitions", lambda root: (None, only_subagent)):
            with mock.patch.dict(os.environ, self.environment, clear=True):
                with self.assertRaises(models.DefinitionError) as caught:
                    models.cmd_select(self.root, self.runtime, non_interactive=False)
        self.assertIn("no available model declares a primary lane", str(caught.exception))
        self.assertFalse((self.runtime / models.SELECTION).exists())


if __name__ == "__main__":
    unittest.main()
