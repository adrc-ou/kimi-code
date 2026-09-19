#!/usr/bin/env python3
"""Reading Kimi's own literals out of its bundle, and what happens when that cannot be done.

Two halves. The extraction half runs against a synthetic mini-bundle so the suite stays cheap and
keeps passing on a machine with no Kimi image; one class reads the real bundle when it is present
and skips when it is not, because anchor patterns that match a fixture can still miss a build.
The wiring half proves the promise the operator documentation makes: a ``${kimi.*}`` name becomes
the upstream text before staging, and a name that cannot be resolved stops the launch instead of
reaching the model as thirteen literal characters.
"""

from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
# The repository root as well as tools/: unittest only puts the start directory on the path, not
# its parent, so `tests.helpers` needs the root added explicitly to resolve under every way of
# running the suite - `discover -s tests`, `python -m unittest tests.test_x`, and a bare
# `python -m unittest test_x` from inside tests/.
for _directory in (ROOT, ROOT / "tools"):
    if str(_directory) not in sys.path:
        sys.path.insert(0, str(_directory))

import kimi_prompts as kp  # noqa: E402
import prompt_context as pc  # noqa: E402

from tests.helpers import shipped_plan  # noqa: E402

PLAN = shipped_plan()
ALL_OFF = dict.fromkeys(pc.OPTION_IDS, False)

#: The bundle this suite pretends is Kimi: one statement per literal, three quote styles, and a
#: backtick literal that interpolates a sibling.
MINI_BUNDLE = """var unused = 1;
system_default = "You are ${product_name}, working in ${cwd}.\\nRules:\\n- keep going";
CODER_ROLE = `Subagent note. ${TASK_AGENT_ROLE_PREFIX} and the handoff matters.`;
explore_overlay_default = 'Read-only exploration for the explore lane.';
TASK_AGENT_ROLE_PREFIX = "You are now running as a subagent.";
"""

REAL_BUNDLE = Path("/usr/local/bin/kimi")


class IdentifierSpecTests(unittest.TestCase):
    """The spec and the promise must name the same four things."""

    def test_every_promised_placeholder_has_identifiers_to_look_for(self):
        self.assertEqual(
            tuple(sorted(kp._IDENTIFIERS)), tuple(sorted(pc.kimi_literal_names()))
        )

    def test_every_name_is_sought_by_something(self):
        for name in pc.kimi_literal_names():
            self.assertTrue(kp._IDENTIFIERS[name], f"{name} has no candidate identifier")

    def test_the_reverse_map_covers_every_candidate(self):
        expected = {one for many in kp._IDENTIFIERS.values() for one in many}
        self.assertEqual(set(kp._BY_IDENTIFIER), expected)


class AnchorTests(unittest.TestCase):
    """Where a literal is allowed to be found, and where it must not be."""

    def test_an_assignment_at_a_statement_boundary_matches(self):
        pattern = kp.anchor("CODER_ROLE")
        for source in (b'\tCODER_ROLE = "x"', b';CODER_ROLE="x"', b"{CODER_ROLE = `x`"):
            with self.subTest(source=source):
                self.assertIsNotNone(pattern.search(source))

    def test_a_longer_identifier_is_not_mistaken_for_the_one_asked_for(self):
        self.assertIsNone(kp.anchor("CODER_ROLE").search(b'\tMY_CODER_ROLE = "x"'))

    def test_a_comparison_is_not_read_as_a_definition(self):
        self.assertIsNone(kp.anchor("CODER_ROLE").search(b'\tif (CODER_ROLE == "x") {'))

    def test_spacing_between_the_name_and_the_equals_is_forgiven(self):
        data = b'\nsystem_default   = "a"'
        match = kp.anchor("system_default").search(data)
        self.assertIsNotNone(match)
        self.assertEqual(kp.read_literal(data, match.end()), (b'"a"'[1:-1], len(data)))


