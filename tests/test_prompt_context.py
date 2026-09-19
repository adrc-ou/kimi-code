#!/usr/bin/env python3
"""The composer is pure string assembly, so its whole matrix belongs in one cheap file.

Three things are asserted here that nothing else in the suite can see: the four-row
existence/emptiness table for the system document, the order in which the all-lane document is
put together, and the rules the panel will rely on later (prefs, toggles, placeholder checking).
Every figure comes from the shipped definitions rather than an invented plan, because a synthetic
plan would let a real one fail to render.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# The repository root as well as tools/: unittest only puts the start directory on the path, not
# its parent, so `tests.helpers` needs the root added explicitly to resolve under every way of
# running the suite - `discover -s tests`, `python -m unittest tests.test_x`, and a bare
# `python -m unittest test_x` from inside tests/.
for _directory in (ROOT, ROOT / "tools"):
    if str(_directory) not in sys.path:
        sys.path.insert(0, str(_directory))

import kimi_prompts as kp  # noqa: E402
import policy  # noqa: E402
import prompt_context as pc  # noqa: E402

from tests.helpers import shipped_plan  # noqa: E402

#: Text an operator file may hold that no generated block could ever produce.
MINE = "Operator voice goes first."
ALL_OFF = dict.fromkeys(pc.OPTION_IDS, False)
THE_DATE = date(2026, 9, 19)


class DocumentSourceTests(unittest.TestCase):
    """Two tiers each, and the ``.example`` files in neither of them."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.root = Path(self._dir.name)

    def touch(self, relative: str, text: str) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def test_a_system_prompt_comes_from_the_operator_file_or_from_kimi(self):
        self.assertIsNone(pc.system_source(self.root))
        path = self.touch(pc.SYSTEM_FILE, MINE)
        self.assertEqual(pc.system_source(self.root), path)

    def test_the_system_example_is_documentation_and_is_never_loaded(self):
        self.touch(pc.SYSTEM_EXAMPLE, "help text")
        self.assertIsNone(pc.system_source(self.root))
        self.touch(pc.SYSTEM_FILE, MINE)
        self.assertEqual(pc.system_source(self.root).name, pc.SYSTEM_FILE)

    def test_the_contract_prefers_the_operator_file_then_ships_its_own(self):
        self.assertEqual(pc.context_source(self.root), None)
        shipped = self.touch("runtime/AGENTS.md", "shipped contract")
        self.assertEqual(pc.context_source(self.root), shipped)
        operator = self.touch(pc.CONTEXT_FILE, MINE)
        self.assertEqual(pc.context_source(self.root), operator)

    def test_the_context_example_is_documentation_and_is_never_loaded(self):
        self.touch(pc.CONTEXT_EXAMPLE, "help text")
        self.assertIsNone(pc.context_source(self.root))

    def test_the_repository_ships_both_examples_and_a_contract(self):
        # These three files are the documentation tier; a missing one silently removes the
        # only place the convention is explained.
        paths = [Path(pc.SYSTEM_EXAMPLE), Path(pc.CONTEXT_EXAMPLE), Path(*pc.CONTRACT_SOURCE)]
        for relative in paths:
            with self.subTest(relative=str(relative)):
                self.assertTrue((ROOT / relative).is_file(), f"{relative} is missing")


