import json
import re
import subprocess
import sys
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

    def test_prompts_ask_for_models_before_the_kimi_version(self):
        # Every downstream number - lane sizes, fan-out, the proxy plan - is derived from
        # the model pair, so the pickers must run before scripts/select_versions.py.
        launcher = (ROOT / "start.sh").read_text()
        needles = {
            "select": "tools/models.py select",
            "resolve": "tools/models.py resolve",
            "modules": "tools/modules.py select",
            "version": "select_versions.py",
        }
        for name, needle in needles.items():
            self.assertIn(needle, launcher, f"start.sh no longer invokes {name}")
        positions = {name: launcher.index(needle) for name, needle in needles.items()}
        self.assertLess(positions["select"], positions["resolve"])
        self.assertLess(positions["resolve"], positions["modules"])
        self.assertLess(positions["resolve"], positions["version"])
        models = (ROOT / "tools" / "models.py").read_text()
        self.assertIn("last used", models)

    def test_picker_ordering_is_alphabetical_by_the_label_the_operator_reads(self):
        # Directory ids deliberately sort the opposite way to the labels: an implementation
        # that ordered by id would return alpha, bravo, delta, mike, zeta and fail here. The
        # bravo/delta pair shares a casefolded label, so it also pins the id tie-break and
        # proves the comparison is case-insensitive.
        if str(ROOT / "tools") not in sys.path:
            sys.path.insert(0, str(ROOT / "tools"))
        import models as model_setup

        def entry(model_id: str, label: str, lane: str = "primary") -> dict:
            return {"id": model_id, "label": label, "lanes": [lane]}

        chosen = model_setup.selectable(
            [
                entry("zeta", "Alpha"),
                entry("mike", "beta"),
                entry("delta", "charlie"),
                entry("bravo", "Charlie"),
                entry("alpha", "Zulu"),
                entry("offlane", "Not offered", "subagent"),
            ],
            "primary",
        )
        self.assertEqual(
            [model["id"] for model in chosen],
            ["zeta", "mike", "bravo", "delta", "alpha"],
            "labels must sort case-insensitively, ties broken by id, other lanes filtered out",
        )


COMPOSE_SOURCE = (ROOT / "compose.yaml").read_text()
BOOTSTRAP_SOURCE = (ROOT / "compose.bootstrap.yaml").read_text()
ENV_EXAMPLE = (ROOT / ".env.example").read_text()
LANES = ("primary", "long", "subagent")


def compose_value(key: str) -> int:
    """The numeric fallback a compose environment entry is defaulted to."""
    match = re.search(rf'^\s*{key}: "\$\{{[A-Z_]+:?[-]?(-?\d+)\}}"', COMPOSE_SOURCE, re.MULTILINE)
    if match is None:
        raise AssertionError(f"{key} is not defaulted to a number in compose.yaml")
    return int(match.group(1))


def shipped_plan() -> dict:
    """Resolve the checked-in definitions exactly as ./start.sh does."""
    if str(ROOT / "tools") not in sys.path:
        sys.path.insert(0, str(ROOT / "tools"))
    import definitions
    import policy

    providers, models = definitions.load_definitions(ROOT)
    resolved = {}
    for provider_id, provider in providers.items():
        # The launcher only applies .env overrides; tests must not read operator state.
        resolved[provider_id] = dict(provider)
    return policy.resolve(
        resolved,
        {model["id"]: model for model in models},
        {"primary": models[0]["id"], "subagent": models[0]["id"]},
        reserved_context_size=policy_reserved(),
    )


def policy_reserved() -> int:
    with (ROOT / "runtime" / "config.toml").open("rb") as source:
        return tomllib.load(source)["loop_control"]["reserved_context_size"]