class LiteralReadingTests(unittest.TestCase):
    """Quote handling, escapes, and the two ways a read can fail."""

    def test_all_three_quote_styles_are_read(self):
        for quote in ('"', "'", "`"):
            with self.subTest(quote=quote):
                data = f"{quote}body{quote}".encode()
                self.assertEqual(kp.read_literal(data, 0), (b"body", len(data)))

    def test_the_delimiters_are_stripped_and_the_escapes_are_left_alone(self):
        data = rb'"say \"hi\" now"'
        raw, end = kp.read_literal(data, 0)
        self.assertEqual(raw, data[1:-1])
        self.assertEqual(end, len(data))

    def test_a_non_quote_after_the_equals_is_no_literal_at_all(self):
        self.assertIsNone(kp.read_literal(b"42", 0))

    def test_an_unterminated_literal_is_no_literal(self):
        self.assertIsNone(kp.read_literal(b'"running off the end', 0))

    def test_both_ways_of_writing_a_character_are_understood(self):
        # A numeric escape and a real UTF-8 em-dash mean the same thing to JS and must here too;
        # the second is the one a byte-wise unicode_escape decode corrupts.
        self.assertEqual(kp.decode_literal(rb"a\nb\u2014c"), "a\nb\u2014c")
        self.assertEqual(kp.decode_literal("a\nb\u2014c".encode()), "a\nb\u2014c")

    def test_a_backslash_is_dropped_only_from_a_real_escape(self):
        self.assertEqual(kp.decode_literal(rb"\d\*x"), "d*x")
        self.assertEqual(kp.decode_literal(rb"\t\x41\x41"), "\tAA")

    def test_an_empty_literal_decodes_to_an_empty_string(self):
        self.assertEqual(kp.decode_literal(b""), "")


class ExtractionTests(unittest.TestCase):
    """The four names, from candidates, with siblings interpolated."""

    def setUp(self):
        self.data = MINI_BUNDLE.encode()

    def test_every_promised_literal_comes_out_of_the_bundle(self):
        self.assertEqual(set(kp.extract(self.data)), set(pc.kimi_literal_names()))

    def test_a_double_quoted_literal_arrives_with_its_escapes_resolved(self):
        text = kp.extract(self.data)["kimi.system_default"]
        self.assertIn("\nRules:\n- keep going", text)

    def test_a_single_quoted_literal_is_read_the_same_way(self):
        self.assertEqual(
            kp.extract(self.data)["kimi.explore_overlay"],
            "Read-only exploration for the explore lane.",
        )

    def test_a_sibling_interpolated_by_a_template_literal_is_expanded(self):
        coder = kp.extract(self.data)["kimi.coder_role"]
        self.assertIn("You are now running as a subagent.", coder)
        self.assertNotIn("${TASK_AGENT_ROLE_PREFIX}", coder)

    def test_kimi_s_own_runtime_placeholders_are_left_for_kimi(self):
        text = kp.extract(self.data)["kimi.system_default"]
        self.assertIn("${cwd}", text)
        self.assertIn("${product_name}", text)

    def test_a_missing_literal_is_a_hard_failure_naming_what_was_tried(self):
        with self.assertRaises(KeyError) as caught:
            kp.extract(b'\nsystem_default = "x";')
        self.assertIn("kimi.coder_role", str(caught.exception))
        self.assertIn("CODER_ROLE", str(caught.exception))

    def test_a_name_with_no_spec_cannot_be_silently_skipped(self):
        with mock.patch.dict(kp._IDENTIFIERS, {}, clear=True):
            with self.assertRaises(KeyError) as caught:
                kp.extract(self.data)
        self.assertIn("no bundle identifier", str(caught.exception))


class CandidateOrderTests(unittest.TestCase):
    """An alternative spelling is a fallback, never a preference."""

    def test_the_alternative_spelling_is_found_when_the_primary_one_is_absent(self):
        data = b"\nexplore_overlay = 'renamed';"
        self.assertEqual(kp.find(data, "kimi.explore_overlay"), "renamed")

    def test_the_primary_spelling_wins_when_a_build_carries_both(self):
        data = b"\nexplore_overlay_default = 'primary';\nexplore_overlay = 'alias';"
        self.assertEqual(kp.find(data, "kimi.explore_overlay"), "primary")

    def test_a_name_that_is_only_a_prefix_of_another_is_not_read(self):
        self.assertIsNone(kp.find(b"\nexplore_overlay_defaultx = 'nope';", "kimi.explore_overlay"))

    def test_a_placeholder_this_module_was_never_told_about_finds_nothing(self):
        self.assertIsNone(kp.find(MINI_BUNDLE.encode(), "kimi.nope"))