class SystemDocumentTests(unittest.TestCase):
    """The resolution table in docs/prompts.md, cell by cell: existence decides
    authority, emptiness decides payload."""

    @classmethod
    def setUpClass(cls):
        cls.plan = shipped_plan()
        cls.additions = policy.render_guidance(cls.plan, "main").rstrip("\n")

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.root = Path(self._dir.name)

    def staged(self, text: str | None, enabled: dict[str, bool] | None = None) -> str:
        if text is not None:
            (self.root / pc.SYSTEM_FILE).write_text(text, encoding="utf-8")
        return pc.compose_system_document(self.root, self.plan, enabled)

    def test_row_one_absent_file_with_addons_wraps_the_base_prompt_placeholder(self):
        out = self.staged(None)
        self.assertTrue(out.startswith(pc.BASE_PROMPT_WRAPPER))
        self.assertIn(self.additions, out)
        # The wrapper is the bare placeholder and never the expanded built-in text, which is
        # what lets it cost one name rather than a whole prompt. Exactly one placeholder, and a
        # name Kimi binds itself.
        self.assertEqual(pc.PLACEHOLDER_PATTERN.findall(pc.BASE_PROMPT_WRAPPER), ["base_prompt"])
        self.assertIn("base_prompt", pc.known_placeholders())
        self.assertNotIn("You are Kimi", out)

    def test_row_one_absent_file_without_addons_stages_nothing_at_all(self):
        self.assertEqual(self.staged(None, ALL_OFF), "")

    def test_row_two_nonempty_file_keeps_its_own_voice_and_adds_nothing_of_its_own(self):
        out = self.staged(f"{MINE}\n")
        self.assertTrue(out.startswith(MINE))
        self.assertIn(self.additions, out)
        self.assertNotIn(pc.BASE_PROMPT_WRAPPER, out)

    def test_row_two_nonempty_file_with_addons_off_is_the_file_alone(self):
        self.assertEqual(self.staged(f"{MINE}\n", ALL_OFF), f"{MINE}\n")

    def test_row_three_empty_file_with_addons_is_the_additions_alone(self):
        for blank in ("", "\n", "   \n\t\n"):
            with self.subTest(blank=repr(blank)):
                out = self.staged(blank)
                self.assertEqual(out, f"{self.additions}\n")
                self.assertNotIn(pc.BASE_PROMPT_WRAPPER, out)

    def test_row_four_empty_file_with_addons_off_is_the_sentinel(self):
        for blank in ("", "  \n"):
            with self.subTest(blank=repr(blank)):
                self.assertEqual(self.staged(blank, ALL_OFF), pc.EMPTY_PROMPT_SENTINEL + "\n")

    def test_a_single_enabled_addition_is_that_addition_alone_behind_an_empty_file(self):
        enabled = dict(ALL_OFF)
        enabled[policy.OPTION_LANE_TABLE] = True
        table = policy.guidance_block(policy.guidance_sections(self.plan, "main")[0])
        self.assertEqual(self.staged("", enabled), f"{table}\n")

    def test_the_composed_additions_are_byte_identical_to_render_guidance(self):
        # The panel measures a block on its own; the composer ships the same bytes. Any drift
        # here makes every number in the diagram a lie.
        out = self.staged(None)
        joined = "\n\n".join([pc.BASE_PROMPT_WRAPPER, self.additions]) + "\n"
        self.assertEqual(out, joined)

    def test_html_comments_never_reach_the_model(self):
        out = self.staged(f"{MINE}\n\n<!-- why: this is help text for the operator\n-->\n")
        self.assertIn(MINE, out)
        self.assertNotIn("help text", out)
        self.assertNotIn("<!--", out)

    def test_the_harness_date_is_resolved_before_staging(self):
        (self.root / pc.SYSTEM_FILE).write_text("Staged on ${harness.date}.\n", encoding="utf-8")
        out = pc.compose_system_document(self.root, self.plan, ALL_OFF)
        self.assertNotIn("harness.date", out)
        self.assertIn(date.today().isoformat(), out)
        self.assertEqual(pc.harness_values(THE_DATE)["harness.date"], "2026-09-19")


class AgentsDocumentTests(unittest.TestCase):
    """The all-lane document: contract, then lane numbers, then module guidance, then newline."""

    @classmethod
    def setUpClass(cls):
        cls.plan = shipped_plan()
        cls.lane = policy.render_guidance(cls.plan, "lane").rstrip("\n")

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.root = Path(self._dir.name)
        (self.root / "runtime").mkdir()
        (self.root / "runtime" / "AGENTS.md").write_text("shipped contract\n", encoding="utf-8")

    def staged(self, module_guidance: str = "", enabled: dict[str, bool] | None = None) -> str:
        return pc.compose_agents_document(self.root, self.plan, module_guidance, enabled)

    def test_it_starts_from_the_shipped_contract(self):
        out = self.staged()
        self.assertTrue(out.startswith("shipped contract"))
        self.assertIn(self.lane, out)

    def test_an_operator_contract_replaces_the_shipped_one_rather_than_joining_it(self):
        (self.root / pc.CONTEXT_FILE).write_text(f"{MINE}\n", encoding="utf-8")
        out = self.staged()
        self.assertTrue(out.startswith(MINE))
        self.assertNotIn("shipped contract", out)

    def test_module_guidance_follows_the_lane_numbers(self):
        out = self.staged("Module: use make.\n")
        self.assertLess(out.index("shipped contract"), out.index(self.lane))
        self.assertLess(out.index(self.lane), out.index("Module: use make."))

    def test_module_guidance_off_costs_nothing(self):
        enabled = dict(pc.DEFAULT_ENABLED)
        enabled["module_guidance"] = False
        self.assertNotIn("use make", self.staged("Module: use make.\n", enabled))

    def test_blank_module_guidance_adds_no_separators(self):
        self.assertEqual(self.staged("\n \n"), self.staged())

    def test_no_main_only_text_leaks_into_the_all_lane_document(self):
        out = self.staged()
        for section in policy.guidance_sections(self.plan, "main"):
            self.assertNotIn(policy.guidance_block(section), out)

    def test_no_lane_text_leaks_into_the_main_document(self):
        out = pc.compose_system_document(self.root, self.plan, None)
        self.assertNotIn(self.lane, out)

    def test_everything_off_still_leaves_the_contract(self):
        # The bind source must exist whatever the operator unchecks; Docker would otherwise
        # create a directory at that path and the container would fail at start.
        out = self.staged("Module: use make.\n", ALL_OFF)
        self.assertEqual(out, "shipped contract\n")

    def test_a_completely_empty_document_is_still_returned_for_writing(self):
        (self.root / Path(*pc.CONTRACT_SOURCE)).unlink()
        self.assertEqual(self.staged("", ALL_OFF), "")