class NoHardcodedModelFactsTests(unittest.TestCase):
    """The point of ./models and ./providers: nothing else may restate them."""

    def test_env_example_names_no_model_or_policy_number(self):
        forbidden = re.compile(
            r"^(LITELLM_|NRP_(MODEL|FAIR|PARALLEL|OUTPUT|PRIMARY|LONG|SUBAGENT|UPSTREAM)"
            r"|KIMI_SUBAGENT_CONCURRENCY=)",
            re.MULTILINE,
        )
        # MODEL_PROXY_* tunables and the credential/env names are allowed; a retired
        # model or policy variable is not.
        offenders = [
            line.split("=", 1)[0]
            for line in ENV_EXAMPLE.splitlines()
            if forbidden.match(line) and not line.startswith("NRP_API_KEY")
        ]
        self.assertEqual(offenders, [])
        self.assertIn("NRP_API_KEY=", ENV_EXAMPLE)
        # Blank means "derive it from the selected definitions".
        self.assertRegex(ENV_EXAMPLE, r"(?m)^KIMI_BACKGROUND_TASK_SLOTS=$")

    def test_no_tracked_file_mentions_litellm_except_the_nrp_endpoint(self):
        # LiteLLM is one gateway an operator might happen to have behind the endpoint, and the
        # harness must not assume it. Four mentions are deliberately recorded here rather than
        # left to drift, each for a stated reason.
        allowed = {
            "providers/nrp/provider.toml": "the real endpoint and key-issuing URLs",
            "docs/models-providers.md": "the definition example, quoted from the file above",
            "README.md": "the migration table, which names the variable being retired",
            "tools/runtime.sh": "the retired-variable list, which warns an operator off it",
        }
        for path in sorted(ROOT.rglob("*")):
            if (
                not path.is_file()
                or ".git" in path.parts
                or ".local" in path.parts
                or "__pycache__" in path.parts
                or ".ruff_cache" in path.parts
                or path.suffix in {".so", ".pyc"}
            ):
                continue
            try:
                text = path.read_text()
            except (UnicodeDecodeError, OSError):
                continue
            where = path.relative_to(ROOT).as_posix()
            if where == "tests/test_configuration.py":
                continue  # the allow list above
            if "litellm" not in text.lower():
                continue
            self.assertIn(where, allowed, f"{where} assumes which gateway is behind the endpoint")
            if where != "providers/nrp/provider.toml":
                continue
            for line in text.splitlines():
                if "litellm" in line.lower():
                    self.assertTrue(
                        line.startswith(("base_url =", "key_url =", "# ")),
                        f"{where}: only the endpoint URL, key URL, and comments may name it: "
                        f"{line}",
                    )

    def test_runtime_agents_md_carries_no_model_number(self):
        text = (ROOT / "runtime" / "AGENTS.md").read_text()
        for token in ("262,144", "1,000,000", "320,000", "350,000", "64,000", "200,000"):
            self.assertNotIn(token, text)
        # The contract must send the reader to the generated section instead.
        self.assertIn("Model runtime envelope", text)

    def test_bootstrap_declares_every_name_the_definitions_read(self):
        plan = shipped_plan()
        if str(ROOT / "tools") not in sys.path:
            sys.path.insert(0, str(ROOT / "tools"))
        import models as model_setup

        for name in model_setup.bootstrap_names(plan):
            self.assertIn(f"{name}:", BOOTSTRAP_SOURCE)

    def test_compose_declares_no_provider_credential(self):
        # Credential names come from the generated fragment, so compose.yaml cannot name one
        # and mount hygiene stays independent of which providers are installed.
        for path in sorted((ROOT / "providers").iterdir()):
            if not path.is_dir():
                continue
            for credential in re.findall(
                r'^env = "(.+)"$', (path / "provider.toml").read_text(), re.MULTILINE
            ):
                self.assertNotIn(
                    credential,
                    COMPOSE_SOURCE,
                    f"compose.yaml interpolates {credential} directly; the fragment should own it",
                )


