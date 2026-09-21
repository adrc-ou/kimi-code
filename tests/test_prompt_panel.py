"""The startup panel: what it draws, what it accepts, and what it refuses to do.

The panel is the harness's only trust surface - the one place an operator can see the whole
resolved context before it is spent. These tests hold the properties that make it trustworthy
rather than pretty: the diagram sums, no figure is presented as measured when it is not, every
option is reachable, a half-selected pair warns exactly once, and nothing at all happens to the
operator's choices when they never had a terminal.

Two renderers share those properties, so the suite does too: the flat screen an unattended launch
prints, and the modal tree a person at a keyboard drives. What the old line-editor grammar used to
answer with - a typed digit, or ``a``/``n``/``r`` - is asserted here through the tree's own keys,
because a keypress has no aliases to forgive and no command to mis-type.
"""

from __future__ import annotations

import ast
import datetime as dt
import io
import json
import os
import re
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# The repository root as well as tools/, so that `tests.helpers` resolves under every way of
# running the suite: unittest only puts the start directory on the path, not its parent, so a
# plain `helpers` import would break `python -m unittest tests.test_x` and vice versa.
for _directory in (ROOT, ROOT / "tools"):
    if str(_directory) not in sys.path:
        sys.path.insert(0, str(_directory))

import policy  # noqa: E402
import prompt_context as pc  # noqa: E402
import prompt_measure as pm  # noqa: E402
import prompt_panel as pp  # noqa: E402

# Flat, like ``prompt_panel`` imports them: this suite loads the panel as a top-level module, and
# the same engine reached two ways would be two sets of classes, so an ``isinstance`` here would
# fail for the right reasons and a shared row type would not be shared at all.
from tui import flow  # noqa: E402
from tui.app import BACK_BINDING, View, navigation  # noqa: E402
from tui.caps import NONE, Caps  # noqa: E402

from tests.helpers import measured_record, run_in_pty, shipped_plan  # noqa: E402

PLAN = shipped_plan()

#: A painter that asks the terminal nothing, so a tree can be measured in a suite whose own
#: standard output is a pipe.
QUIET = Caps(color=NONE, columns=80, rows=24, probe=False)
#: The harness's own tier of the all-lane contract, as the tree spells it: derived, so that
#: renaming :data:`prompt_context.CONTRACT_SOURCE` moves every expectation with it.
CONTRACT = "/".join(pc.CONTRACT_SOURCE)


def tree(
    root: Path,
    *,
    enabled: dict[str, bool] | None = None,
    static: dict[str, str] | None = None,
    latest: dict[str, dict] | None = None,
    guidance: str = "module guidance text for the selected module",
) -> pp.ContextStep:
    """The modal tree over a workspace, wired the way :func:`prompt_panel.choose_context` wires it.

    The graph stays on the step as ``.graph``, which is where the launcher keeps it too: an answer
    map is only meaningful beside the object that composed the rows it came from.
    """
    graph = pp.ContextGraph(
        PLAN,
        root,
        guidance,
        latest or {},
        dict(pc.resolve_enabled(enabled)),
        dict(pc.resolve_static(static)),
    )
    step = pp.ContextStep(graph)
    step.caps = QUIET
    return step


def tree_rows(step: pp.ContextStep, state: object | None = None) -> list[str]:
    """Every row the tree paints, as plain text: the body without a terminal attached."""
    live = step.initial() if state is None else state
    return [row.line.text() for row in step.rows(live)]


def prose(step: pp.ContextStep, state: object | None = None) -> str:
    """The tree as one run of prose, for sentences that are wrapped over two rows.

    A note is laid out inside its own column, so the second line of it starts with that column's
    indentation. Row alignment is what :func:`tree_rows` is asserted against; a sentence's
    *wording* has to be read the way the operator reads it, spaces and all.
    """
    return re.sub(r"\s+", " ", " ".join(tree_rows(step, state)))


def speaking(step: pp.ContextStep, state: object | None = None) -> str:
    """Everything the screen is able to say: the map, every row's pane text, and the rules.

    The map holds no sentences and the pane holds only the row under the cursor at a time, so a
    fact may live in either and still be on the screen for anyone who goes looking for it. That is
    the whole argument for moving the old inline notes off the rows, and it is what a test about a
    fact not being *lost* has to measure — not the body alone.
    """
    parts = tree_rows(step, state)
    parts += [node.detail for node, _, _, _ in step.rows_in_order() if node.detail]
    parts += list(step.rules)
    return re.sub(r"\s+", " ", " ".join(parts))


def stage_files(root: Path, files: dict[str, str]) -> Path:
    """Write exactly these files under ``root``, making whatever directories they name."""
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