class CommentTests(unittest.TestCase):
    def test_comments_are_removed_including_multilingual_and_trailing_newline(self):
        self.assertEqual(pc.strip_html_comments("a<!--x-->b"), "ab")
        self.assertEqual(pc.strip_html_comments("a<!--\nx\ny\n-->b"), "ab")
        self.assertEqual(pc.strip_html_comments("one\n<!-- two -->\nthree\n"), "one\nthree\n")
        self.assertEqual(pc.strip_html_comments("keep <!-- unclosed"), "keep <!-- unclosed")

    def test_the_shipped_examples_are_help_text_over_one_placeholder(self):
        # Each example documents the convention and then demonstrates it, so what survives
        # comment-stripping must be a working minimal file and nothing more.
        for name in (pc.SYSTEM_EXAMPLE, pc.CONTEXT_EXAMPLE):
            with self.subTest(name=name):
                raw = (ROOT / name).read_text(encoding="utf-8")
                self.assertIn("<!--", raw, f"{name} documents nothing")
                body = pc.strip_html_comments(raw)
                self.assertNotIn("<!--", body)
                expected = 1 if name == pc.SYSTEM_EXAMPLE else 0
                self.assertEqual(body.count(pc.BASE_PROMPT_WRAPPER), expected)
                self.assertTrue(body.strip(), f"{name} has no body once commented out")
                pc.check_placeholders(body, name)


class PlaceholderTests(unittest.TestCase):
    def test_a_near_miss_is_fatal_because_kimi_would_silently_pass_it_through(self):
        with self.assertRaises(SystemExit) as caught:
            pc.check_placeholders("<!-- a -->\n${base_prampt}\n")
        self.assertIn("base_prompt", str(caught.exception))

    def test_every_known_name_is_accepted(self):
        pc.check_placeholders("\n".join(f"${{{name}}}" for name in pc.known_placeholders()))

    def test_an_unrelated_name_is_prose_not_a_typo(self):
        for text in ("${HOME}", "run ${FOO:?set first}", "${KIMI_MODEL_LABEL}"):
            with self.subTest(text=text):
                pc.check_placeholders(text)

    def test_a_documented_but_undefined_name_survives_as_the_text_it_is(self):
        """The panel advertises these as literal text, so the near-miss gate must spare them.

        ``now`` is two edits from ``os``, which is how this failed before: the gate promised a
        pass-through and delivered an aborted launch.
        """
        for name in pc.DOCUMENTED_BUT_UNDEFINED:
            with self.subTest(name=name):
                self.assertNotIn(name, pc.known_placeholders())
                pc.check_placeholders(f"the timestamp is ${{{name}}}")

    def test_code_spans_hold_examples_not_intentions(self):
        pc.check_placeholders("```bash\nexport X=${base_prampt}\n```\n")
        pc.check_placeholders("Type `${base_prampt}` to see the literal text.\n")
        pc.check_placeholders("~~~\n${base_prampt}\n~~~\n")
        # The double-backtick idiom exists precisely so a span can contain a backtick, and this
        # repository's own guidance uses it for placeholder names.
        pc.check_placeholders("Write ``${base_prampt}`` to wrap it.\n")

    def test_a_name_outside_the_code_span_is_still_fatal(self):
        """Blanking a span must not blank the sentence around it."""
        with self.assertRaises(SystemExit):
            pc.check_placeholders("use ``${cwd}`` and then ${base_prampt}\n")


    def test_a_second_wrapper_is_duplicate_whatever_it_is_wrapped_in(self):
        """Substitution is unconditional, so this check is not code-span exempt like the other."""
        for text in (
            "${base_prompt}\n${base_prompt}\n",
            "${base_prompt}\nas in ``${base_prompt}``\n",
            "```\n${base_prompt}\n```\nand ${base_prompt}\n",
        ):
            with self.subTest(text=text):
                with self.assertRaises(SystemExit):
                    pc.check_placeholders(text)

    def test_substitution_touches_only_what_the_harness_owns(self):
        values = pc.harness_values(THE_DATE)
        out = pc.substitute_harness_placeholders("${harness.date} ${cwd} ${base_prompt}", values)
        self.assertEqual(out, "2026-09-19 ${cwd} ${base_prompt}")

    def test_the_variable_list_is_the_one_kimi_renders(self):
        # A count would notice a rename but not a duplicate or a stray spelling, and the list is
        # only useful if every name in it is one Kimi actually binds. The conditions table is the
        # second copy of the same fact, so pinning agreement between the two is what catches drift.
        self.assertEqual(len(set(pc.KIMI_PLACEHOLDERS)), len(pc.KIMI_PLACEHOLDERS))
        for name in pc.KIMI_PLACEHOLDERS:
            self.assertRegex(name, r"^[a-z_][a-z0-9_]*$")
        self.assertEqual(set(kp.PLACEHOLDER_CONDITIONS), set(pc.KIMI_PLACEHOLDERS))
        self.assertNotIn("base_prompt", pc.KIMI_PLACEHOLDERS)
        self.assertIn("base_prompt", pc.known_placeholders())
        self.assertIn("agents_md", pc.KIMI_PLACEHOLDERS)


class PrefsTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.path = Path(self._dir.name) / pc.PREFS_FILE

    def test_choices_survive_a_round_trip_and_remember_who_made_them(self):
        wanted = dict(ALL_OFF)
        wanted["lane_table"] = True
        pc.save_prefs(self.path, wanted)
        self.assertEqual(pc.load_prefs(self.path), wanted)

    def test_the_prefs_file_is_private_to_the_operator(self):
        pc.save_prefs(self.path, pc.DEFAULT_ENABLED)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_a_file_that_has_never_been_written_is_all_defaults(self):
        self.assertEqual(pc.load_prefs(self.path), dict(pc.DEFAULT_ENABLED))
        self.assertTrue(all(pc.DEFAULT_ENABLED.values()))

    def test_an_unreadable_or_unshaped_file_falls_back_rather_than_failing_a_launch(self):
        for junk in ("not json", "[]", "null", ""):
            with self.subTest(junk=junk):
                self.path.write_text(junk, encoding="utf-8")
                self.assertEqual(pc.load_prefs(self.path), dict(pc.DEFAULT_ENABLED))

    def test_a_name_this_build_does_not_ship_is_dropped_not_trusted(self):
        # An older or newer panel must not be able to silence a live mechanism by leaving a
        # stale key behind.
        self.path.write_text('{"gone_option": false}\n', encoding="utf-8")
        self.assertEqual(pc.load_prefs(self.path), dict(pc.DEFAULT_ENABLED))

    def test_resolve_enabled_never_invents_or_forgets_an_option(self):
        self.assertEqual(pc.resolve_enabled(None), dict(pc.DEFAULT_ENABLED))
        self.assertEqual(pc.resolve_enabled({}), dict(pc.DEFAULT_ENABLED))
        self.assertEqual(pc.resolve_enabled({"lane_table": 0})[policy.OPTION_LANE_TABLE], False)
        self.assertEqual(set(pc.resolve_enabled({"nope": True})), set(pc.OPTION_IDS))