class ResolvedEnvelopeTests(unittest.TestCase):
    """The shipped definitions must still resolve to the envelope we advertise."""

    @classmethod
    def setUpClass(cls):
        cls.plan = shipped_plan()
        cls.lanes = cls.plan["lanes"]
        cls.counters = cls.plan["counters"]

    def context_counter(self) -> dict:
        return next(c for c in self.counters.values() if c["family"] == "context")

    def shipped_slug(self, model_id: str) -> str:
        """The slug the checked-in definition declares, read independently of the plan.

        Deriving the expectation from the plan itself would make the alias assertion
        unfalsifiable: renaming a model moves both sides at once.
        """
        if str(ROOT / "tools") not in sys.path:
            sys.path.insert(0, str(ROOT / "tools"))
        import definitions

        _, models = definitions.load_definitions(ROOT)
        return next(model["slug"] for model in models if model["id"] == model_id)

    def test_three_lanes_exist_and_bind_to_their_own_route(self):
        self.assertEqual(set(self.lanes), set(LANES))
        for lane, entry in self.lanes.items():
            with self.subTest(lane=lane):
                self.assertEqual(entry["route"], f"/{lane}/v1")
                self.assertEqual(
                    entry["alias"], f"{self.shipped_slug(entry['model_id'])}-{lane}"
                )
                self.assertLessEqual(
                    entry["input_tokens"] + entry["output_clamp_tokens"],
                    entry["context_tokens"],
                )
                self.assertLessEqual(entry["input_tokens"] + self.plan["reserved_context_size"],
                                     entry["context_tokens"])

    def test_one_model_in_all_lanes_shares_one_context_counter(self):
        counter = self.context_counter()
        self.assertEqual(len(counter["lanes"]), len(LANES))
        self.assertTrue(self.plan["limits"]["shared_context_counters"])
        # One model serving three lanes must contend for one budget, not three.
        self.assertEqual(len([c for c in self.counters.values() if c["family"] == "context"]), 1)

    def test_aggregate_budget_is_the_threshold_times_the_margin(self):
        counter = self.context_counter()
        provider = self.plan["providers"][counter["provider"]]
        percent = min(
            rule["value"]
            for rule in provider["rules"]
            if rule["kind"] == "aggregate_context_fraction"
        )
        self.assertEqual(
            counter["ceiling"], counter["advertised_tokens"] * percent // 100
        )
        self.assertEqual(
            counter["budget"],
            min(counter["ceiling"], counter["ceiling"] * provider["context_margin_percent"] // 100),
        )
        self.assertLessEqual(counter["budget"], counter["ceiling"])

    def test_fanout_is_the_largest_the_budget_allows(self):
        subagent = self.lanes["subagent"]
        counter = self.context_counter()
        self.assertEqual(
            self.plan["limits"]["subagent_concurrency"],
            counter["budget"] // subagent["reservation"],
        )
        self.assertEqual(
            self.plan["limits"]["subagent_concurrency_basis"], "the tightest provider rule"
        )
        # Greedy: one more would genuinely not fit, rather than being a hand-picked cap.
        self.assertGreaterEqual(
            (self.plan["limits"]["subagent_concurrency"] + 1) * subagent["reservation"],
            counter["budget"],
        )
        declared = next(
            rule["value"]
            for rule in self.plan["providers"]["nrp"]["rules"]
            if rule["kind"] == "max_concurrent_requests"
        )
        # The context budget, not the provider's own request cap, must be what limits fan-out.
        # Named in the failure so a future cap change reports which rule became binding.
        self.assertLess(
            self.plan["limits"]["subagent_concurrency"],
            declared,
            f"the provider count rule ({declared}) is binding, not the context budget "
            f"({counter['budget']})",
        )

    def test_long_lane_is_exclusive_and_primary_is_not(self):
        self.assertTrue(self.lanes["long"]["exclusive"])
        for lane in ("primary", "subagent"):
            self.assertFalse(self.lanes[lane]["exclusive"])

    def test_the_primary_lane_overlaps_one_subagent_but_not_the_whole_swarm(self):
        budget = self.context_counter()["budget"]
        primary = self.lanes["primary"]["reservation"]
        subagent = self.lanes["subagent"]["reservation"]
        fan_out = self.plan["limits"]["subagent_concurrency"]
        # The margin exists to buy exactly this: one subagent concurrent with a primary request.
        self.assertLessEqual(primary + subagent, budget)
        # A full swarm beside a primary request is not, so the proxy queues it instead.
        self.assertGreater(fan_out * subagent + primary, budget)

    def test_rate_ledger_is_booked_in_the_provider_unit(self):
        rates = [c for c in self.counters.values() if c["family"] == "rate"]
        self.assertTrue(rates)
        for rate in rates:
            self.assertEqual(rate["unit"], "output_tokens")
            provider = self.plan["providers"][rate["provider"]]
            limit = next(
                rule["value"]
                for rule in provider["rules"]
                if rule["kind"] == rate["kind"] and rule["scope"] == rate["scope"]
            )
            self.assertEqual(
                rate["capacity"], limit * provider["output_rate_margin_percent"] // 100
            )

    def test_readme_quotes_the_plan_it_claims_to_quote(self):
        # README.md and docs/verification.md restate derived numbers in prose. This is the
        # only thing that notices when a margin edit in providers/nrp makes that prose stale.
        if str(ROOT / "tools") not in sys.path:
            sys.path.insert(0, str(ROOT / "tools"))
        readme = (ROOT / "README.md").read_text()
        verification = (ROOT / "docs" / "verification.md").read_text()
        counter = self.context_counter()
        rate = next(c for c in self.counters.values() if c["family"] == "rate")
        figures = {
            "advertised context": counter["advertised_tokens"],
            "aggregate ceiling": counter["ceiling"],
            "in-flight budget": counter["budget"],
            "rate ledger": rate["capacity"],
        }
        for lane in ("primary", "long", "subagent"):
            figures[f"{lane} reservation"] = self.lanes[lane]["reservation"]
        for name, value in figures.items():
            with self.subTest(figure=name):
                self.assertIn(f"{value:,}", readme, f"README does not state the {name}")
        fan_out = self.plan["limits"]["subagent_concurrency"]
        self.assertIn(f"which is {fan_out} at once", readme)
        # doctor.sh's condensed line is documented with the same budget, lane and permit count.
        self.assertIn(f"context budget {counter['budget']}", verification)
        self.assertIn(f"{fan_out} subagent permits", verification)
        self.assertIn(f"{len(self.lanes)} lanes", verification)

    def test_background_capacity_does_not_steal_a_subagent_lane(self):
        if str(ROOT / "tools") not in sys.path:
            sys.path.insert(0, str(ROOT / "tools"))
        import models as model_setup

        derived = model_setup.model_environment(self.plan, {})
        self.assertEqual(
            derived["KIMI_SUBAGENT_CONCURRENCY"],
            str(self.plan["limits"]["subagent_concurrency"]),
        )
        self.assertGreaterEqual(
            int(derived["KIMI_BACKGROUND_TASK_SLOTS"]),
            self.plan["limits"]["subagent_concurrency"],
        )
        self.assertEqual(
            compose_value("KIMI_CODE_BACKGROUND_BASH_TASK_TIMEOUT_S"),
            0,
            "background Bash must default to no wall-clock timeout",
        )

    def test_subagent_wall_clock_is_unlimited_and_policy_pinned(self):
        # Unlimited has to live here: the equivalent environment variables outrank
        # this file and reject 0, so expressing it in compose.yaml would fail startup.
        with (ROOT / "runtime" / "config.toml").open("rb") as source:
            config = tomllib.load(source)
        self.assertEqual(config["subagent"]["timeout_ms"], 0)
        self.assertEqual(config["swarm"]["timeout_ms"], 0)
        policy_document = json.loads((ROOT / "runtime" / "config-policy.json").read_text())
        for table in ("subagent", "swarm", "secondary_model", "default_model", "models"):
            self.assertNotIn(table, policy_document["user_owned"])
        for key in ("KIMI_SUBAGENT_TIMEOUT_MS", "KIMI_CODE_SWARM_TIMEOUT_MS"):
            self.assertIsNone(re.search(rf'^\s*{key}\s*:', COMPOSE_SOURCE, re.MULTILINE))

    def test_guidance_publishes_the_envelope_and_tells_the_agent_to_fill_it(self):
        if str(ROOT / "tools") not in sys.path:
            sys.path.insert(0, str(ROOT / "tools"))
        import policy as policy_module

        text = policy_module.render_guidance(self.plan)
        limit = self.plan["limits"]["subagent_concurrency"]
        self.assertIn(f"up to {limit} subagents concurrently", text)
        self.assertIn("Use the whole envelope", text)
        self.assertIn("Model runtime envelope", text)
        # The provider's published terms are cited so a reader can check the transcription.
        self.assertIn(self.plan["providers"]["nrp"]["policy_url"], text)
        for lane in self.plan["lanes"].values():
            self.assertIn(lane["alias"], text)


if __name__ == "__main__":
    unittest.main()