class InterpolationTests(unittest.TestCase):
    """One pass, and only over names we actually own."""

    def test_an_unrelated_brace_expression_survives(self):
        literals = {"kimi.coder_role": "see ${someObject.method()} and ${cwd}"}
        self.assertEqual(kp.interpolate(literals), literals)

    def test_a_reference_to_a_literal_that_was_not_extracted_stays_put(self):
        literals = {"kimi.coder_role": "${TASK_AGENT_ROLE_PREFIX}"}
        with mock.patch.dict(kp._BY_IDENTIFIER, {}, clear=True):
            self.assertEqual(kp.interpolate(literals), literals)


class CacheTests(unittest.TestCase):
    """The document, its round trip, and the modes it is written with."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.runtime = Path(self._dir.name)
        self.literals = kp.extract(MINI_BUNDLE.encode())

    def test_a_cold_cache_reads_as_empty_rather_than_raising(self):
        self.assertEqual(kp.load(self.runtime), {})
        self.assertIsNone(kp.read_document(self.runtime))

    def test_an_unreadable_or_wrongly_shaped_cache_reads_as_empty(self):
        path = kp.cache_path(self.runtime)
        path.parent.mkdir(parents=True, exist_ok=True)
        for body in ("not json", "[]", '{"literals": "no"}'):
            with self.subTest(body=body):
                path.write_text(body, encoding="utf-8")
                self.assertEqual(kp.load(self.runtime), {})

    def test_the_round_trip_keeps_every_literal_byte(self):
        kp.store(self.runtime, self.literals, "sha256:abc")
        self.assertEqual(kp.load(self.runtime), self.literals)

    def test_the_cache_is_owner_only(self):
        path = kp.store(self.runtime, self.literals, "sha256:abc")
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_the_cache_lands_under_the_named_directory(self):
        path = kp.store(self.runtime, self.literals, "x")
        self.assertEqual(
            path.relative_to(self.runtime), Path(kp.CACHE_DIRECTORY) / kp.CACHE_FILE
        )

    def test_the_document_names_its_image_and_fingerprints_each_literal(self):
        document = kp.document({"kimi.coder_role": "role"}, "sha256:abc")
        self.assertEqual(document["image"], "sha256:abc")
        self.assertEqual(document["schema_version"], 1)
        counts = document["counts"]["kimi.coder_role"]
        self.assertEqual((counts["characters"], counts["bytes"]), (4, 4))
        self.assertEqual(len(counts["sha256"]), 64)
        self.assertGreater(counts["tokensEstimated"], 0)

    def test_a_refresh_says_which_text_upstream_changed(self):
        kp.store(self.runtime, self.literals, "one")
        updated = dict(self.literals, **{"kimi.coder_role": "different upstream wording"})
        noise = io.StringIO()
        with redirect_stderr(noise):
            kp.store(self.runtime, updated, "two")
        self.assertIn("kimi.coder_role", noise.getvalue())
        self.assertNotIn("kimi.explore_overlay", noise.getvalue())

    def test_an_unchanged_refresh_stays_silent(self):
        kp.store(self.runtime, self.literals, "one")
        noise = io.StringIO()
        with redirect_stderr(noise):
            kp.store(self.runtime, self.literals, "one")
        self.assertEqual(noise.getvalue(), "")

    def test_substitutions_add_the_harness_values_to_the_cached_literals(self):
        # One reading of harness_values(), not two: the date is today's, and a test that called
        # it on either side of a compaction or a midnight rollover would compare two dates.
        today = pc.harness_values()
        self.assertEqual(kp.substitutions(self.runtime), today)
        kp.store(self.runtime, self.literals, "one")
        mapping = kp.substitutions(self.runtime)
        self.assertEqual(set(mapping), {"harness.date", *pc.kimi_literal_names()})
        self.assertEqual(mapping["harness.date"], today["harness.date"])


class CommandTests(unittest.TestCase):
    """Exit codes, because an operator reads those more carefully than the prose."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.runtime = Path(self._dir.name)
        self.bundle = self.runtime / "mini-kimi"
        self.bundle.write_bytes(MINI_BUNDLE.encode())

    def run_cli(self, *argv: str) -> tuple[int, str, str]:
        out, noise = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(noise):
            code = kp.main(list(argv))
        return code, out.getvalue(), noise.getvalue()

    def test_print_produces_a_document_the_reader_accepts(self):
        code, out, _ = self.run_cli("--bundle", str(self.bundle), "--image", "x", "--print")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["image"], "x")
        elsewhere = self.runtime / "elsewhere"
        elsewhere.mkdir()
        kp.cache_path(elsewhere).parent.mkdir()
        kp.cache_path(elsewhere).write_text(out, encoding="utf-8")
        self.assertEqual(len(kp.load(elsewhere)), 4)

    def test_print_does_not_need_a_cache_directory(self):
        self.assertEqual(self.run_cli("--bundle", str(self.bundle), "--print")[0], 0)

    def test_extracting_without_saying_where_fails_usage(self):
        code, _, noise = self.run_cli("--bundle", str(self.bundle))
        self.assertEqual(code, 2)
        self.assertIn("--runtime-dir", noise)

    def test_a_bundle_that_is_not_there_is_reported_not_raised(self):
        code, _, noise = self.run_cli("--bundle", str(self.runtime / "absent"), "--print")
        self.assertEqual(code, 1)
        self.assertIn("absent", noise)

    def test_a_bundle_without_the_literals_says_which_one_is_missing(self):
        thin = self.runtime / "thin"
        thin.write_text('system_default = "only one"', encoding="utf-8")
        code, _, noise = self.run_cli("--bundle", str(thin), "--print")
        self.assertEqual(code, 1)
        self.assertIn("kimi.", noise)
        self.assertNotIn("KeyError", noise)

    def test_show_without_a_runtime_directory_fails_usage(self):
        self.assertEqual(self.run_cli("--show")[0], 2)

    def test_show_on_a_cold_cache_says_how_to_warm_it(self):
        code, _, noise = self.run_cli("--runtime-dir", str(self.runtime), "--show")
        self.assertEqual(code, 1)
        self.assertIn("--extract", noise)

    def test_show_reports_the_image_and_a_size_per_literal(self):
        kp.store(self.runtime, kp.extract(MINI_BUNDLE.encode()), "sha256:abc")
        code, out, _ = self.run_cli("--runtime-dir", str(self.runtime), "--show")
        self.assertEqual(code, 0)
        self.assertIn("sha256:abc", out)
        self.assertIn("tokens (estimated)", out)
        for name in pc.kimi_literal_names():
            self.assertIn(name, out)


