import json
import re
import subprocess
import tempfile
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class ConfigurationTests(unittest.TestCase):
    def test_checked_in_json_and_toml_parse(self):
        for path in ROOT.rglob("*.json"):
            if "node_modules" not in path.parts and ".local" not in path.parts:
                json.loads(path.read_text())
        for path in ROOT.rglob("*.toml"):
            if ".local" in path.parts:
                continue
            with path.open("rb") as source:
                tomllib.load(source)

    def test_browser_sandbox_is_not_disabled(self):
        document = json.loads((ROOT / "runtime" / "mcp.json").read_text())
        args = document["mcpServers"]["chrome-devtools"]["args"]
        self.assertFalse(any("no-sandbox" in arg for arg in args))
        profile = json.loads((ROOT / "container" / "chromium-seccomp.json").read_text())
        allowed = [
            call
            for call in profile["syscalls"]
            if call.get("action") == "SCMP_ACT_ALLOW"
            and {"clone", "chroot", "setns", "unshare"}.issubset(call.get("names", []))
            and not call.get("includes")
        ]
        self.assertTrue(allowed)

    def test_all_base_images_are_digest_pinned(self):
        for relative in (
            "container/Dockerfile",
            "proxy/Dockerfile",
            "search-adapter/Dockerfile",
        ):
            for line in (ROOT / relative).read_text().splitlines():
                if line.startswith("FROM ") and "${" not in line:
                    image = line.split()[1]
                    self.assertRegex(image, r"@sha256:[0-9a-f]{64}$")

    def test_platform_key_is_not_persisted(self):
        # Match any form (dict literal, CLI flag, variable): the selector writes
        # a session file that is persisted and sourced by the launcher.
        selector = (ROOT / "scripts" / "select_versions.py").read_text()
        self.assertIsNone(
            re.search(r"platform[\s_-]*key", selector, re.IGNORECASE),
            "select_versions.py must not accept or emit a platform key",
        )

    def test_read_env_uses_last_resolved_value(self):
        with tempfile.NamedTemporaryFile("w", delete=False) as output:
            output.write("VALUE=first\nVALUE=second\n")
            path = Path(output.name)
        try:
            result = subprocess.run(
                ["python3", str(ROOT / "scripts" / "read_env.py"), str(path), "VALUE"],
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.stdout.strip(), "second")
        finally:
            path.unlink()

    def test_dependency_lock_has_only_digests_and_commits(self):
        lock = json.loads((ROOT / "dependencies.lock.json").read_text())
        self.assertTrue(lock["images"])
        self.assertTrue(
            all(re.fullmatch(r"sha256:[0-9a-f]{64}", value) for value in lock["images"].values())
        )
        self.assertTrue(
            all(re.fullmatch(r"[0-9a-f]{40}", value) for value in lock["sources"].values())
        )


COMPOSE_SOURCE = (ROOT / "compose.yaml").read_text()
PROXY_SOURCE = (ROOT / "proxy" / "nrp_proxy.py").read_text()
LANES = ("primary", "long", "subagent")


def compose_value(key: str) -> int:
    """The numeric fallback a compose environment entry is defaulted to."""
    match = re.search(rf'^\s*{key}: "\$\{{[A-Z_]+:-(-?\d+)\}}"', COMPOSE_SOURCE, re.MULTILINE)
    if match is None:
        raise AssertionError(f"{key} is not defaulted to a number in compose.yaml")
    return int(match.group(1))


def proxy_clamp(lane: str) -> int:
    """The proxy's built-in output-token clamp for one lane."""
    match = re.search(rf'"NRP_{lane.upper()}_MAX_OUTPUT_TOKENS", "(\d+)"', PROXY_SOURCE)
    if match is None:
        raise AssertionError(f"proxy has no default output clamp for lane {lane!r}")
    return int(match.group(1))


