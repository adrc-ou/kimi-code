#!/usr/bin/env python3
"""Token accounting for the startup panel.

The estimator is a reimplementation of arithmetic that lives in a JavaScript bundle, so it is
pinned here against a hand-computed reference rather than against itself. The rest of the file is
the read path: what a ``profile.bind`` log has to contain before the panel will show a number, and
what it does when the log is torn, absent, or from a lane it has never heard of.
"""

from __future__ import annotations

import io
import json
import math
import re
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# The repository root as well as tools/: unittest only puts the start directory on the path, not
# its parent, so `tests.helpers` needs the root added explicitly to resolve under every way of
# running the suite - `discover -s tests`, `python -m unittest tests.test_x`, and a bare
# `python -m unittest test_x` from inside tests/.
for _directory in (ROOT, ROOT / "tools"):
    if str(_directory) not in sys.path:
        sys.path.insert(0, str(_directory))

import prompt_context  # noqa: E402
import prompt_measure as pm  # noqa: E402

from tests.helpers import lane_alias, shipped_plan  # noqa: E402

#: The one line :func:`prompt_measure.report` prints its figures on, matched so a test can add the
#: parts up instead of eyeballing the screen.
BREAKDOWN_RE = re.compile(
    r"(?P<tokens>[\d,]+) tokens in [\d,]+ bytes:"
    r" framing (?P<framing>[\d,]+), harness (?P<harness>[\d,]+), project (?P<project>[\d,]+)"
)


def reference_estimate(text: str) -> int:
    """The same rule, spelled out a second time, so a refactor cannot quietly change the meaning."""
    ascii_points = [c for c in text if ord(c) <= 127]
    return math.ceil(len(ascii_points) / 4) + (len(list(text)) - len(ascii_points))


class EstimatorTests(unittest.TestCase):
    VECTORS = {
        "": 0,
        "a": 1,
        "abcd": 1,
        "abcde": 2,
        "日本語": 3,
        "ab日本": 3,  # ceil(2/4) + 2
        "\U0001f600": 1,
        "héllo": 2,
        "line\nbreak\tand é": 5,
    }

    def test_kimi_pricing_is_a_quarter_of_ascii_plus_every_non_ascii_point(self):
        for text, wanted in self.VECTORS.items():
            with self.subTest(text=text):
                self.assertEqual(pm.estimate_tokens(text), wanted)
                self.assertEqual(pm.estimate_tokens(text), reference_estimate(text))

    def test_an_astral_character_is_one_point_not_two_units(self):
        # JavaScript's for-of and Python's str iteration both walk code points, so a surrogate pair
        # costs one token here and one there. A UTF-16 length would say two, which is the mistake
        # this pins: two emoji are priced as 2, not 4.
        self.assertEqual(pm.estimate_tokens("\U0001f600\U0001f600"), 2)

    def test_a_long_prompt_matches_the_independent_reference(self):
        text = (ROOT / "runtime" / "AGENTS.md").read_text(encoding="utf-8")
        self.assertEqual(pm.estimate_tokens(text), reference_estimate(text))
        self.assertGreater(pm.estimate_tokens(text), 100)

    def test_json_pricing_uses_jsonstringify_shape_not_python_style(self):
        self.assertEqual(pm.stringify_json({"b": 1, "a": [1, 2]}), '{"b":1,"a":[1,2]}')
        self.assertEqual(pm.stringify_json({"k": "é"}), '{"k":"é"}')

    def test_tool_schemas_cost_their_name_description_and_parameters(self):
        tools = [{"name": "Read", "description": "read a file", "parameters": {"type": "object"}}]
        self.assertEqual(
            pm.estimate_tools_tokens(tools),
            pm.estimate_tokens("Read")
            + pm.estimate_tokens("read a file")
            + pm.estimate_tokens('{"type":"object"}'),
        )

    def test_a_tool_with_no_schema_is_priced_as_an_empty_one(self):
        # The bundle assumes parameters exists; this harness would rather over-charge by the
        # two characters of "{}" than raise while drawing a panel.
        self.assertEqual(pm.estimate_tools_tokens([{}]), pm.estimate_tokens("{}"))


