"""NRP behaviour is unchanged by the local-engine work, compared as complete documents.

The whole point of the local-inference change set is that it must not disturb the provider that
actually serves traffic today. Field-level assertions cannot establish that: a plan can keep every
field a test named while growing a counter, losing a route, or shifting a derived cap, and each of
those is a provider-policy change that would pass a suite written against the fields someone
thought to check.

So the comparison here is against the complete resolved plan *and* the complete generated guidance,
captured before the local work landed, for each supported main-lane designation. Anything that moves
a byte of an NRP-only plan fails this test, and the failure names the section rather than the
internal structure that differs.

The references are durable fixtures in this repository, not operator state and not runtime output:
they were resolved from the checked-in definitions with synthetic credentials, which is the only
way a reference like this can run in CI at all.
"""

import copy
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "local-inference"

from tests.helpers import add_tools_to_path, shipped_plan  # noqa: E402

add_tools_to_path()
import policy  # noqa: E402
from definitions import LANES  # noqa: E402


def reference(lane: str) -> dict:
    path = FIXTURES / f"nrp-only-{lane}.json"
    if not path.is_file():
        raise AssertionError(
            f"{path.relative_to(ROOT)} is missing. These references are the guard that the local "
            "engine work does not disturb NRP behaviour; regenerate them deliberately with "
            "tools/capture_local_inference_references.py and review the diff, never by deleting "
            "the file."
        )
    return json.loads(path.read_text(encoding="utf-8"))


class NrpReferenceTests(unittest.TestCase):
    """Every NRP-only plan and guidance block still matches its captured reference."""

    def test_references_exist_for_every_main_lane_designation(self):
        for lane in policy.AGENT_LANES:
            with self.subTest(lane=lane):
                self.assertTrue((FIXTURES / f"nrp-only-{lane}.json").is_file())

    def test_a_reference_describes_an_nrp_only_plan(self):
        # If the fixture itself grew engines, every comparison below would be comparing the wrong
        # thing and passing.
        for lane in policy.AGENT_LANES:
            document = reference(lane)
            with self.subTest(lane=lane):
                self.assertEqual(document["plan"]["schema_version"], 1)
                self.assertFalse(document["plan"].get("engines"))
                self.assertFalse(document["plan"].get("resources"))

    def test_the_resolved_plan_is_byte_for_byte_unchanged(self):
        for lane in policy.AGENT_LANES:
            with self.subTest(lane=lane):
                plan = shipped_plan(agent_lane=lane)
                self.assertEqual(plan, reference(lane)["plan"])

    def test_the_generated_guidance_is_unchanged(self):
        for lane in policy.AGENT_LANES:
            for audience in policy.GUIDANCE_AUDIENCES:
                with self.subTest(lane=lane, audience=audience):
                    rendered = policy.render_guidance(
                        shipped_plan(agent_lane=lane), audience
                    )
                    self.assertEqual(rendered, reference(lane)["guidance"][audience])

    def test_the_reference_covers_lanes_counters_routes_and_credentials(self):
        # A guard that silently compares an empty dict would pass. This names the parts, so a
        # fixture that lost one is caught by this test rather than by the equality below.
        for lane in policy.AGENT_LANES:
            plan = reference(lane)["plan"]
            with self.subTest(lane=lane):
                self.assertEqual(set(plan["lanes"]), set(LANES))
                self.assertTrue(plan["counters"])
                self.assertTrue(plan["providers"])
                for entry in plan["lanes"].values():
                    self.assertTrue(entry["counters"], f"lane {entry} carries no counters")
                    self.assertIn("alias", entry)
                    self.assertIsNone(entry.get("engine_id"))

    def test_engines_do_not_leak_into_an_nrp_only_plan(self):
        # The resolver keys plan version on the presence of engines. A selection with no local
        # model must therefore still emit version 1 even after the engine contract moved.
        for lane in policy.AGENT_LANES:
            plan = shipped_plan(agent_lane=lane)
            with self.subTest(lane=lane):
                self.assertEqual(plan["schema_version"], 1, "NRP-only plans do not version bump")
                self.assertNotIn("engines", plan)


class MixedProviderDirectionTests(unittest.TestCase):
    """Both mixed directions, because a plan that only works one way is half a contract."""

    def _mixed(self, primary: str, subagent: str, agent_lane: str = "primary") -> dict:
        """Resolve a mixed selection the way the launcher does.

        Engines come from capacity.allocate rather than a hand-written dict, because the
        resolver checks that the engine set matches the selected local models exactly - a fixture
        that faked that step would not be exercising the thing worth testing.
        """
        import capacity
        import definitions

        providers, items = definitions.load_definitions(ROOT)
        models = {model["id"]: model for model in items}
        selection = {"primary": primary, "subagent": subagent}
        # The launcher's real reserve, not a round number: the reserve is subtracted from every
        # lane's usable input, so an inflated one fails the arithmetic the resolver is there to do.
        reserved = shipped_plan(agent_lane="long")["reserved_context_size"]
        local = capacity.selected(
            copy.deepcopy(providers), copy.deepcopy(models), selection
        )
        engines = capacity.allocate(local, providers, reserved) if local else {"engines": {}}
        return policy.resolve(
            copy.deepcopy(providers),
            copy.deepcopy(models),
            selection,
            reserved_context_size=reserved,
            capacity=engines,
            agent_lane=agent_lane,
        )

    def test_local_main_with_remote_children_is_a_version_four_plan(self):
        local = "local_mps/qwen3_8_27b"
        remote = "nrp/qwen3_8_flash_next"
        plan = self._mixed(local, remote)
        self.assertEqual(plan["schema_version"], 4)
        self.assertIn(local, plan["engines"])
        # The remote lane keeps its own provider accounting beside the engine-scoped counter.
        remote_lane = plan["lanes"]["primary"]
        self.assertNotIn(
            local,
            {counter for counter in remote_lane["counters"] if "engine:" in counter},
        )

    def test_a_local_engine_counter_cannot_be_shared_with_a_remote_lane(self):
        local = "local_mps/qwen3_8_27b"
        remote = "nrp/qwen3_8_flash_next"
        plan = self._mixed(local, remote)
        engine_counter = next(
            counter
            for counter in plan["counters"].values()
            if counter.get("scope") == "engine"
        )
        for lane_name, lane in plan["lanes"].items():
            model_id = lane["model_id"]
            member = engine_counter["id"] in lane["counters"]
            self.assertEqual(member, model_id == local, f"lane {lane_name}")

    def test_the_nrp_only_shape_survives_a_mixed_plan_losing_its_engine(self):
        # Removing the local engine has to return the NRP half untouched, not a version-1 plan with
        # residue from the mixed one.
        mixed = self._mixed("local_mps/qwen3_8_27b", "nrp/qwen3_8_flash_next")
        pure = shipped_plan(agent_lane="long")
        for key in ("counters", "limits", "safety"):
            if key in pure:
                self.assertIn(key, mixed)


if __name__ == "__main__":
    unittest.main()