class FairUseInvariantsTests(unittest.TestCase):
    """The checked-in numbers must still add up to the NRP fair-use policy."""

    @classmethod
    def setUpClass(cls):
        with (ROOT / "runtime" / "config.toml").open("rb") as source:
            cls.config = tomllib.load(source)
        cls.clamps = {lane: proxy_clamp(lane) for lane in LANES}
        cls.models = {}
        for alias, model in cls.config["models"].items():
            provider = str(model.get("provider", ""))
            if provider.startswith("nrp-"):
                cls.models[provider.removeprefix("nrp-")] = (alias, model)
        cls.reserve = cls.config["loop_control"]["reserved_context_size"]

    def lane_reservation(self, lane: str) -> int:
        model = self.models[lane][1]
        return model["max_input_size"] + self.clamps[lane]

    def test_fair_use_numbers_come_from_compose_defaults(self):
        self.assertEqual(compose_value("NRP_MODEL_CONTEXT"), 1000000)
        self.assertEqual(compose_value("NRP_FAIR_USE_PERCENT"), 35)
        self.assertEqual(compose_value("NRP_PARALLEL_CONTEXT_BUDGET"), 320000)
        self.assertEqual(compose_value("NRP_OUTPUT_TOKENS_PER_MINUTE"), 200000)

    def test_every_lane_is_bound_to_its_own_provider_and_route(self):
        self.assertEqual(set(self.models), set(LANES))
        for lane, (_alias, model) in self.models.items():
            with self.subTest(lane=lane):
                provider = self.config["providers"][f"nrp-{lane}"]
                # A lane may not reach the model through another lane's route.
                self.assertEqual(model["provider"], f"nrp-{lane}")
                self.assertTrue(provider["base_url"].endswith(f"/{lane}/v1"))

    def test_output_clamp_and_reserve_fit_every_lane(self):
        for lane, (_alias, model) in self.models.items():
            with self.subTest(lane=lane):
                self.assertLessEqual(
                    model["max_input_size"] + self.clamps[lane], model["max_context_size"]
                )
                self.assertLessEqual(
                    model["max_input_size"] + self.reserve, model["max_context_size"]
                )
                self.assertLessEqual(model["max_context_size"], compose_value("NRP_MODEL_CONTEXT"))

    def test_subagents_are_forced_onto_the_subagent_lane(self):
        secondary = self.config["secondary_model"]
        self.assertTrue(secondary["force"])
        self.assertEqual(secondary["default_model"], self.models["subagent"][0])

    def test_subagent_wall_clock_is_unlimited_and_policy_pinned(self):
        # Unlimited has to live here: the equivalent environment variables outrank
        # this file and reject 0, so expressing it in compose.yaml would fail startup.
        self.assertEqual(self.config["subagent"]["timeout_ms"], 0)
        self.assertEqual(self.config["swarm"]["timeout_ms"], 0)
        policy = json.loads((ROOT / "runtime" / "config-policy.json").read_text())
        for table in ("subagent", "swarm"):
            self.assertNotIn(table, policy["user_owned"])

    def test_compose_reintroduces_no_wall_clock_cap(self):
        for key in ("KIMI_SUBAGENT_TIMEOUT_MS", "KIMI_CODE_SWARM_TIMEOUT_MS"):
            self.assertIsNone(re.search(rf'^\s*{key}\s*:', COMPOSE_SOURCE, re.MULTILINE))

    def test_subagent_fanout_is_the_largest_the_budget_allows(self):
        concurrency = compose_value("KIMI_CODE_AGENT_SWARM_MAX_CONCURRENCY")
        self.assertEqual(concurrency, compose_value("NRP_SUBAGENT_MAX_CONCURRENCY"))
        budget = compose_value("NRP_PARALLEL_CONTEXT_BUDGET")
        reservation = self.lane_reservation("subagent")
        self.assertLessEqual(concurrency * reservation, budget)
        self.assertEqual(concurrency, budget // reservation)
        self.assertLessEqual(concurrency + 1, compose_value("NRP_MODEL_MAX_CONCURRENCY"))

    def test_parallel_budget_stays_under_the_fair_use_ceiling(self):
        ceiling = (
            compose_value("NRP_MODEL_CONTEXT") * compose_value("NRP_FAIR_USE_PERCENT") // 100
        )
        self.assertLessEqual(compose_value("NRP_PARALLEL_CONTEXT_BUDGET"), ceiling)

    def test_primary_lane_is_shared_but_long_context_is_exclusive(self):
        ceiling = (
            compose_value("NRP_MODEL_CONTEXT") * compose_value("NRP_FAIR_USE_PERCENT") // 100
        )
        self.assertLess(self.lane_reservation("primary"), ceiling)
        self.assertGreaterEqual(self.lane_reservation("long"), ceiling)

    def test_a_primary_request_cannot_share_the_budget_with_a_subagent(self):
        budget = compose_value("NRP_PARALLEL_CONTEXT_BUDGET")
        overlap = self.lane_reservation("primary") + self.lane_reservation("subagent")
        self.assertGreater(overlap, budget)

    def test_background_slots_do_not_steal_the_subagent_lane(self):
        # [background] is user-owned, so compose also states the same numbers: the
        # environment wins if the volume copy drifts.
        self.assertEqual(
            self.config["background"]["max_running_tasks"],
            compose_value("KIMI_CODE_BACKGROUND_MAX_RUNNING_TASKS"),
        )
        self.assertEqual(
            self.config["background"]["bash_task_timeout_s"],
            compose_value("KIMI_CODE_BACKGROUND_BASH_TASK_TIMEOUT_S"),
        )
        self.assertGreaterEqual(
            compose_value("KIMI_CODE_BACKGROUND_MAX_RUNNING_TASKS"),
            compose_value("KIMI_CODE_AGENT_SWARM_MAX_CONCURRENCY"),
        )


if __name__ == "__main__":
    unittest.main()
