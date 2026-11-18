"""The renderer that turns a resolved plan into Kimi's model and provider tables.

`AGENTS.md` makes three demands of this output that nothing else checks: each lane must reach the
proxy through its OWN route (so the proxy can tell which policy admitted a request without trusting
a client header), the generated half must be tables because it is appended to a static baseline, and
``secondary_model.force`` must stay true so a child cannot silently run on the parent's model.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for _directory in (ROOT, ROOT / "tests", ROOT / "tools"):
    if str(_directory) not in sys.path:
        sys.path.insert(0, str(_directory))

import model_config  # noqa: E402


def lane(alias: str, provider: str, provider_name: str, effort: str = "") -> dict:
    return {
        "alias": alias,
        "provider": provider,
        "provider_name": provider_name,
        "model": "Qwen3.8-Flash",
        "context_tokens": 262144,
        "input_tokens": 196608,
        "capabilities": ["tool_use"],
        "support_efforts": ["low", "high"],
        "default_effort": effort,
        "display_name": "Qwen " + alias,
    }


def plan(**lanes) -> dict:
    lanes = lanes or {"primary": lane("a-primary", "nrp", "nrp__a"),
                      "subagent": lane("a-sub", "nrp", "nrp__b", "low")}
    return {
        "lanes": lanes,
        "providers": {entry["provider"]: {"protocol": "openai"} for entry in lanes.values()},
    }


class RenderTests(unittest.TestCase):
    def test_each_lane_gets_its_own_route_and_provider_table(self) -> None:
        text = model_config.render(plan())
        self.assertIn("base_url = \"http://model-proxy:8080/primary/v1\"", text)
        self.assertIn("base_url = \"http://model-proxy:8080/subagent/v1\"", text)
        self.assertEqual(text.count("[providers."), 2, "one table per lane, none shared")

    def test_secondary_model_is_bound_to_the_subagent_and_forced(self) -> None:
        # force = true is a standing requirement, not a default: without it a child could be served
        # the parent's model and no route would reveal that the subagent policy was bypassed.
        text = model_config.render(plan())
        self.assertIn("[secondary_model]", text)
        self.assertIn('default_model = "a-sub"', text)
        self.assertIn("force = true", text)

    def test_a_lane_without_an_effort_emits_no_effort_key(self) -> None:
        text = model_config.render(plan())
        primary = text.split("[models.a-sub]")[0]
        self.assertNotIn("default_effort", primary)
        self.assertIn('default_effort = "low"', text.split("[secondary_model]")[1])

    def test_a_plan_with_no_subagent_lane_is_refused_not_rendered(self) -> None:
        with self.assertRaisesRegex(ValueError, "no subagent lane to bind"):
            model_config.render(plan(primary=lane("a-primary", "nrp", "nrp__a")))

    def test_escaping_reaches_the_rendered_values(self) -> None:
        only = {"subagent": lane('we"ird', "nrp", "nrp__b")}
        only["subagent"]["display_name"] = 'back\\slash "q"'
        text = model_config.render({"lanes": only,
                                    "providers": {"nrp": {"protocol": "openai"}}})
        self.assertIn('display_name = "back\\\\slash \\"q\\""', text)

    def test_two_lanes_sharing_one_provider_name_are_refused(self) -> None:
        # Measured first: this used to emit `[providers.shared]` twice, each with a different
        # base_url because the route carries the lane name. One of the two halves would have won and
        # a lane would have been talking to the proxy off another lane's route.
        with self.assertRaisesRegex(
            ValueError, r"lanes primary and subagent share provider 'shared'"
        ):
            model_config.render(plan(
                primary=lane("a-primary", "nrp", "shared"),
                subagent=lane("a-sub", "nrp", "shared", "low"),
            ))


if __name__ == "__main__":
    unittest.main()