class ToggleTests(unittest.TestCase):
    CONFIG = "\n".join(
        [
            "[skills]",
            "merge_all_available_skills = true",
            "extra_skill_dirs = [\"runtime/skills\"]",
            "builtin_product_skills = true",
            "extra_agent_dirs = [\"runtime/agents\"]",
            "",
        ]
    )

    def test_an_option_switches_its_key_off_rather_than_silently_missing(self):
        out = pc.apply_config_toggles(self.CONFIG, {"harness_skills": False})
        self.assertIn("extra_skill_dirs = []", out)
        self.assertIn("merge_all_available_skills = true", out)

    def test_every_config_option_actually_reaches_a_key_that_exists(self):
        rendered = (ROOT / "runtime" / "config.toml").read_text(encoding="utf-8")
        for option, (key, _) in pc.CONFIG_TOGGLES.items():
            with self.subTest(option=option, key=key):
                self.assertRegex(rendered, rf"(?m)^{key} = ")

    def test_a_key_that_has_moved_fails_closed(self):
        with self.assertRaises(SystemExit) as caught:
            pc.apply_config_toggles("[skills]\n", {"harness_skills": False})
        self.assertIn("extra_skill_dirs", str(caught.exception))

    def test_the_permission_banner_is_an_env_choice_not_a_config_one(self):
        self.assertEqual(pc.apply_env_toggles({}, pc.DEFAULT_ENABLED),
                         {"KIMI_CODE_PERMISSION_MODE_REMINDER": "true"})
        self.assertEqual(pc.apply_env_toggles({}, {"permission_banner": False}),
                         {"KIMI_CODE_PERMISSION_MODE_REMINDER": "false"})


class OptionTableTests(unittest.TestCase):
    """An option that names no real mechanism is a lie in the panel, so it does not ship."""

    def test_ids_are_unique_and_every_companion_is_real(self):
        self.assertEqual(len(set(pc.OPTION_IDS)), len(pc.OPTION_IDS))
        for option in pc.OPTIONS:
            with self.subTest(option=option.id):
                for companion in option.companions:
                    self.assertIn(companion, pc.OPTION_IDS)
                    self.assertNotEqual(companion, option.id)

    def test_every_option_labels_itself_for_a_human(self):
        for option in pc.OPTIONS:
            with self.subTest(option=option.id):
                self.assertTrue(option.label and option.summary)
                self.assertTrue(option.label[0].isupper())
                self.assertIn(option.target, (pc.TARGET_AGENTS, pc.TARGET_SYSTEM,
                                              pc.TARGET_CONFIG, pc.TARGET_ENV))

    def test_every_option_names_a_mechanism_that_this_repository_actually_runs(self):
        generated = {section.option for audience in policy.GUIDANCE_AUDIENCES
                     for section in policy.guidance_sections(shipped_plan(), audience)}
        implemented = (generated | set(pc.CONFIG_TOGGLES) | set(pc.ENV_TOGGLES)
                       | {"module_guidance"})
        self.assertEqual(set(pc.OPTION_IDS), implemented)

    def test_the_generated_blocks_are_governed_by_the_options_the_panel_draws(self):
        generated = [section.option for audience in policy.GUIDANCE_AUDIENCES
                     for section in policy.guidance_sections(shipped_plan(), audience)]
        self.assertEqual(len(generated), len(set(generated)))
        self.assertIn(policy.OPTION_LANE_LIMITS, generated)
        for option in policy.OPTION_LANE_TABLE, policy.OPTION_PARALLELISM:
            self.assertIn(option, generated)

    def test_no_option_is_an_umbrella_for_another(self):
        # A flat list means one checkbox per block; two ids must not switch the same text.
        blocks: dict[str, str] = {}
        for audience in policy.GUIDANCE_AUDIENCES:
            for section in policy.guidance_sections(shipped_plan(), audience):
                blocks[section.option] = policy.guidance_block(section)
        self.assertEqual(len(blocks), len(set(blocks.values())))

    def test_the_switched_off_case_costs_exactly_nothing(self):
        empty = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(empty, ignore_errors=True))
        plan = shipped_plan()
        self.assertEqual(pc.compose_system_document(empty, plan, ALL_OFF), "")
        self.assertEqual(pc.compose_agents_document(empty, plan, "", ALL_OFF), "")