class StagingIntegrationTests(unittest.TestCase):
    """The wiring, which is the part an operator's file actually depends on.

    These live here rather than in ``test_prompt_context.py`` because they are about what a
    ``${kimi.*}`` name does to staging, and that is a promise this module makes.
    """

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.root = Path(self._dir.name)
        kp.store(self.root / "runtime", kp.extract(MINI_BUNDLE.encode()), "mini")
        self.values = kp.substitutions(self.root / "runtime")

    def write(self, name: str, text: str) -> None:
        (self.root / name).write_text(text, encoding="utf-8")

    def test_a_quoted_literal_becomes_the_upstream_text_before_staging(self):
        self.write(pc.SYSTEM_FILE, "Mine first.\n${kimi.task_agent_prefix}\n")
        staged = pc.compose_system_document(self.root, PLAN, ALL_OFF, self.values)
        self.assertEqual(staged, "Mine first.\nYou are now running as a subagent.\n")

    def test_a_name_that_cannot_be_resolved_stops_the_launch(self):
        self.write(pc.SYSTEM_FILE, "Mine.\n${kimi.coder_role}\n")
        with self.assertRaises(SystemExit) as caught:
            pc.compose_system_document(self.root, PLAN, ALL_OFF, None)
        message = str(caught.exception)
        self.assertIn("kimi.coder_role", message)
        self.assertIn("--extract", message)

    def test_the_check_runs_first_so_a_literal_s_own_placeholders_survive(self):
        # ${cwd} arrives from inside the extracted literal rather than from the operator, and must
        # not be mistaken for something the harness failed to deliver.
        self.write(pc.SYSTEM_FILE, "${kimi.system_default}")
        staged = pc.compose_system_document(self.root, PLAN, ALL_OFF, self.values)
        self.assertIn("${cwd}", staged)
        self.assertIn("${product_name}", staged)
        self.assertNotIn("${kimi.system_default}", staged)

    def test_an_all_lane_file_resolves_its_literals_too(self):
        self.write(pc.CONTEXT_FILE, "Contract.\n${kimi.explore_overlay}\n")
        staged = pc.compose_agents_document(self.root, PLAN, "", ALL_OFF, self.values)
        self.assertEqual(staged, "Contract.\nRead-only exploration for the explore lane.\n")

    def test_module_guidance_is_staged_verbatim_and_needs_no_mapping(self):
        self.write(pc.CONTEXT_FILE, "Contract.")
        staged = pc.compose_agents_document(
            self.root, PLAN, "Module says: run the pipeline.",
            dict(ALL_OFF, **{pc.OPTION_MODULE_GUIDANCE: True}),
        )
        self.assertIn("Module says: run the pipeline.", staged)

    def test_a_file_without_literals_is_unaffected_by_the_mapping(self):
        self.write(pc.SYSTEM_FILE, "Plain words only.")
        composed = pc.compose_system_document(self.root, PLAN, ALL_OFF, None)
        self.assertEqual(composed, "Plain words only.\n")

    def test_the_harness_date_and_a_literal_resolve_in_the_same_pass(self):
        self.write(pc.CONTEXT_FILE, "Today is ${harness.date}. ${kimi.task_agent_prefix}")
        staged = pc.compose_agents_document(
            self.root, PLAN, "", ALL_OFF, self.values
        )
        self.assertIn(f"Today is {pc.harness_values()['harness.date']}.", staged)
        self.assertIn("subagent", staged)


