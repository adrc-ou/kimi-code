"""The launch question about multimodal context reduction, and what its answer becomes.

The step's whole job is to turn an operator's choice into one environment variable, so these
tests follow that value all the way to the file :file:`compose.yaml` reads — a default that
drifts between the two would silently change what every unattended launch sends.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
for _directory in (ROOT, ROOT / "tools"):
    if str(_directory) not in sys.path:
        sys.path.insert(0, str(_directory))

from tests.helpers import load_script  # noqa: E402

STEP = load_script("image_eviction", Path("tools/image_eviction.py"))
FLOW = load_script("tui_flow_check", Path("tools/tui/flow.py"))


class AnswerOfTests(unittest.TestCase):
    def test_both_spellings_of_each_answer_are_accepted(self):
        for value, expected in (
            (True, STEP.ON),
            (False, STEP.OFF),
            ("on", STEP.ON),
            ("YES", STEP.ON),
            ("1", STEP.ON),
            ("off", STEP.OFF),
            ("no", STEP.OFF),
            ("0", STEP.OFF),
        ):
            with self.subTest(value=value):
                self.assertEqual(STEP.answer_of(value), expected)

    def test_nonsense_is_not_read_as_an_answer(self):
        """A corrupt record must fall through to a default, not pick an option at random."""
        for value in ("maybe", "", None, 7, {"a": 1}):
            with self.subTest(value=value):
                self.assertEqual(STEP.answer_of(value), "")


class DefaultTests(unittest.TestCase):
    def setUp(self):
        storage = tempfile.TemporaryDirectory()
        self.addCleanup(storage.cleanup)
        self.runtime = Path(storage.name)

    def test_an_operator_who_has_never_been_asked_gets_no(self):
        self.assertEqual(STEP.FIRST_DEFAULT, STEP.OFF)
        self.assertEqual(STEP.remembered(self.runtime), "")
        self.assertEqual(STEP.choose("", non_interactive=True), STEP.OFF)

    def test_the_last_answer_opens_next_time(self):
        (self.runtime / STEP.PREVIOUS_FILE).write_text(json.dumps({"eviction": STEP.ON}))

        self.assertEqual(STEP.remembered(self.runtime), STEP.ON)
        self.assertEqual(STEP.choose(STEP.remembered(self.runtime), non_interactive=True), STEP.ON)

    def test_a_recorded_malformed_answer_falls_back_to_the_first_default(self):
        (self.runtime / STEP.PREVIOUS_FILE).write_text(json.dumps({"eviction": "sideways"}))

        self.assertEqual(STEP.choose(STEP.remembered(self.runtime), non_interactive=True), STEP.OFF)

    def test_a_non_terminal_launch_never_blocks_on_the_question(self):
        """--non-interactive is not the only way to end up with no keyboard."""
        stdin, stdout = tempfile.TemporaryFile(), tempfile.TemporaryFile()
        self.addCleanup(stdin.close)
        self.addCleanup(stdout.close)
        with (
            patch.object(sys, "stdin", stdin),
            patch.object(sys, "stdout", stdout),
        ):
            self.assertEqual(STEP.choose("", non_interactive=False), STEP.OFF)


class OutputTests(unittest.TestCase):
    def setUp(self):
        storage = tempfile.TemporaryDirectory()
        self.addCleanup(storage.cleanup)
        self.runtime = Path(storage.name)

    def test_the_answer_reaches_the_variable_compose_reads(self):
        STEP.record(self.runtime, STEP.ON)
        env = (self.runtime / STEP.OUTPUT_ENV).read_text()

        self.assertEqual(env.strip(), "MODEL_PROXY_IMAGE_EVICTION=1")

    def test_the_negative_answer_writes_zero_rather_than_nothing(self):
        STEP.record(self.runtime, STEP.OFF)
        env = (self.runtime / STEP.OUTPUT_ENV).read_text()

        self.assertEqual(env.strip(), "MODEL_PROXY_IMAGE_EVICTION=0")

    def test_the_chosen_answer_is_recorded_for_the_commit_point_to_remember(self):
        STEP.record(self.runtime, STEP.ON)
        stored = json.loads((self.runtime / STEP.CHOSEN_FILE).read_text())

        self.assertEqual(stored["eviction"], STEP.ON)
        # The screen explains itself into the record, so a later reader can tell which
        # behaviour they opted into without re-deriving it from an id.
        self.assertIn("analyze_image", stored["explained"])

    def test_main_writes_both_files_in_one_pass(self):
        code = STEP.main(["--runtime-dir", str(self.runtime), "--non-interactive"])

        self.assertEqual(code, 0)
        self.assertTrue((self.runtime / STEP.OUTPUT_ENV).is_file())
        self.assertTrue((self.runtime / STEP.CHOSEN_FILE).is_file())


class GuidanceAndGatingTests(unittest.TestCase):
    """An agent must be told how this works before its first attachment, or not at all."""

    def setUp(self):
        storage = tempfile.TemporaryDirectory()
        self.addCleanup(storage.cleanup)
        self.runtime = Path(storage.name)
        assets = self.runtime / "assets"
        assets.mkdir()
        (assets / "mcp.json").write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "serena": {"command": "serena", "enabled": True},
                        "images": {"command": "python3", "enabled": True},
                    }
                },
                indent=2,
            )
        )

    def mcp(self):
        return json.loads((self.runtime / "assets" / "mcp.json").read_text())["mcpServers"]

    def test_opting_in_ships_the_explanation_and_the_tool(self):
        STEP.record(self.runtime, STEP.ON)

        guidance = (self.runtime / STEP.GUIDANCE_FILE).read_text()
        self.assertIn("analyze_image", guidance)
        self.assertIn("handle", guidance)
        self.assertNotIn("TBD", guidance)
        self.assertTrue(self.mcp()["images"]["enabled"])
        self.assertEqual(self.mcp()["serena"]["enabled"], True, "no other server is touched")

    def test_opting_out_ships_neither(self):
        STEP.record(self.runtime, STEP.OFF)

        self.assertEqual((self.runtime / STEP.GUIDANCE_FILE).read_text(), "")
        self.assertNotIn("images", self.mcp())
        self.assertIn("serena", self.mcp(), "declining must not disable an unrelated server")

    def test_the_explanation_names_the_failure_it_exists_to_prevent(self):
        """An agent that only learns the mechanism, not the consequence, still loses detail."""
        text = STEP.GUIDANCE.lower()

        self.assertIn("write", text, "told to record what it needs, not merely that bytes vanish")
        self.assertIn("prose", text)

    def test_a_missing_staged_config_is_left_alone_rather_than_invented(self):
        missing = self.runtime.parent / "not-yet-assembled"
        STEP.gate_server(missing, True)  # must not raise
        self.assertFalse((missing / "assets" / "mcp.json").exists())

    def test_the_two_halves_are_written_from_one_decision(self):
        """Guidance text and enforcement must never disagree about the same launch."""
        for answer in (STEP.ON, STEP.OFF):
            with self.subTest(answer=answer):
                STEP.record(self.runtime, answer)
                enabled = (self.runtime / STEP.OUTPUT_ENV).read_text().strip().endswith("=1")
                described = bool((self.runtime / STEP.GUIDANCE_FILE).read_text().strip())
                tool = "images" in self.mcp()
                self.assertEqual(enabled, answer == STEP.ON)
                self.assertEqual(described, enabled)
                self.assertEqual(tool, enabled)


class ConsistencyTests(unittest.TestCase):
    """Three files have to agree on one default, and drift would be invisible."""

    def test_compose_defaults_to_the_same_answer_as_the_screen(self):
        compose = (ROOT / "compose.yaml").read_text()

        self.assertIn(
            f'MODEL_PROXY_IMAGE_EVICTION: "${{MODEL_PROXY_IMAGE_EVICTION:-'
            f'{0 if STEP.FIRST_DEFAULT == STEP.OFF else 1}}}"',
            compose,
        )

    def test_the_documented_example_matches_that_default(self):
        example = (ROOT / ".env.example").read_text()

        self.assertIn("MODEL_PROXY_IMAGE_EVICTION=0", example)

    def test_the_step_is_in_the_interactive_sequence_before_the_keys(self):
        order = FLOW.STEPS

        self.assertIn(FLOW.EVICTION, order)
        self.assertLess(order.index(FLOW.CONTEXT), order.index(FLOW.EVICTION))
        self.assertLess(order.index(FLOW.EVICTION), order.index(FLOW.CREDENTIALS),
                        "asking for keys stays the last question of the launch")

    def test_the_shipped_mcp_registration_matches_the_shipped_default(self):
        """The tool ships disabled, because the feature ships declined.

        Otherwise a launch that never reaches the gate — a read-only entry point, or one that
        dies before the step — would offer an agent a re-read tool for a reduction it is not
        getting, and the tool's own description would be false about the session.
        """
        servers = json.loads((ROOT / "runtime/mcp.json").read_text())["mcpServers"]

        self.assertIn(STEP.SERVER_KEY, servers, "the server must be declared to be gated")
        self.assertEqual(
            servers[STEP.SERVER_KEY]["enabled"],
            STEP.FIRST_DEFAULT == STEP.ON,
            "registration default and launch default are one decision, not two",
        )

    def test_the_launcher_offers_the_same_step_list(self):
        """start.sh keeps its own comma list; if it drifts the rail miscounts silently."""
        launcher = (ROOT / "start.sh").read_text()
        declared = next(
            line.split("=", 1)[1].strip()
            for line in launcher.splitlines()
            if line.startswith("flow_steps=")
        )

        self.assertEqual(declared.split(","), list(FLOW.STEPS))


if __name__ == "__main__":
    unittest.main()