class PanelDrawTests(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.root = Path(self._dir.name) / "ws"
        self.root.mkdir()
        (self.root / "AGENTS.md").write_text("# project rules\n" + "x" * 400 + "\n")
        self.all_on = dict(pc.DEFAULT_ENABLED)

    def screen(self, latest: dict | None = None, enabled: dict | None = None) -> str:
        """Render the panel with colour off, which is what a log or a CI run would show."""
        return pp.draw(
            PLAN,
            self.root,
            "module guidance text for the selected module",
            enabled or self.all_on,
            latest or {},
            colour_on=False,
            remembered=False,
        )

    def test_a_first_launch_prices_what_it_wrote_and_admits_what_it_did_not(self) -> None:
        """Everything the harness generated itself gets a ~ figure; Kimi's framing gets none.

        Scoped to the diagram, above the lane legend: the legend quotes real caps and windows,
        which are exact facts about the model and must not be marked as guesses.
        """
        diagram = self.screen().split("Lane caps,")[0]
        self.assertIn("cannot be totalled", diagram)
        self.assertIn("?", diagram)
        for row in diagram.splitlines():
            value = row.rsplit("  ", 1)[-1].strip()
            if not value or value[-1] not in "09?":
                continue
            self.assertTrue(
                value.startswith("~") or value.endswith("?"),
                f"an unmeasured diagram drew a bare figure: {row!r}",
            )

    def test_a_measured_row_is_exact_and_closes(self) -> None:
        latest = {
            pm.AUDIENCE_MAIN: measured_record(PLAN, pm.AUDIENCE_MAIN, 7601, 2369, 2379, 2853),
            pm.AUDIENCE_SUBAGENT: measured_record(
                PLAN, pm.AUDIENCE_SUBAGENT, 7099, 2877, 2379, 1843
            ),
        }
        screen = self.screen(latest)
        self.assertIn("  7,601", screen)
        self.assertIn("  7,099", screen)
        main = pp.pieces(PLAN, self.root, "m", self.all_on, latest[pm.AUDIENCE_MAIN], "primary")
        self.assertEqual(
            main.total.tokens,
            main.system_cost.tokens + main.agents_cost.tokens + main.project_cost.tokens,
            "the measured boxes must add up to the measured whole with no residual line",
        )

    def test_the_system_prompt_is_never_counted_twice(self) -> None:
        # Kimi renders the staged prompt ahead of the AGENTS.md documents, so the framing region
        # already contains it. A diagram that drew both would overstate the row it is measuring.
        latest = {
            pm.AUDIENCE_MAIN: measured_record(PLAN, pm.AUDIENCE_MAIN, 7601, 2369, 2379, 2853)
        }
        main = pp.pieces(PLAN, self.root, "m", self.all_on, latest[pm.AUDIENCE_MAIN], "primary")
        self.assertEqual(main.system_cost.tokens, 2369)
        self.assertLess(main.system_cost.tokens, main.total.tokens)

    def test_every_option_appears_with_its_own_number_and_its_own_price(self) -> None:
        """A long label wraps across lines, so the check rejoins the screen before matching.

        The gutter mark, the digit, and the price all have to be on the same physical line: that
        adjacency is what lets an operator see which number belongs to which option.
        """
        screen = self.screen()
        joined = " ".join(" ".join(screen.splitlines()).split())
        for index, option in enumerate(pc.OPTIONS, start=1):
            self.assertIn(f"{index}. {option.label}", joined)
            # The number rides on whichever physical line the label ended on, which wrapping can
            # move; the pairing that matters is that the gutter mark and its price share a line.
            marked = [r for r in screen.splitlines() if f"[x] {index}. " in r]
            holder = marked or [
                r for r in screen.splitlines() if re.search(rf"\s{index}\. ", r)
            ]
            self.assertTrue(holder, f"option {index} has no line")
            self.assertTrue(
                any(re.search(r"(~?[\d,]+|no prompt cost)$", r.rstrip()) for r in holder),
                f"option {index} has no price on its line: {holder}",
            )
        self.assertIn("DYNAMIC CONTEXT THIS HARNESS ADDS", screen)

    def test_figures_on_one_screen_are_distinct_numbers(self) -> None:
        """Two identical figures on one screen are indistinguishable, which defeats the point.

        The percentages legitimately repeat a label but never a bare count, so the check is on the
        leading number of each right-aligned value.
        """
        latest = {
            pm.AUDIENCE_MAIN: measured_record(PLAN, pm.AUDIENCE_MAIN, 7601, 2369, 2379, 2853),
        }
        figures = []
        for row in self.screen(latest).splitlines():
            match = re.search(r"~?[\d,]+(?=\s{2,}\d\.\d%$)", row)
            if match:
                figures.append(match.group().strip("~,"))
        self.assertGreater(len(figures), 4, figures)
        self.assertEqual(len(figures), len(set(figures)), f"duplicate figures: {figures}")

    def test_the_denominator_is_the_input_cap_not_the_window(self) -> None:
        latest = {
            pm.AUDIENCE_MAIN: measured_record(PLAN, pm.AUDIENCE_MAIN, 7601, 2369, 2379, 2853)
        }
        screen = self.screen(latest)
        pct = 7601 / pm.input_cap(PLAN, "primary") * 100
        row = next(r for r in screen.splitlines() if "  7,601" in r)
        self.assertIn(f"{pct:.1f}%", row, "the percentage shares its line with the number")
        window_pct = 7601 / PLAN["lanes"]["primary"]["context_tokens"] * 100
        self.assertNotIn(f"{window_pct:.1f}%", row, "that denominator was the window, not the cap")
        window = PLAN["lanes"]["primary"]["context_tokens"]
        self.assertIn(
            f"window {window:,}", screen, "the window is named, so the ratio stays checkable"
        )

    def test_a_measured_row_uses_the_cap_of_the_lane_that_produced_it(self) -> None:
        """The long lane is the main audience on another denominator, so no third row is needed."""
        if "long" not in PLAN["lanes"]:
            self.skipTest("this selection has no long lane")
        record = measured_record(PLAN, pm.AUDIENCE_MAIN, 7601, 2369, 2379, 2853, lane="long")
        main = pp.pieces(PLAN, self.root, "m", self.all_on, record, "primary")
        self.assertEqual(main.cap, pm.input_cap(PLAN, "long"))
        self.assertNotEqual(main.cap, pm.input_cap(PLAN, "primary"))

    def test_no_file_means_no_box(self) -> None:
        screen = self.screen()
        self.assertNotIn("SYSTEM.md.example", screen)
        self.assertNotIn("CONTEXT.md.example", screen)

    def test_an_absent_operator_file_says_the_default_is_in_use(self) -> None:
        screen = self.screen()
        self.assertIn("absent", screen)

    def test_a_present_operator_file_is_named_by_path(self) -> None:
        (self.root / "SYSTEM.md").write_text("be terse\n")
        self.assertIn("SYSTEM.md", self.screen())

    def test_the_generated_wrapper_gets_no_box_of_its_own(self) -> None:
        # The wrapper is a mechanism; its rendered length is Kimi's built-in prompt exactly, so a
        # row for it would show a size that no model ever pays.
        screen = self.screen()
        self.assertNotIn("${base_prompt}", screen)

    def test_switches_that_inject_no_text_are_not_priced_as_zero_tokens(self) -> None:
        screen = self.screen()
        self.assertIn("no prompt cost", screen)

    def test_module_guidance_is_priced_from_its_real_bytes(self) -> None:
        guidance = "use the module's tool\n" * 40
        option = pc.OPTION_BY_ID[pc.OPTION_MODULE_GUIDANCE]
        self.assertEqual(
            pp.option_cost(option, PLAN, guidance).tokens, pm.estimate_tokens(guidance.strip("\n"))
        )

    def staged_price(self, screen: str) -> int:
        row = next(line for line in screen.splitlines() if "the staged system prompt" in line)
        match = re.search(r"~([\d,]+)", row)
        self.assertIsNotNone(match, f"the system prompt row carries no price: {row!r}")
        return int(match.group(1).replace(",", ""))

    def test_turning_the_envelope_off_moves_the_number_it_prices(self) -> None:
        """The figure has to follow the choice, not merely the screen.

        Comparing whole screens passes on the ``[x]``/``[ ]`` gutter mark alone, which says nothing
        about the diagram, so this reads the one number the option is supposed to move.
        """
        on = self.staged_price(self.screen())
        off = dict(self.all_on)
        off[policy.OPTION_PARALLELISM] = False
        self.assertGreater(
            on,
            self.staged_price(self.screen(None, off)),
            "the parallelism block ships in the system prompt, so switching it off must shrink it",
        )

    def test_help_text_is_priced_as_the_model_pays_it_not_as_the_editor_shows_it(self) -> None:
        """Comments are stripped before staging, so the panel must not charge for them.

        This is the deliberate cost of that rule: the file an operator edits is longer than the
        document that ships, and a panel that priced the file would overstate every prompt that
        explains itself. The help here is worth hundreds of tokens; the body is worth a few.
        """
        bare = pp.pieces(PLAN, self.root, "m", self.all_on, None, "primary")
        help_text = "<!-- " + "options you could pick here " * 120 + "-->\n"
        (self.root / "SYSTEM.md").write_text(help_text + "be terse\n")
        (self.root / "CONTEXT.md").write_text(help_text + "test before shipping\n")
        commented = pp.pieces(PLAN, self.root, "m", self.all_on, None, "primary")
        self.assertGreater(len(help_text), 3_000)
        for note, body in (("SYSTEM.md", "be terse"), ("CONTEXT.md", "test before shipping")):
            with self.subTest(note=note):
                self.assertNotIn("options you could pick here", commented.system + commented.agents)
                self.assertIn(body, commented.system + commented.agents)
        # A few hundred tokens of help moves each figure by the body it left behind, not by itself.
        self.assertLess(commented.system_cost.tokens - bare.system_cost.tokens, 20)
        self.assertLess(commented.agents_cost.tokens - bare.agents_cost.tokens, 20)

    def test_the_panel_shows_whether_it_is_remembering_or_defaulting(self) -> None:
        self.assertIn("this build's defaults", self.screen())
        remembered = pp.draw(
            PLAN, self.root, "m", self.all_on, {}, colour_on=False, remembered=True
        )
        self.assertIn("remembered from your last launch", remembered)

    def test_plain_output_contains_no_escape_sequences(self) -> None:
        self.assertNotIn("\033", self.screen())

    def test_colour_output_uses_escapes(self) -> None:
        screen = pp.draw(PLAN, self.root, "m", self.all_on, {}, colour_on=True, remembered=False)
        self.assertIn("\033[", screen)

    def test_a_styled_paragraph_resets_on_every_physical_line(self) -> None:
        """A dim block note spans lines; the escape must close on each or it leaks downstream."""
        screen = pp.draw(PLAN, self.root, "m", self.all_on, {}, colour_on=True, remembered=False)
        for line in screen.splitlines():
            if "\033[" in line:
                self.assertTrue(line.endswith(pp.RESET), line)

    def test_no_line_is_longer_than_the_panel(self) -> None:
        measured = {pm.AUDIENCE_MAIN: measured_record(PLAN, pm.AUDIENCE_MAIN,
                                                     7601, 2369, 2379, 2853)}
        for latest in ({}, measured):
            for line in self.screen(latest).splitlines():
                self.assertLessEqual(len(line), pp.WIDTH, f"overrun: {line!r}")

    def test_no_number_is_jammed_against_its_label(self) -> None:
        """The alignment guarantee, asserted where it is made: pad() never compresses the gap."""
        latest = {pm.AUDIENCE_MAIN: measured_record(PLAN, pm.AUDIENCE_MAIN,
                                                   7601, 2369, 2379, 2853)}
        rows = pp.static_rows(
            pp.pieces(PLAN, self.root, "m", self.all_on, latest[pm.AUDIENCE_MAIN], "primary"),
            pp.pieces(PLAN, self.root, "m", self.all_on, None, "subagent"), PLAN, self.root)
        rows += pp.checklist(self.all_on, PLAN, "m")
        for label, value, _ in rows:
            if not value:
                continue
            line = pp.pad(label, value, pp.WIDTH - pp.NUMBER_FIELD)
            self.assertTrue(line.endswith(value), line)
            gap = len(line) - len(value) - len(label)
            self.assertGreaterEqual(gap, pp.MIN_GAP, f"{label!r} / {value!r}")

    def test_a_cost_in_the_diagram_and_a_cost_in_the_checklist_share_one_axis(self) -> None:
        """Rendering the two halves in one pass is what makes this true, so it is asserted here."""
        screen = self.screen({
            pm.AUDIENCE_MAIN: measured_record(PLAN, pm.AUDIENCE_MAIN, 7601, 2369, 2379, 2853)
        })
        numbers = [
            line.rfind(match.group()) + len(match.group())
            for line in screen.splitlines()
            for match in [re.search(r"~?[\d,]+\s+\d\.\d%$", line)]
            if match
        ]
        self.assertGreater(len(numbers), 4, "too few aligned figures to judge the axis")
        self.assertEqual(len(set(numbers)), 1, f"the number column sat at {sorted(set(numbers))}")
        percents = [
            len(line.rstrip())
            for line in screen.splitlines()
            if re.search(r"\d\.\d%$", line)
        ]
        self.assertEqual(len(set(percents)), 1, "the percentage column moved")
        # The diagram is the only half with a cap to divide by; a checklist price is a block's own
        # size, and inventing a percentage for it would imply a total that row does not know.
        self.assertGreaterEqual(len(percents), len(numbers))
        self.assertTrue(all("%" in line for line in screen.splitlines()
                            if re.search(r"~?[\d,]+\s+\d\.\d%$", line)))


class WarningTests(unittest.TestCase):
    def test_some_options_declare_a_companion_at_all(self) -> None:
        """Without a pair the gutter, the warning, and the adjacency rule are all untestable."""
        self.assertTrue([option for option in pc.OPTIONS if option.companions])

    def test_the_default_selection_produces_no_warning(self) -> None:
        self.assertEqual(pp.warning(dict(pc.DEFAULT_ENABLED)), "")

    def test_everything_off_produces_no_warning(self) -> None:
        self.assertEqual(pp.warning(dict.fromkeys(pc.OPTION_IDS, False)), "")

    def test_an_orphaned_sibling_is_named_once(self) -> None:
        enabled = dict(pc.DEFAULT_ENABLED)
        parent = next(o for o in pc.OPTIONS if o.companions)
        enabled[parent.id] = True
        for other in parent.companions:
            enabled[other] = False
        note = pp.warning(enabled)
        self.assertTrue(note)
        self.assertIn("may not work as expected", note)
        self.assertEqual(note.count("."), 0, f"the gentle note must be one sentence: {note}")
        self.assertEqual(note.count("!"), 0)
        self.assertNotIn("Error", note)

    def test_the_warning_does_not_refuse_the_choice(self) -> None:
        """A warning that also disabled the option would be a constraint wearing other clothes."""
        enabled = dict(pc.DEFAULT_ENABLED)
        parent = next(o for o in pc.OPTIONS if o.companions)
        for other in parent.companions:
            enabled[other] = False
        self.assertTrue(pp.warning(enabled), "the pair has to be half-selected for this to mean")
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        step = tree(Path(holder.name), enabled=enabled)
        drawn = {node.id: node for node, _, _, _ in step.rows_in_order()}
        for name in (parent.id, *parent.companions):
            self.assertTrue(drawn[name].editable, f"{name} is warned about, not refused")
        # The all-off answer the old ``n`` produced is still reachable, one row at a time, and the
        # sentence goes away with the half-selected pair rather than outliving it.
        values = dict(step.graph.opening())
        for option in pc.OPTIONS:
            values[option.id] = pp.ON_OFF[1]
        after, _ = step.graph.answer(values)
        self.assertEqual(after[parent.id], False, "all-off must not silently re-enable a pair")
        self.assertFalse(any(after.values()))
        self.assertEqual(pp.warning(after), "", "nothing on can hardly be a half-selected pair")


class AnswerTests(unittest.TestCase):
    """What the tree answers: what the cursor can reach, what one key moves, and what undoes it.

    These are the properties the old line-editor grammar used to be tested through. The grammar is
    gone - a fullscreen tree answers a keypress, not a typed command - so each one is now checked
    against the tree's own answers, which is where a regression would actually show.
    """

    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.root = Path(self._dir.name)
        # Three files with three different fates: the operator's two, each superseding nothing, and
        # the harness contract below ``CONTEXT.md``, which the operator's file does supersede. One
        # workspace, and every shape of row the tree can draw is in it.
        stage_files(self.root, {
            "SYSTEM.md": "# the operator's own voice\n",
            "CONTEXT.md": "# the operator's own contract\n",
            "runtime/AGENTS.md": "# this harness's contract\n",
        })
        self.enabled = dict(pc.DEFAULT_ENABLED)
        self.step = tree(self.root, enabled=self.enabled)
        self.state = self.step.initial()

    def test_every_answer_is_a_row_the_cursor_can_reach(self) -> None:
        """There are no numbers to type, so a row nobody can stop on is a row nobody can answer.

        This replaced a ceiling of nine options, which was only ever an artefact of the single
        digit that used to select one.
        """
        reachable = {node.id for node in self.step.options()}
        self.assertTrue(set(pc.OPTION_IDS) <= reachable, sorted(set(pc.OPTION_IDS) - reachable))
        self.assertTrue(set(pc.STATIC_IDS) <= reachable)
        by_id = {node.id: node for node in self.step.options()}
        for option in pc.OPTIONS:
            self.assertEqual(by_id[option.id].states, pp.ON_OFF, option.id)

    def test_space_moves_one_row_and_only_that_row(self) -> None:
        """The whole contract of the key, over every row that answers it."""
        for node in self.step.options():
            moved = self.step.toggled(self.state, node.id)
            changed = {
                key for key in self.state.values if self.state.values[key] != moved.values[key]
            }
            self.assertEqual(changed, {node.id}, node.id)
            self.assertIn(moved.values[node.id], node.states, node.id)
            self.assertNotEqual(moved.values[node.id], self.state.values[node.id], node.id)
            self.assertEqual(moved.focus, self.state.focus, "answering must not move the cursor")

    def test_the_space_key_visits_every_state_a_row_offers(self) -> None:
        """A tri-state switch that skipped ``on`` would be a two-state switch with a lie on it."""
        for node in self.step.options():
            seen: list[str] = []
            state = self.state
            for _ in range(len(node.states)):
                state = self.step.toggled(state, node.id)
                seen.append(state.values[node.id])
            self.assertEqual(seen, list(node.states[1:]) + [node.states[0]], node.id)

    def test_a_row_with_no_switch_cannot_be_moved_by_any_key(self) -> None:
        """Headings and superseded sources hold the picture and refuse the keystroke.

        The old test fed the grammar a number past the end of the list; the tree's equivalent is a
        row the cursor was never meant to rest on, and the answer must be exactly "nothing".
        """
        still = [node for node, _, _, _ in self.step.rows_in_order() if not node.editable]
        self.assertTrue(still, "a tree with nothing fixed in it is a form, not a diagram")
        for node in still:
            self.assertIs(self.step.toggled(self.state, node.id), self.state, node.id)
        self.assertIs(self.step.toggled(self.state, "no-such-row"), self.state)

    def test_accept_hands_over_the_whole_answer_and_nothing_else(self) -> None:
        """Enter commits the map on screen, as a copy, in the halves the launcher composes from."""
        result = self.step.commit(self.state)
        self.assertEqual(result.value, self.state.values)
        self.assertIsNot(result.value, self.state.values)
        enabled, static = self.step.graph.answer(result.value)
        self.assertEqual(set(enabled), set(pc.OPTION_IDS))
        self.assertEqual(set(static), set(pc.STATIC_IDS))

    def test_all_off_reaches_the_tabula_rasa_selection(self) -> None:
        """The blank prompt has to be reachable by answers this screen actually offers.

        Both halves at once, which is the part the flat list could not say: the add-ons switch off
        one by one, and the two documents switch off beside the files they are read from.
        """
        values = dict(self.step.graph.opening())
        for option in pc.OPTIONS:
            values[option.id] = pp.ON_OFF[1]
        for block in pc.STATIC_IDS:
            values[block] = pc.OFF
        enabled, static = self.step.graph.answer(values)
        self.assertFalse(any(enabled.values()))
        self.assertEqual(set(enabled), set(pc.OPTION_IDS))
        self.assertEqual(static, dict.fromkeys(pc.STATIC_IDS, pc.OFF))
        self.assertEqual(
            pc.compose_agents_document(self.root, PLAN, "m", enabled, static=static), ""
        )
        self.assertEqual(
            pc.compose_system_document(self.root, PLAN, enabled, static=static),
            pc.EMPTY_PROMPT_SENTINEL + "\n",
        )

    def test_reset_restores_this_builds_defaults(self) -> None:
        """``Ctrl-R`` is an undo of the whole answer, cursor included, back to shipped defaults."""
        moved = self.state
        for node in self.step.options():
            moved = self.step.toggled(moved, node.id)
        moved = self.step.with_focus(moved, 4)
        tree_rows(self.step, moved)
        self.assertNotEqual(moved.values, self.state.values)
        self.assertNotEqual(moved.focus, self.state.focus)
        back = self.step.reset(moved)
        self.assertEqual(back, self.step.initial())
        enabled, static = self.step.graph.answer(back.values)
        self.assertEqual(enabled, dict(pc.DEFAULT_ENABLED))
        self.assertEqual(static, dict(pc.STATIC_DEFAULT))


class StaticSwitchTests(unittest.TestCase):
    """The tri-state switch: what each shape of disk deserves, and what an untouched row stores.

    ``auto``/``on``/``off`` are three words, but a workspace does not always hold three different
    answers, and a third position that composes the bytes the first one already did is a keypress
    that changes nothing. These tests pin the collapsing rule rather than the wording of a row.
    """

    def root(self, files: dict[str, str]) -> Path:
        """A throwaway workspace holding exactly these files, reaped with the test that made it."""
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        return stage_files(Path(holder.name), files)

    def document(self, root: Path, block: str, mode: str) -> str:
        """What this block's state alone puts in front of the model, the other left at ``auto``."""
        enabled = dict(pc.DEFAULT_ENABLED)
        static = dict.fromkeys(pc.STATIC_IDS, pc.AUTO)
        static[block] = mode
        if block == pc.STATIC_SYSTEM:
            return pc.compose_system_document(root, PLAN, enabled, static=static)
        return pc.compose_agents_document(root, PLAN, "m", enabled, static=static)

    def test_the_switch_offers_only_states_that_compose_differently(self) -> None:
        cases: dict[str, tuple[dict[str, str], dict[str, tuple[str, ...]]]] = {
            "nothing on disk": ({}, {"system": (pc.AUTO, pc.OFF), "context": (pc.AUTO,)}),
            "both files empty": (
                {pc.SYSTEM_FILE: "", pc.CONTEXT_FILE: ""},
                {"system": (pc.ON, pc.OFF), "context": (pc.OFF,)},
            ),
            "both empty over the contract": (
                {pc.SYSTEM_FILE: "", pc.CONTEXT_FILE: "", CONTRACT: "# c\n"},
                {"system": (pc.ON, pc.OFF), "context": (pc.ON, pc.OFF)},
            ),
            "both files filled": (
                {pc.SYSTEM_FILE: "# v\n", pc.CONTEXT_FILE: "# k\n"},
                {"system": (pc.AUTO, pc.OFF), "context": (pc.AUTO, pc.OFF)},
            ),
            "an empty voice over a real contract": (
                {pc.SYSTEM_FILE: "", CONTRACT: "# c\n"},
                {"system": (pc.ON, pc.OFF), "context": (pc.AUTO, pc.OFF)},
            ),
        }
        for name, (files, expected) in cases.items():
            root = self.root(files)
            for block, states in expected.items():
                switch = pp._block_switch(root, block, pc.AUTO)
                with self.subTest(case=name, block=block):
                    self.assertEqual(switch.states, states)
                    self.assertIn(switch.opening, switch.states)
                    # Every state the row does *not* offer composes what one it does composes, and
                    # the ones it offers compose different bytes from each other. Those two
                    # sentences are the whole difference between a collapsed switch and a broken
                    # one, and the table above only says which side of it each case is on.
                    for spelling, members in switch.members.items():
                        texts = {self.document(root, block, mode) for mode in members}
                        self.assertEqual(len(texts), 1, f"{block} {spelling} groups states apart")
                    apart = {
                        self.document(root, block, members[0])
                        for members in switch.members.values()
                    }
                    self.assertEqual(len(apart), len(switch.members), f"{block} offers a dead key")

    def test_an_empty_file_opens_the_block_switched_off(self) -> None:
        """The brief's own rule: an empty file on disk *is* the block being off."""
        for block, own in (
            (pc.STATIC_SYSTEM, pc.SYSTEM_FILE),
            (pc.STATIC_CONTEXT, pc.CONTEXT_FILE),
        ):
            root = self.root({own: "", CONTRACT: "# c\n"})
            switch = pp._block_switch(root, block, pc.AUTO)
            with self.subTest(block=block):
                self.assertEqual(switch.opening, pc.OFF)
                self.assertEqual(pc.static_state(root, block), pc.OFF)
                self.assertIn(pc.ON, switch.states, "the state that ignores the file must exist")

    def test_a_switch_with_nothing_to_switch_says_so(self) -> None:
        """One state left standing is not a switch, and the row has to admit it rather than stall.

        An empty ``CONTEXT.md`` over no harness contract composes blank whichever way it is pushed,
        so the honest screen shows the answer and refuses the key.
        """
        root = self.root({pc.CONTEXT_FILE: ""})
        step = tree(root)
        self.assertEqual(pp._block_switch(root, pc.STATIC_CONTEXT, pc.AUTO).states, (pc.OFF,))
        rows = {node.id: node for node, _, _, _ in step.rows_in_order()}
        self.assertFalse(rows[pc.STATIC_CONTEXT].editable)
        self.assertIn("no state of it composes a different document", prose(step))

    def test_an_untouched_row_stores_the_word_that_was_saved(self) -> None:
        """The screen may call an empty file ``off``; the file still remembers ``auto``.

        Both spellings compose the same document, so the stored word is the one that says what the
        operator chose, and only the diagram has to speak in effects.
        """
        root = self.root({pc.SYSTEM_FILE: "", CONTRACT: "# c\n"})
        graph = tree(root).graph
        opening = graph.opening()
        self.assertEqual(opening[pc.STATIC_SYSTEM], pc.OFF)
        _, stored = graph.answer(opening)
        self.assertEqual(stored[pc.STATIC_SYSTEM], pc.AUTO)
        self.assertEqual(graph.modes(opening)[pc.STATIC_SYSTEM], pc.OFF)
        moved = dict(opening, **{pc.STATIC_SYSTEM: pc.ON})
        self.assertEqual(graph.answer(moved)[1][pc.STATIC_SYSTEM], pc.ON)

    def test_the_drawn_word_and_the_stored_word_compose_the_same_prompt(self) -> None:
        """Collapsing is only honest while both halves agree about the bytes.

        :meth:`ContextGraph.answer` and :meth:`ContextGraph.modes` differ by design - one names
        provenance for the file, one for the picture - and the one thing they may never differ
        about is the document they describe.
        """
        shapes = (
            {},
            {pc.SYSTEM_FILE: ""},
            {pc.SYSTEM_FILE: "", pc.CONTEXT_FILE: ""},
            {pc.SYSTEM_FILE: "# v\n", pc.CONTEXT_FILE: ""},
        )
        for files in shapes:
            root = self.root({**files, CONTRACT: "# c\n"})
            graph = tree(root).graph
            opening = graph.opening()
            _, stored = graph.answer(opening)
            drawn = graph.modes(opening)
            for block in pc.STATIC_IDS:
                with self.subTest(files=tuple(sorted(files)), block=block):
                    self.assertEqual(
                        self.document(root, block, stored[block]),
                        self.document(root, block, drawn[block]),
                    )

    def test_a_context_file_of_only_whitespace_is_empty_where_that_is_decided(self) -> None:
        """The one shape the collapse bends on, named rather than smoothed over.

        :func:`prompt_context.static_state` calls a blank-on-trim file empty - which is what the
        sentence on the tree promises - so the row opens ``off`` while the file still remembers
        ``auto``. Composing under the latter keeps that file's whitespace: the agents document has
        always appended its tier's text as it stands, where the system document asks for
        :meth:`str.strip` first. So the two spellings differ by two spaces the model would trim
        before it decided anything, which is the seam this test exists to keep visible.
        """
        root = self.root({pc.CONTEXT_FILE: "  \n", CONTRACT: "# c\n"})
        graph = tree(root).graph
        opening = graph.opening()
        _, stored = graph.answer(opening)
        drawn = graph.modes(opening)
        self.assertEqual(opening[pc.STATIC_CONTEXT], pc.OFF)
        self.assertEqual(stored[pc.STATIC_CONTEXT], pc.AUTO)
        self.assertEqual(drawn[pc.STATIC_CONTEXT], pc.OFF)
        self.assertEqual(
            self.document(root, pc.STATIC_CONTEXT, stored[pc.STATIC_CONTEXT]).strip(),
            self.document(root, pc.STATIC_CONTEXT, drawn[pc.STATIC_CONTEXT]).strip(),
        )
        self.assertIn("counts as empty", pp.STATIC_NOTE)


class PrefsRoundTripTests(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.runtime = Path(self._dir.name)
        (self.runtime / "model-policy.json").write_text(json.dumps(PLAN))

    def capture(self, argv: list[str]) -> tuple[int, str]:
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = pp.main(["--runtime-dir", str(self.runtime), *argv])
        return code, buffer.getvalue()

    def test_a_choice_survives_the_next_launch(self) -> None:
        off = dict(pc.DEFAULT_ENABLED)
        off[policy.OPTION_PARALLELISM] = False
        pc.save_prefs(self.runtime / pc.PREFS_FILE, off)
        code, screen = self.capture(["--plain"])
        self.assertEqual(code, 0)
        self.assertIn("remembered from your last launch", screen)
        context = pp.load_context(self.runtime)
        self.assertFalse(context.enabled[policy.OPTION_PARALLELISM])
        self.assertTrue(context.remembered)
        self.assertIn("lanes", context.plan)

    def test_an_unattended_launch_applies_remembered_choices_without_prompting(self) -> None:
        self.capture([])  # no tty under the test runner: must draw, not hang, and not overwrite
        code, screen = self.capture(["--plain"])
        self.assertEqual(code, 0)
        self.assertIn("STATIC CONTEXT", screen)
        self.assertNotIn("Enter accepts, 1-9 toggles", screen)

    def test_configure_turns_one_option_off_without_a_screen(self) -> None:
        code, out = self.capture(
            ["--configure", "--disable", policy.OPTION_LANE_TABLE]
        )
        self.assertEqual(code, 0)
        self.assertIn("wrote", out)
        loaded = pc.load_prefs(self.runtime / pc.PREFS_FILE)
        self.assertFalse(loaded[policy.OPTION_LANE_TABLE])
        self.assertTrue(loaded[policy.OPTION_LANE_LIMITS])

    def test_configure_rejects_an_unknown_id_and_writes_nothing(self) -> None:
        code, _ = self.capture(["--configure", "--enable", "make_it_faster"])
        self.assertEqual(code, 2)
        self.assertFalse((self.runtime / pc.PREFS_FILE).exists())

    def test_configure_lists_every_option(self) -> None:
        code, out = self.capture(["--configure", "--show"])
        self.assertEqual(code, 0)
        for option in pc.OPTIONS:
            self.assertIn(option.id, out)

    def test_configure_all_off_is_the_scripted_tabula_rasa(self) -> None:
        self.capture(["--configure", "--all-off"])
        self.assertFalse(any(pc.load_prefs(self.runtime / pc.PREFS_FILE).values()))

    def test_forcing_a_document_lands_in_the_same_file_as_a_box(self) -> None:
        """The scripted half of the tri-state, which is what a CI job has instead of arrow keys."""
        code, out = self.capture(["--configure", "--static", f"{pc.STATIC_CONTEXT}=off"])
        self.assertEqual(code, 0)
        self.assertEqual(
            pc.load_static(self.runtime / pc.PREFS_FILE),
            dict(pc.STATIC_DEFAULT) | {pc.STATIC_CONTEXT: pc.OFF},
        )
        self.assertIn(pc.STATIC_CONTEXT, out)

    def test_resetting_the_boxes_leaves_the_documents_the_operator_set_alone(self) -> None:
        pc.save_prefs(self.runtime / pc.PREFS_FILE, dict(pc.DEFAULT_ENABLED),
                      {pc.STATIC_SYSTEM: pc.OFF})
        self.capture(["--configure", "--all-off"])
        self.assertFalse(any(pc.load_prefs(self.runtime / pc.PREFS_FILE).values()))
        self.assertEqual(
            pc.load_static(self.runtime / pc.PREFS_FILE)[pc.STATIC_SYSTEM], pc.OFF
        )

    def test_an_unknown_document_or_state_is_refused_before_anything_is_written(self) -> None:
        for argument in ("system=sometimes", "kimi=on", "system"):
            with self.subTest(argument=argument):
                code, _ = self.capture(["--configure", "--static", argument])
                self.assertEqual(code, 2)
                self.assertFalse((self.runtime / pc.PREFS_FILE).exists())

    def test_the_scripted_report_prints_both_halves_of_the_selection(self) -> None:
        code, out = self.capture(["--configure", "--show"])
        self.assertEqual(code, 0)
        for option in pc.OPTIONS:
            self.assertIn(option.id, out)
        for block in pc.STATIC_IDS:
            self.assertIn(block, out)

    def test_the_prefs_file_never_leaves_the_options_it_knows(self) -> None:
        pc.save_prefs(self.runtime / pc.PREFS_FILE, dict(pc.DEFAULT_ENABLED))
        mode = self.runtime / pc.PREFS_FILE
        self.assertEqual(mode.stat().st_mode & 0o777, 0o600)
        stored = json.loads(mode.read_text())
        # Two halves now: the flat option ids, and one nested map of the static block ids. Anything
        # else in the file would be a key no reader owns.
        self.assertEqual(set(stored) - {"static"}, set(pc.OPTION_IDS))
        self.assertEqual(set(stored["static"]), set(pc.STATIC_IDS))

    def test_a_pref_file_with_stray_keys_does_not_become_stray_text(self) -> None:
        (self.runtime / pc.PREFS_FILE).write_text('{"parallelism": true, "grape": true}')
        loaded = pc.load_prefs(self.runtime / pc.PREFS_FILE)
        self.assertNotIn("grape", loaded)

    def test_a_plan_without_lanes_is_refused_before_anything_is_priced(self) -> None:
        # A truncated or foreign plan file is readable JSON, so it used to reach the drawing code
        # and die in a lane lookup. Every figure on this screen is a lane's input cap, which is
        # what the panel is for, so it says so instead of raising.
        (self.runtime / "model-policy.json").write_text("{}")
        with self.assertRaises(SystemExit) as caught:
            pp.load_context(self.runtime)
        self.assertIn("names no lanes", str(caught.exception))


class HistoryReadingTests(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.runtime = Path(self._dir.name)

    def test_no_history_at_all_is_the_normal_first_case(self) -> None:
        self.assertEqual(pp.read_latest(self.runtime), {})

    def test_history_is_read_from_the_file_the_measurement_job_appends(self) -> None:
        rows = [
            measured_record(PLAN, pm.AUDIENCE_MAIN, 7601, 2369, 2379, 2853),
            measured_record(PLAN, pm.AUDIENCE_SUBAGENT, 7099, 2877, 2379, 1843),
        ]
        with (self.runtime / pc.MEASUREMENTS_FILE).open("w") as sink:
            for row in rows:
                sink.write(json.dumps(row) + "\n")
        latest = pp.read_latest(self.runtime)
        self.assertEqual(set(latest), {pm.AUDIENCE_MAIN, pm.AUDIENCE_SUBAGENT})
        screen = pp.draw(PLAN, self.runtime, "m", dict(pc.DEFAULT_ENABLED), latest,
                         colour_on=False, remembered=False)
        self.assertIn("7,601", screen)
        self.assertIn("7,099", screen)

    def test_the_newest_record_wins(self) -> None:
        old = measured_record(PLAN, pm.AUDIENCE_MAIN, 5000, 2000, 1500, 1500)
        new = measured_record(PLAN, pm.AUDIENCE_MAIN, 9000, 4000, 2500, 2500)
        new["time"] = old["time"] + 60_000
        with (self.runtime / pc.MEASUREMENTS_FILE).open("w") as sink:
            for row in (old, new):
                sink.write(json.dumps(row) + "\n")
        self.assertEqual(pp.read_latest(self.runtime)[pm.AUDIENCE_MAIN]["tokens"], 9000)


class AgeTests(unittest.TestCase):
    def test_age_is_shown_in_the_unit_the_operator_thinks_in(self) -> None:
        now_ms = 1_800_000_000_000
        base = {"time": now_ms}
        now = dt.datetime.fromtimestamp(now_ms / 1000, dt.UTC)
        five = dict(base, time=now_ms - 5 * 60_000)
        forty = dict(base, time=now_ms - 40 * 60_000)
        three = dict(base, time=now_ms - 3 * 3_600_000)
        self.assertEqual(pp._age(five, now), "5m")
        self.assertEqual(pp._age(forty, now), "40m")
        self.assertEqual(pp._age(three, now), "3h")


class CostRenderingTests(unittest.TestCase):
    def test_the_three_states_render_apart_from_each_other(self) -> None:
        self.assertEqual(pp.Cost(1234, measured=True).text(), "1,234")
        self.assertEqual(pp.Cost(1234).text(), "~1,234")
        self.assertEqual(pp.UNKNOWN.text(), "?")
        # "?" carries no number, and no convention puts a second tilde on "~1,234": both follow
        # from the three renderings above, so they are stated here rather than re-asserted.


class TabulaRasaTests(unittest.TestCase):
    """The documented reset has to be reachable from the screen it is documented on."""

    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.root = Path(self._dir.name)
        self.enabled = dict.fromkeys(pc.OPTION_IDS, False)

    def test_all_off_plus_two_empty_files_reaches_no_hoisted_prompt(self) -> None:
        (self.root / "SYSTEM.md").write_text("")
        (self.root / "CONTEXT.md").write_text("")
        staged_system = pc.compose_system_document(self.root, PLAN, self.enabled)
        staged_agents = pc.compose_agents_document(self.root, PLAN, "", self.enabled)
        self.assertEqual(staged_system, pc.EMPTY_PROMPT_SENTINEL + "\n")
        self.assertEqual(staged_agents, "")

    def test_every_fact_the_help_screen_carried_now_lives_on_the_tree(self) -> None:
        """The deleted overlay had four facts in it, and a screen cannot lose a fact by moving.

        It had them because the flat list had nowhere else to put them. The tree has the room, so
        each one belongs beside the row it explains - which is what this checks, rather than the
        wording of a help page nobody has to leave the diagram to read.
        """
        stage_files(self.root, {
            pc.SYSTEM_FILE: "", pc.CONTEXT_FILE: "", CONTRACT: "# the harness contract\n",
        })
        step = tree(self.root)
        flat = speaking(step)
        for fact in (
            pc.SYSTEM_FILE,
            pc.CONTEXT_FILE,
            CONTRACT,
            "An empty file is a decision",
            "counts as empty",
            "writes nothing to your files",
            "built-in",
        ):
            self.assertIn(fact, flat)
        self.assertIn(repr(pc.EMPTY_PROMPT_SENTINEL), flat)
        # What used to be the word for an overridden source is a glyph now. A glyph is not a fact
        # that can be lost by moving prose off the rows, but it can be lost by never drawing it, so
        # it is pinned here rather than left to the render tests.
        self.assertIn("- -", prose(step))

    def test_the_tree_states_the_fallback_a_blank_document_prevents(self) -> None:
        """``.`` looks like a typo, so the row that stages it has to say what it is for."""
        stage_files(self.root, {pc.SYSTEM_FILE: "", CONTRACT: "# the harness contract\n"})
        step = tree(self.root)
        rows = tree_rows(step)
        self.assertIn(pp.BLANK_NOTE, speaking(step))
        self.assertIn(pc.EMPTY_PROMPT_SENTINEL, pp.BLANK_NOTE)
        # The sentence hangs off the built-in row, and that row also says with a hollow mark that it
        # is not running: a reader who never opened the pane still cannot conclude Kimi's own prompt
        # is in play.
        marks = [row for row in rows if "built-in prompt" in row]
        self.assertEqual(len(marks), 1)
        self.assertIn("- -", marks[0])

    def test_the_context_step_invents_no_keys_of_its_own(self) -> None:
        """``a``/``n``/``r``/``i`` died here: a step may answer only what its footer advertises.

        The tree inherits the engine's generated table, so this fails the moment the panel
        special-cases a letter nobody printed - which is the exact shape of the old grammar.
        """
        step = tree(self.root)
        table = step.keys(step.initial(), View(can_go_back=True))
        base = navigation()
        expected = [
            *(item.action for item in base[:1]),
            BACK_BINDING.action,
            *(item.action for item in base[1:]),
        ]
        self.assertEqual([item.action for item in table], expected)
        spelled = {name for item in table for name in item.keys}
        for letter in ("a", "n", "r", "i"):
            self.assertNotIn(letter, spelled, f"{letter} would be a magic keystroke again")
        for name in ("?", "Space", "Enter", "Ctrl-R", "Backspace", "Up", "Down"):
            self.assertIn(name, spelled, f"{name} does something and must be printed")


class TerminalAssumptionTests(unittest.TestCase):
    """What the interface claims about the terminal, checked against a real one.

    The line editor used to stand or fall by three assumptions - that a command arrives only after
    a newline, that EOF is an answerable state, and that the screen admitted the buffering. All
    three went with the line editor. What replaces them is the single claim the modal does make,
    which is that a keypress is an answer by itself, and that is only observable through a tty
    driver rather than a pipe.
    """

    DRIVER = '''
import json, sys
from pathlib import Path

harness, workspace = (Path(arg) for arg in sys.argv[1:3])
sys.path[:0] = [str(harness), str(harness / "tools")]

import prompt_context as pc
import prompt_panel as pp
from tests.helpers import shipped_plan
from tui.app import View, run

graph = pp.ContextGraph(
    shipped_plan(),
    workspace,
    "module guidance text",
    {},
    dict(pc.DEFAULT_ENABLED),
    dict(pc.STATIC_DEFAULT),
)
step = pp.ContextStep(graph)
result = run(step, View(position=7, total=8, label="Context", can_go_back=True))
opening = graph.opening()
moved = {k: v for k, v in dict(result.value).items() if k in opening and opening[k] != v}
print("MOVED=" + json.dumps(moved, sort_keys=True))
sys.exit(result.status)
'''

    def setUp(self) -> None:
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        # Both documents empty, over a harness contract that is not: this is the shape where the
        # two static switches are the first two rows the cursor meets.
        self.workspace = Path(holder.name)
        stage_files(self.workspace, {
            pc.SYSTEM_FILE: "", pc.CONTEXT_FILE: "", CONTRACT: "# the harness contract\n",
        })
        driver = self.workspace / "drive.py"
        driver.write_text(self.DRIVER, encoding="utf-8")
        self.driver = driver
        self.env = {
            key: value
            for key, value in os.environ.items()
            if key not in ("COLUMNS", "LINES", "NO_COLOR", "TERM")
        }

    def answer(self, session) -> dict:
        return json.loads(session.screen.split("MOVED=")[1].splitlines()[0])

    def test_a_keypress_answers_without_a_newline(self) -> None:
        """Space moves a row, an arrow moves the cursor, and only Enter ends the step.

        Under the old line discipline none of this would have happened: the driver buffers until a
        newline, so the two switches would still read ``off`` and the answer would be the map the
        screen opened with. The key reference is opened and closed first, so the answer also has to
        survive a detour through the overlay.
        """
        session = run_in_pty(
            [sys.executable, str(self.driver), str(ROOT), str(self.workspace)],
            # The map interleaves the add-ons between its two blocks, so the second switch is three
            # rows down. Arrows are the only thing used to get there, which is the point.
            keys=[b"?", b"?", b" ", b"\x1b[B", b"\x1b[B", b"\x1b[B", b" ", b"\r"],
            expect=b"main system prompt",
            env=self.env,
        )
        self.assertEqual(session.status, flow.CONTINUE)
        # The key reference is the only place the overlay's title and the ``k`` alias are ever
        # painted, so their presence proves the ``?`` opened it - and that the two switches below
        # it still landed proves closing it cost the answer nothing.
        self.assertIn("Keys", session.screen, "the key reference never opened")
        self.assertIn("/k", session.screen, "the overlay listed no aliases")
        self.assertEqual(self.answer(session), {pc.STATIC_CONTEXT: pc.ON, pc.STATIC_SYSTEM: pc.ON})
        self.assertTrue(session.restored, "the terminal came back as it was found")

    def test_the_panel_installs_no_signal_handler(self) -> None:
        """Ctrl-C has to reach start.sh's cleanup trap, so nothing here may claim it.

        Read off the syntax tree: a source grep is tripped by the prose that explains the rule,
        and misses a handler reached through an import alias.
        """
        tree = ast.parse(Path(pp.__file__).read_text(encoding="utf-8"))
        imported: set[str] = set()
        attributes: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module or "")
            elif isinstance(node, ast.Attribute):
                attributes.add(node.attr)
        self.assertNotIn("signal", imported)
        self.assertNotIn("signal", attributes, "signal.signal(...) would install a handler")
        self.assertNotIn("SIGINT", attributes)


class NoWritesBeforeAcceptanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.runtime = Path(self._dir.name)
        (self.runtime / "model-policy.json").write_text(json.dumps(PLAN))

    def test_drawing_never_creates_the_prefs_file(self) -> None:
        pp.draw(PLAN, self.runtime, "m", dict(pc.DEFAULT_ENABLED), {},
                colour_on=False, remembered=False)
        self.assertFalse((self.runtime / pc.PREFS_FILE).exists())

    def test_configure_show_never_creates_the_prefs_file(self) -> None:
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = pp.main(["--runtime-dir", str(self.runtime), "--configure", "--show"])
        self.assertEqual(code, 0)
        self.assertIn("parallelism", buffer.getvalue())
        self.assertFalse((self.runtime / pc.PREFS_FILE).exists())


class OptionSetCompletenessTests(unittest.TestCase):
    def test_the_panel_governs_everything_the_composer_can_add(self) -> None:
        """A block outside the list is an injection the operator cannot see or refuse."""
        generated = {
            section.option
            for audience in policy.GUIDANCE_AUDIENCES
            for section in policy.guidance_sections(PLAN, audience)
        }
        self.assertTrue(generated <= set(pc.OPTION_IDS))
        self.assertIn(pc.OPTION_MODULE_GUIDANCE, pc.OPTION_IDS)
        self.assertEqual(set(pc.CONFIG_TOGGLES) | set(pc.ENV_TOGGLES),
                         set(pc.OPTION_IDS) - generated - {pc.OPTION_MODULE_GUIDANCE})

    def test_every_option_is_a_tree_row_in_the_order_the_panel_governs_them(self) -> None:
        """Neither renderer may hold a set of its own, and neither may need numbers to reach it.

        The old screen could offer nine options because nine is what a single digit labels; a row
        the cursor walks to has no such ceiling, so what is pinned here is the coverage and the
        order, with nothing in the way of either.
        """
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        root = Path(holder.name)
        stage_files(root, {pc.SYSTEM_FILE: "# voice\n", CONTRACT: "# contract\n"})
        step = tree(root)
        boxes = [node for node, _, _, _ in step.rows_in_order() if node.kind == pp.CHECK]
        self.assertTrue(all(node.editable for node in boxes), "a box the cursor cannot reach")
        # The map groups by destination, so an option's row is not free to sit anywhere: the groups
        # read in destination order, a block heads the group whose document it is, and the add-ons
        # follow it in the order the panel declares them. Built from those declarations rather than
        # spelled out, because the point is that the tree holds no list of its own either.
        expected: list[str] = []
        for target in pp.DESTINATIONS:
            block = pp.BLOCK_FOR.get(target)
            if block:
                expected.append(block)
            expected += [option.id for option in pc.OPTIONS if option.target == target]
        self.assertEqual([node.id for node in boxes], expected)

    def test_no_option_is_its_own_companion(self) -> None:
        for option in pc.OPTIONS:
            self.assertNotIn(option.id, option.companions)
            for other in option.companions:
                self.assertIn(other, pc.OPTION_IDS)

    def test_companions_are_mutual_so_the_gutter_marks_both_sides(self) -> None:
        for option in pc.OPTIONS:
            for other in option.companions:
                self.assertIn(option.id, pc.OPTION_BY_ID[other].companions, f"{option.id}/{other}")


class StartUpWiringTests(unittest.TestCase):
    """The panel only earns trust if the launch actually runs it and honours what it decided."""

    def setUp(self) -> None:
        self.script = (ROOT / "start.sh").read_text()
        self.text = "\n".join(
            line
            for line in self.script.splitlines()
            if not line.lstrip().startswith("#")
        )

    def test_the_panel_runs_after_selection_and_before_render(self) -> None:
        assemble = self.text.index("tools/modules.py assemble")
        panel = self.text.index("tools/prompt_panel.py")
        render = self.text.index("tools/render_runtime.py")
        self.assertLess(assemble, panel)
        self.assertLess(panel, render)

    def test_an_unattended_launch_still_draws_the_screen_it_acts_on(self) -> None:
        self.assertIn("non_interactive", self.text)
        start = self.text.index("tools/prompt_panel.py")
        self.assertIn("--plain", self.text[start - 200 : start + 200])

    def test_ctrl_c_still_aborts_the_whole_launch(self) -> None:
        self.assertIn("trap cleanup", (ROOT / "start.sh").read_text())


class StalenessNoticeTests(unittest.TestCase):
    """The restart hint that only a live stack can honestly tell you.

    Documents are staged and made immutable at launch, so an edit after that point has no effect
    until the next start. The panel is where that gets said - but only on the inspect path, because
    at startup this screen is drawn before the staging step and would otherwise call every file
    stale seconds before its new bytes ship.
    """

    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.root = Path(self._dir.name) / "ws"
        self.root.mkdir()
        (self.root / "AGENTS.md").write_text("# project rules\n" + "x" * 400 + "\n")
        self.all_on = dict(pc.DEFAULT_ENABLED)

    def screen(self, **kwargs: object) -> str:
        return pp.draw(
            PLAN, self.root, "module guidance", self.all_on, {},
            colour_on=False, remembered=False, **kwargs,  # type: ignore[arg-type]
        )

    def test_a_stale_document_is_reported_below_the_checklist(self) -> None:
        notice = "SYSTEM.md changed since it was staged - restart to apply it"
        self.assertIn(notice, self.screen(stale=[notice]))

    def test_a_screen_with_nothing_stale_invents_no_staleness(self) -> None:
        self.assertNotIn("since it was staged", self.screen())

    def capture(self, *argv: str) -> str:
        runtime = Path(self._dir.name) / "runtime"
        runtime.mkdir(exist_ok=True)
        (runtime / "model-policy.json").write_text(json.dumps(PLAN), encoding="utf-8")
        # A sidecar that disagrees with the workspace: exactly the state --show is meant to report
        # and a launch must not, because the launch is what is about to write the new bytes.
        (runtime / pc.SOURCES_FILE).write_text(
            json.dumps({"system": {"source": "SYSTEM.md", "digest": "staged-before"}}),
            encoding="utf-8",
        )
        (self.root / "SYSTEM.md").write_text("edited since then\n", encoding="utf-8")
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(pp.main(["--runtime-dir", str(runtime), "--root", str(self.root),
                                      "--plain", *argv]), 0)
        return out.getvalue()

    def test_only_the_inspect_path_compares_against_a_live_stack(self) -> None:
        """The same workspace with a stale sidecar: --show says so, a launch holds its peace."""
        self.assertIn("since it was staged", self.capture("--show"))
        self.assertNotIn("since it was staged", self.capture())

    def test_the_startup_path_does_not_ask_about_staleness(self) -> None:
        start = (ROOT / "start.sh").read_text()
        call = start[start.index("panel=(python3") : start.index('"${panel[@]}"')]
        self.assertIn("tools/prompt_panel.py", call)
        self.assertNotIn("--show", call, "the launch must take the quiet path above")


class InstructionBillTests(unittest.TestCase):
    """Kimi's 32 KB recommendation, quoted as a bill rather than enforced as a cap."""

    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.root = Path(self._dir.name) / "ws"
        self.root.mkdir()
        self.all_on = dict(pc.DEFAULT_ENABLED)

    def pieces(self, project_bytes: int, measured: dict | None = None):
        (self.root / "AGENTS.md").write_text("x" * project_bytes, encoding="utf-8")
        return pp.pieces(PLAN, self.root, "module guidance", self.all_on, measured, "primary")

    def test_a_normal_prompt_is_never_billed(self) -> None:
        (self.root / "AGENTS.md").write_text("# rules\n", encoding="utf-8")
        self.assertEqual(pp.over_limit(self.pieces(400)), ())

    def test_a_measurement_does_not_stop_billing_the_workspace_documents(self) -> None:
        """The bill is in bytes and a measurement is in tokens, so the text cannot be discarded.

        A measured row used to blank the project text as surplus to the diagram, which quietly
        dropped the workspace's own ``AGENTS.md`` from the 32 KB bill as soon as any measurement
        existed - the one case where an operator most wants the number to be current.
        """
        record = measured_record(PLAN, pm.AUDIENCE_MAIN, 900, 300, 300, 300)
        notice, = pp.over_limit(self.pieces(pc.KIMI_RECOMMENDED_MAX_INSTRUCTION_BYTES + 1, record))
        self.assertIn("over Kimi's recommended 32 KB", notice)

    def test_an_oversized_prompt_is_billed_in_kimis_own_units(self) -> None:
        notice, = pp.over_limit(self.pieces(pc.KIMI_RECOMMENDED_MAX_INSTRUCTION_BYTES + 1))
        self.assertIn("over Kimi's recommended 32 KB", notice)
        self.assertIn("Nothing here is truncated", notice)

    def test_the_bill_profits_nothing_by_being_vague_about_consequence(self) -> None:
        """A warning that implied truncation would push the operator to trim text that is safe."""
        notice, = pp.over_limit(self.pieces(pc.KIMI_RECOMMENDED_MAX_INSTRUCTION_BYTES + 1))
        self.assertNotIn("will be", notice)
        self.assertNotIn("dropped", notice)


if __name__ == "__main__":
    unittest.main()