@unittest.skipUnless(REAL_BUNDLE.is_file(), "no Kimi bundle on this machine")
class RealBundleTests(unittest.TestCase):
    """The one class that reads the shipped build, and the reason it must not be fixture-only.

    Every pattern in this module was written against offsets that have since moved once; a
    fixture proves the reader is coherent, and only the real file proves it is still aimed at the
    right thing.
    """

    #: Cached on the class: extraction scans all 182 MB once per identifier, so running it per
    #: test method made this one class about three quarters of the whole suite's runtime.
    _literals: dict[str, str] | None = None

    @classmethod
    def literals(cls) -> dict[str, str]:
        import mmap

        if cls._literals is None:
            with REAL_BUNDLE.open("rb") as handle:
                with mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ) as data:
                    cls._literals = kp.extract(data)
        return cls._literals

    def test_the_anchors_find_every_literal_in_the_shipped_build(self):
        self.assertEqual(set(self.literals()), set(pc.kimi_literal_names()))

    def test_the_built_in_prompt_arrives_as_a_template_not_a_finished_string(self):
        text = self.literals()["kimi.system_default"]
        self.assertIn("${agents_md}", text)
        self.assertIn("${skills_section}", text)
        self.assertIn("system-reminder", text)

    def test_no_sibling_reference_is_left_dangling(self):
        for name, text in self.literals().items():
            self.assertNotIn("${TASK_AGENT_ROLE_PREFIX}", text, f"{name} still interpolates")

    def test_the_built_in_prompt_is_worth_quoting_a_piece_of(self):
        literals = self.literals()
        self.assertGreater(len(literals["kimi.system_default"]), 4000)
        # The role block embeds the prefix, so the expansion must have made it larger.
        self.assertGreater(
            len(literals["kimi.coder_role"]), len(literals["kimi.task_agent_prefix"])
        )


if __name__ == "__main__":
    raise SystemExit(unittest.main())