class RegionTests(unittest.TestCase):
    HARNESS = "harness contract text, long enough to round.\n\n"
    PROJECT = "the user's own project instructions.\n"

    def chunk(self, path: str, text: str) -> str:
        """One hoisted file as Kimi writes it: the marker line belongs to the file it announces."""
        return f"{pm.PART_MARKER}{path}{pm.PART_MARKER_END}\n{text}"

    def prompt(self, harness: str, project: str, framing: str = "Kimi framing here\n") -> str:
        return framing + self.chunk(pm.HARNESS_AGENTS_PATH, harness) + self.chunk(
            "/workspace/AGENTS.md", project)

    def test_the_harness_document_is_told_apart_from_the_users_own_files(self):
        regions = pm.split_regions(self.prompt(self.HARNESS, self.PROJECT))
        self.assertEqual(regions.harness, self.chunk(pm.HARNESS_AGENTS_PATH, self.HARNESS))
        self.assertEqual(regions.project, self.chunk("/workspace/AGENTS.md", self.PROJECT))
        self.assertIn("Kimi framing", regions.prefix)
        self.assertNotIn("contract text", regions.prefix)

    def test_a_row_always_adds_up_to_the_measured_whole(self):
        # The panel draws boxes that must sum to its own total, so framing is whatever is left
        # rather than an independently-rounded figure.
        for pair in ((self.HARNESS, self.PROJECT), ("", ""), ("a", "b" * 7), ("x" * 9, "")):
            with self.subTest(pair=pair):
                tokens = pm.split_regions(self.prompt(*pair)).tokens
                self.assertEqual(
                    tokens["framing"] + tokens["harness"] + tokens["project"], tokens["total"]
                )

    def test_the_independent_prefix_figure_agrees_with_the_residual_within_rounding(self):
        regions = pm.split_regions(self.prompt(self.HARNESS * 3, self.PROJECT * 5))
        self.assertLessEqual(abs(regions.framing_estimate - regions.tokens["framing"]), 2)

    def test_a_prompt_with_no_markers_is_all_framing(self):
        regions = pm.split_regions("nothing hoisted here")
        self.assertEqual((regions.harness, regions.project), ("", ""))
        self.assertEqual(regions.tokens["harness"], 0)
        self.assertEqual(regions.tokens["framing"], regions.tokens["total"])

    def test_several_project_files_all_count_as_the_users_own(self):
        prompt = self.prompt(self.HARNESS, "root\n") + (
            f"{pm.PART_MARKER}/workspace/sub/AGENTS.md{pm.PART_MARKER_END}\nsub\n"
        )
        regions = pm.split_regions(prompt)
        self.assertEqual(regions.harness, self.chunk(pm.HARNESS_AGENTS_PATH, self.HARNESS))
        self.assertIn("sub", regions.project)

    def test_a_marker_that_never_closes_costs_the_rest_of_the_prompt(self):
        regions = pm.split_regions(f"head\n{pm.PART_MARKER}/workspace/AGENTS.md\nbody")
        self.assertEqual(regions.harness, "")
        self.assertIn("body", regions.project)


class BindLogTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.root = Path(self._dir.name)
        self.plan = shipped_plan()

    def write(self, agent: str, records: list[str]) -> Path:
        wire = self.root / "sessions" / "wd_x" / "session_y" / "agents" / agent / pm.WIRE_NAME
        wire.parent.mkdir(parents=True, exist_ok=True)
        wire.write_text("\n".join(records) + "\n", encoding="utf-8")
        return wire

    def bind(self, audience: str, prompt: str, when: int) -> str:
        return json.dumps(
            {
                "type": pm.BIND_TYPE,
                "agentId": "main" if audience == "main" else "child1",
                "modelAlias": lane_alias(self.plan, audience),
                "profileName": "agent" if audience == "main" else "explore",
                "systemPrompt": prompt,
                "time": when,
            }
        )

    def test_a_torn_or_foreign_line_is_skipped_not_fatal(self):
        path = self.write(
            "main",
            [
                '{"type":"metadata"}',
                self.bind("main", "first", 1000)[:60],
                json.dumps({"type": "profile.bind"}),
                json.dumps({"type": "profile.bind", "systemPrompt": 7}),
                self.bind("main", "second", 2000),
            ],
        )
        records = pm.bind_records(path)
        self.assertEqual([r["systemPrompt"] for r in records], ["second"])

    def test_a_missing_log_reads_as_nothing_rather_than_an_error(self):
        self.assertEqual(pm.bind_records(self.root / "absent.jsonl"), [])

    def test_the_newest_prompt_per_audience_wins_across_files(self):
        self.write("main", [self.bind("main", "older", 1000), self.bind("main", "newer", 2000)])
        self.write(
            "child1",
            [self.bind("subagent", "child", 1500), self.bind("subagent", "newest child", 2500)],
        )
        found = pm.newest_binds(self.root, self.plan)
        self.assertEqual(found["main"]["systemPrompt"], "newer")
        self.assertEqual(found["subagent"]["systemPrompt"], "newest child")

    def test_an_alias_this_plan_does_not_declare_is_ignored(self):
        self.write("main", ['{"type":"profile.bind","modelAlias":"an-alias-this-plan-'
                            'never-publishes","systemPrompt":"x","time":1}'])
        self.assertEqual(pm.newest_binds(self.root, self.plan), {})

    def test_a_renamed_session_tree_is_still_found(self):
        # The glob is Kimi's layout and the fallback is ours; an upgrade that re-nests the logs
        # must cost a measurement, never a launch.
        wire = self.root / "deeply" / "nested" / pm.WIRE_NAME
        wire.parent.mkdir(parents=True)
        wire.write_text(self.bind("main", "found anyway", 5) + "\n", encoding="utf-8")
        self.assertIn("main", pm.newest_binds(self.root, self.plan))

    def test_measuring_produces_one_reconciling_row_per_audience(self):
        prompt = self.prompt_for("the harness said this\n", "and the project said this\n")
        self.write("main", [self.bind("main", prompt, 1_700_000_000_000)])
        rows = pm.measure(self.root, self.plan, image="sha256:abc", prefs="prefs-hash")
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["audience"], "main")
        self.assertEqual(row["image"], "sha256:abc")
        self.assertEqual(row["inputCap"], self.plan["lanes"]["primary"]["input_tokens"])
        self.assertEqual(
            row["kimiFraming"] + row["harnessContract"] + row["projectInstructions"], row["tokens"]
        )
        self.assertEqual(row["when"], "2023-11-14T22:13:20+00:00")

    def prompt_for(self, harness: str, project: str) -> str:
        return (
            f"framing\n{pm.PART_MARKER}{pm.HARNESS_AGENTS_PATH}{pm.PART_MARKER_END}\n"
            f"{harness}{pm.PART_MARKER}/workspace/AGENTS.md{pm.PART_MARKER_END}\n{project}"
        )


