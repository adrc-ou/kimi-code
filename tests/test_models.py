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
PRIMARY = "Main agent model"
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

    def test_the_primary_screen_asks_which_model_the_main_agent_should_run(self):
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
        self.assertIn(
            "Main agent model: Big Serious Model - Medium (concurrent) [acme]", self.written()
        )
        self.assertIn("Subagent model: Wide Context Model [globex]", self.written())

    def test_a_remembered_answer_is_still_said_to_be_remembered(self):
        (self.runtime / models.PREVIOUS_SELECTION).write_text(json.dumps({"primary": "wide"}))
        self.select(answer("primary", "wide"), answer("subagent", "fast"))
        self.assertIn(
            "Main agent model: Wide Context Model - Medium (concurrent) [globex] (last used)",
            self.written(),
        )
        # The other lane was never remembered, so it must not borrow the suffix.
        self.assertNotIn("(last used)", self.written().split("Subagent model:")[1])

    def test_a_broken_previous_selection_is_treated_as_no_previous_selection(self):
        (self.runtime / models.PREVIOUS_SELECTION).write_text(json.dumps(["not", "a", "mapping"]))
        code, _ = self.select(answer("primary", "big"), answer("subagent", "fast"))
        self.assertEqual(code, 0)
        # No recorded window survives a document that is not a mapping, and no window means the
        # native one: a broken memory may lose a preference, not launch the wrong lane.
        self.assertEqual(
            self.selection(), {"primary": "big", "subagent": "fast", "agent_lane": "primary"}
        )

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
        self.assertEqual(
            selection, {"primary": "wide", "subagent": "tiny", "agent_lane": "primary"}
        )
        # Lane order, not answer order: the resolver and the proxy read this document. The window
        # rides at the end so those readers see the two lanes first.
        self.assertEqual(list(selection), ["primary", "subagent", "agent_lane"])

    def test_going_back_leaves_one_answer_line_per_lane_and_it_names_the_final_answer(self):
        # Two lines that cannot both be true is worse than a line late: the superseded answer must
        # not reach the scrollback at all.
        self.select(
            answer("primary", "big"),
            Result(status=flow.GO_BACK),
            answer("primary", "wide"),
            answer("subagent", "tiny"),
        )
        self.assertEqual(self.written().count("Main agent model:"), 1)
        self.assertIn(
            "Main agent model: Wide Context Model - Medium (concurrent) [globex]", self.written()
        )
        self.assertNotIn("Big Serious Model", self.written())
        self.assertEqual(
            self.written().splitlines(),
            [
                "Main agent model: Wide Context Model - Medium (concurrent) [globex]",
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
        self.assertEqual(
            self.selection(), {"primary": "wide", "subagent": "fast", "agent_lane": "primary"}
        )
        self.assertIn(
            "Main agent model: Wide Context Model - Medium (concurrent) [globex]", self.written()
        )

    def test_an_overridden_lane_never_takes_the_screen(self):
        code, script = self.select(answer("primary", "big"), HARNESS_SUBAGENT_MODEL="tiny")
        self.assertEqual(code, 0)
        self.assertEqual(script.lanes, [PRIMARY])
        self.assertEqual(
            self.selection(), {"primary": "big", "subagent": "tiny", "agent_lane": "primary"}
        )

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


class WindowTests(unittest.TestCase):
    """The extended-window row: how it is offered, named, answered, and remembered.

    These use a definition-shaped ``lanes`` table (``{"primary": {...}, "long": {...}}``) because
    that is what ``load_definitions`` really returns and the window size in the row's name comes
    out of it. One test keeps the flat list shape the other classes use, which is the case where
    the launcher knows a long lane exists but not how big it is.
    """

    def setUp(self):
        directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory, True)
        self.root = directory / "project"
        self.runtime = self.root / "runtime"
        self.runtime.mkdir(parents=True)
        self.screen = Screen()
        self.models = [
            {
                "id": "roomy",
                "label": "Roomy Model",
                "provider": "acme",
                "lanes": {
                    "primary": {"context_tokens": 262144},
                    "long": {"context_tokens": 1000000},
                    "subagent": {"context_tokens": 65536},
                },
            },
            {
                "id": "modest",
                "label": "Modest Model",
                "provider": "globex",
                "lanes": {
                    "primary": {"context_tokens": 131072},
                    "long": {"context_tokens": 524288},
                },
            },
        ]
        self.definitions = mock.patch.object(
            models, "load_definitions", lambda root: (None, self.models)
        )
        self.stdin = mock.patch.object(sys, "stdin", Screen())
        self.stdout = mock.patch.object(sys, "stdout", self.screen)
        for patcher in (self.definitions, self.stdin, self.stdout):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.environment = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("HARNESS_", "COLORTERM", "NO_COLOR"))
        }

    def pick(self, row_id):
        """A scripted answer naming one of this fixture's rows, twins included."""
        return Result(value=row_id, summary=row_id)

    def select(self, *answers, **overrides):
        script = ScriptedRun(*answers)
        with mock.patch.dict(os.environ, {**self.environment, **overrides}, clear=True):
            with mock.patch.object(models, "run", script):
                code = models.cmd_select(self.root, self.runtime, non_interactive=False)
        return code, script

    def selection(self):
        return json.loads((self.runtime / models.SELECTION).read_text())

    def test_each_model_with_a_long_lane_gets_one_more_row_than_it_used_to_have(self):
        # The queued wide-window row leads its pair because that is the lane a fresh launch opens
        # on, and the two rows for one model are read together rather than sorted apart.
        rows = models.lane_rows(self.models, "primary")
        self.assertEqual(
            [row["id"] for row in rows],
            ["modest@long", "modest", "roomy@long", "roomy"],
        )
        self.assertEqual(
            [row["id"] for row in models.lane_rows(self.models, "subagent")], ["roomy"]
        )

    def test_the_twin_is_named_by_the_window_its_own_definition_declares(self):
        labels = {row["id"]: row["label"] for row in models.lane_rows(self.models, "primary")}
        self.assertEqual(labels["roomy@long"], "Roomy Model - Long (queued, 1M window)")
        # Not every long lane is a megabyte, and the row that says so must not be a string
        # somebody has to remember to edit.
        self.assertEqual(labels["modest@long"], "Modest Model - Long (queued, 512k window)")
        # Both rows name their own trade, since the screen offers them as peers and not as a
        # model plus a footnote.
        self.assertEqual(labels["roomy"], "Roomy Model - Medium (concurrent)")

    def test_a_lane_table_without_sizes_still_offers_the_row(self):
        # The shape the other classes in this file use: a long lane is named but not measured, so
        # the row loses its size and the choice itself survives.
        flat = [
            {
                "id": "one",
                "label": "One Model",
                "provider": "acme",
                "lanes": ["primary", "long"],
            }
        ]
        self.assertEqual(
            [row["label"] for row in models.lane_rows(flat, "primary")],
            ["One Model - Long (queued)", "One Model - Medium (concurrent)"],
        )

    def test_answering_a_twin_row_stores_a_real_model_id_and_the_window_beside_it(self):
        # Every other reader of this document looks a value up in the model table, so a row id
        # written there would fail the launch rather than change the window.
        code, _ = self.select(self.pick("roomy@long"), self.pick("roomy"))
        self.assertEqual(code, 0)
        self.assertEqual(
            self.selection(), {"primary": "roomy", "subagent": "roomy", "agent_lane": "long"}
        )

    def test_the_window_choice_survives_into_the_next_launch_as_the_remembered_row(self):
        (self.runtime / models.PREVIOUS_SELECTION).write_text(
            json.dumps({"primary": "roomy", "subagent": "roomy", "agent_lane": "long"})
        )
        code, script = self.select(self.pick("roomy@long"), self.pick("roomy"))
        self.assertEqual(code, 0)
        step = script.asks[0][0]
        self.assertEqual(step.initial().chosen, ("roomy@long",))
        self.assertIn(
            "Roomy Model - Long (queued, 1M window) [acme] (last used)", self.screen.getvalue()
        )

    def test_a_native_answer_is_not_reported_as_a_remembered_extended_one(self):
        # The suffix has to mean "this is what you picked last time", so the window the last launch
        # used is the only thing that can make the native row look like a repeat of it.
        (self.runtime / models.PREVIOUS_SELECTION).write_text(
            json.dumps({"primary": "roomy", "subagent": "roomy", "agent_lane": "long"})
        )
        self.select(self.pick("roomy"), self.pick("roomy"))
        primary = self.screen.getvalue().splitlines()[0]
        self.assertEqual(primary, "Main agent model: Roomy Model - Medium (concurrent) [acme]")

    def test_an_extended_answer_cannot_leak_onto_the_subagents_row(self):
        # One screen's window is not the other screen's row id: joining the two would invent a
        # "roomy@long" row for a lane that has never had twins and lose its (last used) note.
        (self.runtime / models.PREVIOUS_SELECTION).write_text(
            json.dumps({"primary": "roomy", "subagent": "roomy", "agent_lane": "long"})
        )
        self.select(self.pick("modest@long"), self.pick("roomy"))
        self.assertEqual(
            self.selection(), {"primary": "modest", "subagent": "roomy", "agent_lane": "long"}
        )
        self.assertIn("Subagent model: Roomy Model [acme] (last used)", self.screen.getvalue())

    def test_a_window_no_longer_offered_falls_back_instead_of_failing_the_launch(self):
        # The stale preference is the interesting part: a long lane withdrawn from a model between
        # launches must resolve to the native window, not to a row the screen cannot answer with.
        # One primary model left means the screen is not even asked, so this is also the case where
        # nothing on screen could have corrected the memory.
        self.models.pop(1)
        (self.runtime / models.PREVIOUS_SELECTION).write_text(
            json.dumps({"primary": "roomy", "subagent": "roomy", "agent_lane": "long"})
        )
        for model in self.models:
            model["lanes"].pop("long", None)
        code, script = self.select()
        self.assertEqual(code, 0)
        # Neither lane has a second row left to choose between, so nothing is asked at all and the
        # stale "long" has to correct itself without ever meeting the screen.
        self.assertEqual([step.title for step, _ in script.asks], [])
        self.assertEqual(
            self.selection(), {"primary": "roomy", "subagent": "roomy", "agent_lane": "primary"}
        )

    def test_a_stale_window_preference_does_not_steal_the_default_from_the_native_row(self):
        # The cursor opens on the row the last launch answered, and on a screen whose model lost
        # its long lane there is no such row: defaulting to the first one is the honest answer.
        (self.runtime / models.PREVIOUS_SELECTION).write_text(
            json.dumps({"primary": "modest", "subagent": "roomy", "agent_lane": "long"})
        )
        for model in self.models:
            if model["id"] == "modest":
                model["lanes"].pop("long")
        code, script = self.select(self.pick("modest"), self.pick("roomy"))
        self.assertEqual(code, 0)
        self.assertEqual(script.asks[0][0].initial().chosen, ("modest",))
        self.assertEqual(self.selection()["agent_lane"], "primary")

    def test_the_override_column_accepts_a_twin_row_as_well_as_a_plain_model(self):
        # HARNESS_PRIMARY_MODEL is documented as taking whatever the screen offers, so the window
        # has to be reachable from it for a scripted launch too.
        code, _ = self.select(self.pick("roomy"), HARNESS_PRIMARY_MODEL="roomy@long")
        self.assertEqual(code, 0)
        self.assertEqual(
            self.selection(), {"primary": "roomy", "subagent": "roomy", "agent_lane": "long"}
        )

    def test_a_window_that_is_no_one_round_number_is_written_out_in_full(self):
        # Rounding 12,345 up to "12k" would advertise a window the definition does not declare,
        # which is the one thing a row naming a context size must never do.
        self.assertEqual(models.token_span(12345), "12,345")
        self.assertEqual(models.token_span(262144), "256k")
        self.assertEqual(models.token_span(1000000), "1M")
        self.assertEqual(models.token_span(1048576), "1M")

    def test_a_single_model_with_a_long_lane_still_asks_because_the_window_is_a_question(self):
        # Without the twins this launch would answer itself and the extended lane would be
        # unreachable except by editing a file.
        self.models.pop(1)
        code, script = self.select(self.pick("roomy@long"))
        self.assertEqual(code, 0)
        self.assertEqual([step.title for step, _ in script.asks], [PRIMARY])
        self.assertEqual(self.selection()["agent_lane"], "long")


if __name__ == "__main__":
    unittest.main()
