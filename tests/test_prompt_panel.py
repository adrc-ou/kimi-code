"""The startup panel: what it draws, what it accepts, and what it refuses to do.

The panel is the harness's only trust surface - the one place an operator can see the whole
resolved context before it is spent. These tests hold the properties that make it trustworthy
rather than pretty: the diagram sums, no figure is presented as measured when it is not, every
option is reachable, a half-selected pair warns exactly once, and nothing at all happens to the
operator's choices when they never had a terminal.
"""

from __future__ import annotations

import ast
import datetime as dt
import io
import json
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

from tests.helpers import measured_record, shipped_plan  # noqa: E402

PLAN = shipped_plan()


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
        pp.warning(enabled)
        after = pp.apply_command("n", enabled)[0]
        self.assertEqual(after[parent.id], False, "all-off must not silently re-enable a pair")
        self.assertEqual(pp.warning(after), "", "nothing on can hardly be a half-selected pair")


class CommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.enabled = dict(pc.DEFAULT_ENABLED)

    def test_a_digit_toggles_its_option(self) -> None:
        for index, option in enumerate(pc.OPTIONS, start=1):
            after, done = pp.apply_command(str(index), dict(self.enabled))
            self.assertFalse(done)
            self.assertIsNot(after[option.id], self.enabled[option.id], option.id)

    def test_an_out_of_range_digit_changes_nothing(self) -> None:
        after, done = pp.apply_command(str(len(pc.OPTIONS) + 3), self.enabled)
        self.assertFalse(done)
        self.assertEqual(after, self.enabled)

    def test_enter_accepts(self) -> None:
        after, done = pp.apply_command("", self.enabled)
        self.assertTrue(done)
        self.assertEqual(after, self.enabled)

    def test_all_off_reaches_the_tabula_rasa_selection(self) -> None:
        after, _ = pp.apply_command("n", self.enabled)
        self.assertFalse(any(after.values()))
        self.assertEqual(set(after), set(pc.OPTION_IDS))

    def test_reset_restores_this_builds_defaults(self) -> None:
        after, _ = pp.apply_command("n", self.enabled)
        after, _ = pp.apply_command("r", after)
        self.assertEqual(after, dict(pc.DEFAULT_ENABLED))

    def test_an_unknown_command_is_harmless(self) -> None:
        after, done = pp.apply_command("zzz", self.enabled)
        self.assertFalse(done)
        self.assertEqual(after, self.enabled)

    def test_commands_are_case_insensitive(self) -> None:
        self.assertEqual(
            pp.apply_command("N", self.enabled)[0],
            pp.apply_command("n", self.enabled)[0],
        )


class ExplainTests(unittest.TestCase):
    def test_generated_blocks_name_the_function_that_writes_them(self) -> None:
        text = pp.explain("1", PLAN, "m")
        self.assertIn("tools/policy.py", text)

    def test_a_switch_says_so_instead_of_claiming_text(self) -> None:
        index = next(
            i for i, o in enumerate(pc.OPTIONS, start=1)
            if not pp.option_text(o, PLAN, "m")
        )
        self.assertIn("no text", pp.explain(str(index), PLAN, "m"))

    def test_an_unparsable_answer_asks_rather_than_guessing(self) -> None:
        self.assertIn("Type i", pp.explain("", PLAN, "m"))


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
        plan, enabled, remembered = pp.load_context(self.runtime)
        self.assertFalse(enabled[policy.OPTION_PARALLELISM])
        self.assertTrue(remembered)
        self.assertIn("lanes", plan)

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

    def test_the_prefs_file_never_leaves_the_options_it_knows(self) -> None:
        pc.save_prefs(self.runtime / pc.PREFS_FILE, dict(pc.DEFAULT_ENABLED))
        mode = self.runtime / pc.PREFS_FILE
        self.assertEqual(mode.stat().st_mode & 0o777, 0o600)
        stored = json.loads(mode.read_text())
        self.assertEqual(set(stored), set(pc.OPTION_IDS))

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

    def test_the_recipe_on_the_help_screen_matches_the_code(self) -> None:
        for name in ("SYSTEM.md", "CONTEXT.md"):
            self.assertIn(name, pp.HELP)
        self.assertIn(pc.EMPTY_PROMPT_SENTINEL, pp.HELP)

    def test_the_help_screen_states_the_fallback_it_prevents(self) -> None:
        self.assertIn("built-in", pp.HELP)

    def test_the_help_screen_reaches_the_terminal_from_a_command(self) -> None:
        """`?` is advertised on the prompt line, so it has to bring the recipe with it.

        Checked through the loop that answers the keystroke, and against a sentence that exists
        nowhere on the drawn panel: the panel already prints a bare ``?`` for every figure it
        cannot price, which is what the earlier version of this test was matching.
        """
        stdin, sys.stdin = sys.stdin, io.StringIO("?\n")
        out = io.StringIO()
        try:
            with redirect_stdout(out):
                selection = pp.interactive_loop(
                    PLAN, self.root, "m", self.enabled, {},
                    colour_on=False, remembered=False,
                )
        finally:
            sys.stdin = stdin
        self.assertIn("An empty file is a decision", out.getvalue())
        self.assertEqual(selection, self.enabled, "asking for help changes no choice")


class TerminalAssumptionTests(unittest.TestCase):
    def test_read_line_returns_none_at_eof(self) -> None:
        stdin, sys.stdin = sys.stdin, io.StringIO("")
        try:
            self.assertIsNone(pp.read_line("go: "))
        finally:
            sys.stdin = stdin

    def test_read_line_takes_one_command(self) -> None:
        stdin, sys.stdin = sys.stdin, io.StringIO("3\n")
        try:
            self.assertEqual(pp.read_line("go: "), "3")
        finally:
            sys.stdin = stdin

    def test_the_screen_says_that_a_key_needs_enter(self) -> None:
        # A line-buffered terminal that pretends otherwise trains the user to distrust the
        # screen, so the claim is checked against the text on it rather than against a comment.
        self.assertIn("line-buffered", pp.HELP)
        self.assertIn("press Enter", pp.HELP)

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

    def test_every_option_is_reachable_by_the_number_the_screen_advertises(self) -> None:
        """Both the prompt line and the help screen offer digits 1-9, and no further."""
        self.assertLessEqual(len(pc.OPTIONS), 9, "the screen advertises single-digit selection")
        for index in range(1, len(pc.OPTIONS) + 1):
            self.assertNotEqual(pp.apply_command(str(index), dict(pc.DEFAULT_ENABLED))[0],
                                pc.DEFAULT_ENABLED, f"option {index} does not respond")

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