class ToolSchemaTests(unittest.TestCase):
    """The schemas ride beside the prompt, so pricing them is a separate reading.

    A wire log is an append-only record of a live process, which means the reader has to survive
    the cases a fixture would rather not contain: no snapshot at all, a snapshot with no tools,
    several snapshots where the newest differs from the first, and one agent that never delegated.
    """

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.root = Path(self._dir.name)

    def write(self, agent: str, *records: dict) -> None:
        wire = self.root / "sessions" / "wd_x" / "s" / "agents" / agent / pm.WIRE_NAME
        wire.parent.mkdir(parents=True, exist_ok=True)
        wire.write_text(
            "".join(json.dumps(record) + "\n" for record in records), encoding="utf-8"
        )

    def snapshot(self, names: list[str], when: int = 1_700_000_000_000) -> dict:
        return {
            "type": pm.TOOLS_TYPE,
            "agentId": "main",
            "time": when,
            "tools": [{"name": n, "description": "", "parameters": {}} for n in names],
        }

    def test_a_snapshot_is_priced_by_the_same_arithmetic_kimi_uses(self):
        self.write("main", self.snapshot(["Read", "Write"]))
        found = pm.newest_tools(self.root)
        self.assertEqual(found["main"]["count"], 2)
        self.assertEqual(
            found["main"]["tokens"],
            pm.estimate_tools_tokens(self.snapshot(["Read", "Write"])["tools"]),
        )

    def test_the_newest_schema_set_wins_because_that_is_the_one_a_request_sends(self):
        self.write(
            "main",
            self.snapshot(["Read"], when=1_700_000_000_000),
            self.snapshot(["Read", "Write", "Bash"], when=1_700_000_060_000),
        )
        self.assertEqual(pm.newest_tools(self.root)["main"]["count"], 3)

    def test_each_agent_pays_for_the_tools_it_was_given(self):
        self.write("main", self.snapshot(["Read", "Write", "Bash", "Edit"]))
        self.write("sub-1", self.snapshot(["Read"]))
        found = pm.newest_tools(self.root)
        self.assertEqual({k: v["count"] for k, v in found.items()}, {"main": 4, "sub-1": 1})
        self.assertGreater(found["main"]["tokens"], found["sub-1"]["tokens"])

    def test_a_log_with_no_snapshot_prices_as_absent(self):
        self.write("main", {"type": pm.BIND_TYPE, "systemPrompt": "x", "time": 1})
        self.assertEqual(pm.newest_tools(self.root), {})

    def test_a_malformed_or_empty_snapshot_costs_nothing_rather_than_raising(self):
        self.write(
            "main",
            {"type": pm.TOOLS_TYPE, "time": 1, "tools": "not a list"},
            {"type": pm.TOOLS_TYPE, "time": 2, "tools": []},
        )
        self.write("sub", {"type": pm.TOOLS_TYPE, "time": 3, "tools": [{}]})
        found = pm.newest_tools(self.root)
        self.assertNotIn("main", found, "a schema set with nothing in it has nothing to price")
        self.assertEqual(found["sub"]["count"], 1)

    def test_the_figure_lands_in_history_so_a_later_launch_can_see_it(self):
        prompt = (
            f"framing\n{pm.PART_MARKER}{pm.HARNESS_AGENTS_PATH}{pm.PART_MARKER_END}\ncontract\n"
        )
        self.write("main", self.snapshot(["Read", "Write"]))
        wire = self.root / "sessions" / "wd_x" / "s" / "agents" / "main" / pm.WIRE_NAME
        plan = shipped_plan()
        record = {
            "type": pm.BIND_TYPE,
            "agentId": "main",
            "modelAlias": lane_alias(plan, pm.AUDIENCE_MAIN),
            "profileName": "agent",
            "systemPrompt": prompt,
            "time": 1_700_000_120_000,
        }
        wire.write_text(
            wire.read_text(encoding="utf-8") + json.dumps(record) + "\n", encoding="utf-8"
        )
        row = pm.measure(self.root, plan)[0]
        self.assertGreater(row["toolSchemaTokens"], 0)
        self.assertEqual(row["toolSchemaCount"], 2)
        self.assertEqual(
            row["tokens"],
            pm.estimate_tokens(prompt),
            "the prompt figure stays the prompt figure; schemas are beside it, not inside it",
        )


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.path = Path(self._dir.name) / prompt_context.MEASUREMENTS_FILE

    def row(self, audience: str, tokens: int, when: datetime) -> dict:
        # The alias is carried through history untouched and is priced only when a row is drawn,
        # so a fixture name is enough here; the read path never resolves it against a plan.
        return {
            "audience": audience,
            "tokens": tokens,
            "time": int(when.timestamp() * 1000),
            "modelAlias": "fixture-primary",
        }

    def test_history_appends_and_never_rewrites(self):
        now = datetime.now(UTC)
        pm.append_history(self.path, [self.row("main", 100, now)])
        pm.append_history(self.path, [self.row("main", 120, now + timedelta(minutes=5))])
        self.assertEqual([r["tokens"] for r in pm.read_history(self.path)], [100, 120])

    def test_the_file_is_private(self):
        pm.append_history(self.path, [self.row("main", 1, datetime.now(UTC))])
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_nothing_to_record_leaves_no_file_behind(self):
        pm.append_history(self.path, [])
        self.assertFalse(self.path.exists())

    def test_unreadable_or_unrelated_lines_are_not_history(self):
        self.path.write_text("junk\n{}\n[]\n" + json.dumps(self.row("main", 5, datetime.now(UTC)))
                             + "\n" + json.dumps({"audience": "other", "tokens": 9}) + "\n",
                             encoding="utf-8")
        self.assertEqual(len(pm.read_history(self.path)), 1)
        self.assertEqual(pm.read_history(self.path.parent / "absent.jsonl"), [])

    def test_the_newest_observation_per_audience_is_what_gets_displayed(self):
        now = datetime.now(UTC)
        latest = pm.latest_by_audience(
            [
                self.row("main", 100, now - timedelta(hours=2)),
                self.row("subagent", 90, now - timedelta(hours=1)),
                self.row("main", 110, now),
            ]
        )
        self.assertEqual(latest["main"]["tokens"], 110)
        self.assertEqual(latest["subagent"]["tokens"], 90)

    def test_measured_has_to_mean_measured_recently(self):
        moment = datetime(2026, 9, 19, 12, 0, tzinfo=UTC)
        row = self.row("main", 1, moment)
        self.assertEqual(pm.age_minutes(row, moment + timedelta(minutes=90)), 90)
        self.assertGreaterEqual(pm.age_minutes({}), 0)


class DenominatorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.plan = shipped_plan()

    def test_the_denominator_is_the_input_cap_because_that_is_what_binds_a_request(self):
        # The numbers themselves live only in ./models and ./providers, so what is pinned here is
        # which field is the denominator and the relationship that makes it the binding one.
        for name, lane in self.plan["lanes"].items():
            with self.subTest(lane=name):
                self.assertEqual(pm.input_cap(self.plan, name), lane["input_tokens"])
                self.assertLess(pm.input_cap(self.plan, name), lane["context_tokens"])
        self.assertIsNone(pm.input_cap(self.plan, "no-lane-by-this-name"))

    def test_the_long_lane_is_the_main_audience_on_its_own_cap(self):
        # Not a third row in the diagram: the record says which alias was bound, so the
        # percentage is against the ceiling that request actually had.
        for name, lane in self.plan["lanes"].items():
            with self.subTest(lane=name):
                self.assertEqual(pm.cap_for_alias(self.plan, lane["alias"]), lane["input_tokens"])
        self.assertIsNone(pm.cap_for_alias(self.plan, "an-alias-this-plan-never-publishes"))
        self.assertEqual(set(pm.caps(self.plan)), set(self.plan["lanes"]))
        long_lane = self.plan["lanes"].get("long")
        if long_lane is None:
            self.skipTest("this selection has no long lane")
        self.assertGreater(
            pm.cap_for_alias(self.plan, long_lane["alias"]),
            pm.input_cap(self.plan, "primary"),
            "the point of resolving by alias is that the long lane is not priced as primary",
        )

    def test_windows_are_larger_than_caps_so_a_window_denominator_would_lie(self):
        for name, lane in self.plan["lanes"].items():
            with self.subTest(name=name):
                self.assertLess(lane["input_tokens"], lane["context_tokens"])