class SourceRecordTests(unittest.TestCase):
    """What was staged, and whether the file on disk has moved since.

    The digest is of the comment-stripped text rather than of the file, and that single choice is
    what makes the resulting notice worth reading: help text never ships, so an edit to it must not
    claim that a restart is needed. A missing record says nothing at all, because inventing a
    warning for a stack staged by an older harness would teach the operator to ignore the real ones.
    """

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.root = Path(self._dir.name) / "ws"
        self.root.mkdir()
        self.runtime = self.root / ".local/runtime/i"
        self.runtime.mkdir(parents=True)

    def stage(self):
        """Write the sidecar exactly as the renderer does, from whatever is on disk now."""
        (self.runtime / pc.SOURCES_FILE).write_text(
            json.dumps(pc.document_sources(self.root)), encoding="utf-8"
        )

    def contract(self):
        """Stand in for the harness's own default contract, which a temp checkout does not have."""
        default = self.root / "runtime"
        default.mkdir()
        (default / "AGENTS.md").write_text("# operating contract\n", encoding="utf-8")

    def stale(self):
        return pc.stale_sources(self.root, self.runtime)

    def test_an_untouched_file_is_not_stale(self):
        (self.root / pc.SYSTEM_FILE).write_text("the voice\n", encoding="utf-8")
        self.stage()
        self.assertEqual(self.stale(), [])

    def test_editing_help_text_needs_no_restart(self):
        path = self.root / pc.SYSTEM_FILE
        path.write_text("<!-- short note -->\nthe voice\n", encoding="utf-8")
        self.stage()
        path.write_text("<!-- a much longer note written later -->\nthe voice\n", encoding="utf-8")
        self.assertEqual(self.stale(), [])

    def test_editing_the_body_names_the_file_that_moved(self):
        path = self.root / pc.SYSTEM_FILE
        path.write_text("the voice\n", encoding="utf-8")
        self.stage()
        path.write_text("a different voice\n", encoding="utf-8")
        self.assertEqual(
            self.stale(), [f"{pc.SYSTEM_FILE} changed since it was staged - restart to apply it"]
        )

    def test_a_file_that_appeared_after_staging_is_stale_too(self):
        """The contract falls back to the harness default, so a new file really does change it."""
        self.stage()
        (self.root / pc.CONTEXT_FILE).write_text("operator contract\n", encoding="utf-8")
        self.assertEqual(len(self.stale()), 1)

    def test_a_record_from_before_this_file_existed_says_nothing(self):
        self.assertEqual(
            pc.stale_sources(self.root, self.runtime), [], "no sidecar is not a clean stack"
        )

    def test_an_unshaped_record_says_nothing_rather_than_crashing(self):
        (self.runtime / pc.SOURCES_FILE).write_text("[]", encoding="utf-8")
        self.assertEqual(self.stale(), [])

    def test_a_document_with_no_source_records_no_digest(self):
        recorded = pc.document_sources(self.root)
        self.assertEqual(recorded["system"], {"source": None, "digest": None})
        self.assertEqual(recorded["agents"], {"source": None, "digest": None})

    def test_both_tiers_are_recorded_by_name_not_by_absolute_path(self):
        """An absolute path would make the record differ per checkout for no reason at all."""
        self.contract()
        (self.root / pc.SYSTEM_FILE).write_text("the voice\n", encoding="utf-8")
        recorded = pc.document_sources(self.root)
        self.assertEqual(recorded["system"]["source"], pc.SYSTEM_FILE)
        self.assertEqual(recorded["agents"]["source"], "runtime/AGENTS.md")

    def test_the_harness_default_is_recorded_under_its_own_name(self):
        """A contract nobody overrode still has to be reportable when the harness copy changes."""
        self.contract()
        self.stage()
        (self.root / "runtime/AGENTS.md").write_text("# a new contract\n", encoding="utf-8")
        self.assertEqual(
            self.stale(), ["runtime/AGENTS.md changed since it was staged - restart to apply it"]
        )


class InstructionBillTests(unittest.TestCase):
    """Kimi's own oversized-instruction warning, in the unit Kimi uses for it."""

    def test_the_limit_is_kimis_number_not_an_invention(self):
        self.assertEqual(pc.KIMI_RECOMMENDED_MAX_INSTRUCTION_BYTES, 32 * 1024)

    def test_bytes_are_counted_as_utf8_not_as_characters(self):
        self.assertEqual(pc.instruction_bytes("é" * 100), 200)
        self.assertEqual(pc.instruction_bytes("ab", "cd"), 4)

    def test_the_limit_is_inclusive(self):
        limit = pc.KIMI_RECOMMENDED_MAX_INSTRUCTION_BYTES
        self.assertEqual(pc.over_instruction_limit(limit), "")
        self.assertTrue(pc.over_instruction_limit(limit + 1))

    def test_the_bill_says_the_text_survives(self):
        """Kimi warns and ships every byte, so anything implying truncation would be a lie."""
        limit = pc.KIMI_RECOMMENDED_MAX_INSTRUCTION_BYTES
        notice = pc.over_instruction_limit(limit + 1)
        self.assertIn("32 KB", notice)
        self.assertIn("Nothing here is truncated", notice)
        self.assertNotIn("\n", notice.strip(), "one line, so the panel can align it")


if __name__ == "__main__":
    unittest.main()