class ReportTests(unittest.TestCase):
    """``--report`` is what ``./prompts.sh --live`` prints, so it is answered, not styled.

    The properties that matter are the ones an operator would act on: the printed parts add up to
    the printed whole, the percentage is against the cap of the lane that actually served the
    request, unsubstituted placeholders are visible as text, and a number that is not there is
    reported as absent rather than as zero.
    """

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.root = Path(self._dir.name)
        self.plan = shipped_plan()

    def schemas(self, audience: str, names: list[str]) -> None:
        wire = self.root / "sessions" / "wd_x" / "s" / "agents" / audience / pm.WIRE_NAME
        with wire.open("a", encoding="utf-8") as log:
            log.write(
                json.dumps(
                    {
                        "type": pm.TOOLS_TYPE,
                        "agentId": audience,
                        "time": 1_700_000_060_000,
                        "tools": [
                            {
                                "name": n,
                                "description": f"does {n}",
                                "parameters": {"type": "object"},
                            }
                            for n in names
                        ],
                    }
                )
                + "\n"
            )

    def bind(self, audience: str, prompt: str, when: int = 1_700_000_000_000) -> None:
        wire = self.root / "sessions" / "wd_x" / "s" / "agents" / audience / pm.WIRE_NAME
        wire.parent.mkdir(parents=True, exist_ok=True)
        wire.write_text(
            json.dumps(
                {
                    "type": pm.BIND_TYPE,
                    "agentId": audience,
                    "modelAlias": lane_alias(self.plan, audience),
                    "profileName": "agent" if audience == "main" else "explore",
                    "systemPrompt": prompt,
                    "time": when,
                }
            )
            + "\n",
            encoding="utf-8",
        )

    def prompt(self, harness: str, project: str) -> str:
        return (
            f"framing\n{pm.PART_MARKER}{pm.HARNESS_AGENTS_PATH}{pm.PART_MARKER_END}\n"
            f"{harness}{pm.PART_MARKER}/workspace/AGENTS.md{pm.PART_MARKER_END}\n{project}"
        )

    def capture(
        self, history: Path | None = None, now: datetime | None = None
    ) -> tuple[int, str, str]:
        buffer, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(buffer), redirect_stderr(errors):
            code = pm.report(self.root, self.plan, history, out=buffer, now=now)
        return code, buffer.getvalue(), errors.getvalue()

    def test_no_record_is_an_answer_with_a_failing_exit_code(self):
        code, _, errors = self.capture()
        self.assertEqual(code, 1)
        self.assertIn(pm.BIND_TYPE, errors)

    def test_the_parts_it_prints_add_up_to_the_whole_it_prints(self):
        self.bind("main", self.prompt("harness text here\n", "project text here\n"))
        _, screen, _ = self.capture()
        found = BREAKDOWN_RE.search(screen)
        self.assertIsNotNone(found, screen)
        parts = [int(found[group]) for group in ("framing", "harness", "project")]
        self.assertEqual(int(found["tokens"]), sum(parts))

    def test_the_percentage_uses_the_cap_of_the_lane_that_served_the_request(self):
        self.bind("subagent", self.prompt("harness\n", "project\n"))
        _, screen, _ = self.capture()
        cap = self.plan["lanes"]["subagent"]["input_tokens"]
        self.assertIn(f"of the {cap:,}-token input cap", screen)
        # The negative half only carries information when the lane that did not serve has a
        # different ceiling. Where both resolve equal, that figure is the right answer anyway
        # and the two assertions would contradict each other.
        other = self.plan["lanes"]["primary"]["input_tokens"]
        if other != cap:
            self.assertNotIn(f"{other:,}", screen)

    def test_a_placeholder_kimi_never_received_is_shown_as_the_text_it_became(self):
        self.bind("main", self.prompt("harness ${base_prampt}\n", "project\n"))
        _, screen, _ = self.capture()
        self.assertIn("unsubstituted: base_prampt", screen)

    def test_a_clean_prompt_says_none_rather_than_printing_nothing(self):
        self.bind("main", self.prompt("harness\n", "project\n"))
        _, screen, _ = self.capture()
        self.assertIn("unsubstituted: none", screen)

    def test_a_placeholder_named_inside_code_is_documentation_not_a_leak(self):
        """The contract explains ``${base_prompt}`` to the operator, in backticks, on purpose."""
        prompt = self.prompt(
            "harness text here\n", "write `${base_prompt}` to wrap it, or ``${cwd}`` inline\n"
        )
        self.bind("main", prompt)
        _, screen, _ = self.capture()
        self.assertIn("unsubstituted: none", screen)

    def test_a_bare_placeholder_still_shows_even_when_the_file_also_has_code(self):
        prompt = self.prompt("harness `${cwd}` text\n", "and a real one: ${never_given}\n")
        self.bind("main", prompt)
        _, screen, _ = self.capture()
        self.assertIn("unsubstituted: never_given", screen)
        self.assertNotIn("cwd", screen.split("unsubstituted:")[1])

    def test_an_absent_history_reports_absence_not_zero(self):
        self.bind("main", self.prompt("harness\n", "project\n"))
        _, screen, _ = self.capture()
        self.assertIn("previous measurement: none recorded", screen)

    def test_the_schema_figure_is_printed_beside_the_prompt_and_not_inside_it(self):
        """Adding it to the sum would break the one property the report is trusted for."""
        prompt = self.prompt("harness text here\n", "project text here\n")
        self.bind("main", prompt)
        self.schemas("main", ["Read", "Write"])
        _, screen, _ = self.capture()
        found = BREAKDOWN_RE.search(screen)
        self.assertIsNotNone(found, screen)
        self.assertEqual(
            int(found["tokens"]),
            sum(int(found[group]) for group in ("framing", "harness", "project")),
            "the printed prompt breakdown must still close with schemas on screen",
        )
        self.assertIn("beside the prompt and not clearable", screen)
        self.assertIn("2 tools", screen)

    def test_a_log_that_never_sent_a_snapshot_prints_no_schema_line(self):
        self.bind("main", self.prompt("harness text here\n", "project text here\n"))
        _, screen, _ = self.capture()
        self.assertNotIn("tool schemas", screen)

    def test_a_previous_observation_is_shown_as_a_delta_and_an_age(self):
        prompt = self.prompt("harness\n", "project\n")
        self.bind("main", prompt)
        history = self.root / "prompt-measurements.jsonl"
        pm.append_history(history, pm.measure(self.root, self.plan))
        # A longer prompt on the second read has to show as growth, or the delta is decoration.
        self.bind("main", self.prompt("harness " + "longer " * 40 + "\n", "project\n"), when=2)
        _, screen, _ = self.capture(history)
        self.assertIn("previous measurement:", screen)
        self.assertIn("delta +", screen)
        # The name promises an age too, and the age is what tells an operator whether they are
        # comparing this session against the last one or against a month-old run. Pin the clock
        # seven minutes after the record so the figure is the one under test.
        earlier = pm.read_history(history)[0]
        then = datetime.fromtimestamp(int(earlier["time"]) / 1000, UTC) + timedelta(minutes=7)
        _, aged, _ = self.capture(history, now=then)
        self.assertIn("7 min ago", aged)


class CliTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.root = Path(self._dir.name)
        self.out = self.root / "prompt-measurements.jsonl"
        self.plan = self.root / "model-policy.json"
        self.plan.write_text(json.dumps({"lanes": {}}), encoding="utf-8")
        self.sessions = self.root / "home"

    def run_cli(self, *extra: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [
                sys.executable,
                str(ROOT / "tools" / "prompt_measure.py"),
                "--sessions-dir", str(self.sessions),
                "--plan", str(self.plan),
                "--out", str(self.out),
                *extra,
            ],
            capture_output=True, text=True, timeout=60,
        )

    def test_no_records_yet_is_a_normal_exit_with_no_history(self):
        result = self.run_cli()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.out.exists())
        self.assertIn("yet under", result.stderr)

    def test_report_forwards_a_missing_record_as_a_failing_exit(self):
        """The one mode where the exit code is an answer rather than a launch that gave up.

        Write mode says "no records yet" and returns zero; report mode is a question somebody
        asked on purpose, so the same empty log has to exit non-zero. The wording is shared
        between the two, which is why this drives the command line rather than ``report()``.
        """
        result = self.run_cli("--report")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn(pm.BIND_TYPE, result.stderr)

    def test_an_unreadable_plan_is_swallowed_because_a_measurement_is_not_a_launch(self):
        self.plan.write_text("{not json", encoding="utf-8")
        result = self.run_cli()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("skipped", result.stderr)

    def test_a_missing_sessions_directory_is_swallowed_too(self):
        self.assertEqual(self.run_cli("--sessions-dir", str(self.root / "gone")).returncode, 0)

    def test_a_record_becomes_exactly_one_appended_row(self):
        prompt = "framing\n<!-- From: /home/agent/.kimi-code/AGENTS.md -->\ncontract text here\n"
        wire = self.sessions / "sessions" / "wd" / "se" / "agents" / "main" / pm.WIRE_NAME
        wire.parent.mkdir(parents=True)
        wire.write_text(
            json.dumps({
                "type": "profile.bind", "agentId": "main", "modelAlias": "fixture-primary",
                "profileName": "agent", "systemPrompt": prompt, "time": 1700000000000,
            }) + "\n", encoding="utf-8")
        # A plan of its own, so nothing in this test reads the shipped definitions: the CLI's job
        # is to append one row per launch, and the numbers are only here to be resolvable.
        self.plan.write_text(json.dumps({"lanes": {"primary": {"alias": "fixture-primary",
                                                               "input_tokens": 4096}}}),
                             encoding="utf-8")
        first = self.run_cli("--image", "sha256:1", "--prefs", "abc")
        self.assertEqual(first.returncode, 0, first.stderr)
        rows = pm.read_history(self.out)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["tokens"], pm.estimate_tokens(prompt))
        self.assertEqual(rows[0]["harnessContract"], pm.estimate_tokens(
            f"<!-- From: {pm.HARNESS_AGENTS_PATH} -->\ncontract text here\n"))
        self.assertEqual(rows[0]["image"], "sha256:1")
        self.assertEqual(rows[0]["options"], "abc")
        self.assertEqual(self.run_cli().returncode, 0)
        self.assertEqual(len(pm.read_history(self.out)), 2, "a launch appends, it never replaces")





if __name__ == "__main__":
    unittest.main()
