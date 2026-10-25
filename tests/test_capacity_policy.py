"""Engine capacity joins provider policy without changing remote allowances."""

import copy
import tempfile
import tomllib
import unittest
from pathlib import Path

from tests.test_definitions import LOCAL_LOCK, MODEL, PROVIDER, write_tree
from tools import definitions, model_config, policy

LOCAL_PROVIDER = """
schema_version = 2
label = "Local Fixture"
requires = ["metal"]
rule = []

[execution]
kind = "local"
engine = "llama_cpp"

[execution.profile]
context_granularity = 128
max_sequences = 16
batch_tokens = 128
microbatch_tokens = 32
cache_type_k = "f16"
cache_type_v = "f16"
flash_attention = "auto"
load_mode = "auto"
lazy_mode = "auto"
gpu_layers = "all"
kv_unified = false
cache_ram_mib = 0
context_checkpoints = 0

[capacity]
strategy = "native_context"
sequence_slots = 1

[endpoint]
base_url = "http://host.docker.internal:9001"
protocol = "openai"
sends_cache_salt = false

[[credential]]
id = "default"
label = "Engine token"
env = "FIXTURE_LOCAL_KEY"
generate = true
"""

LOCAL_MODEL = """
schema_version = 2
label = "Fixture Model"
provider = "local_fixture"
model = "fixture-local"
slug = "localfixture"
credential = "default"

[context]
native_tokens = 16384

[lane.primary]
allocation = "engine"
output_mode = "remaining_context"

[lane.subagent]
allocation = "engine"
output_mode = "remaining_context"
default_effort = "low"

[requires]
disk_gib = 2

[install]
variant = "test_quant"
port = 9001
"""


class CapacityFixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        # These small fictitious windows exercise policy arithmetic, not model defaults.
        remote_model = (
            MODEL.replace("context_tokens = 8192", "context_tokens = 4096")
            .replace("input_tokens = 6144", "input_tokens = 3072")
            .replace("output_clamp_tokens = 2048", "output_clamp_tokens = 1024")
        )
        # Keep the subagent reservation below the aggregate context budget.
        remote_model = remote_model.replace(
            "[lane.subagent]\ncontext_tokens = 4096\ninput_tokens = 3072\n"
            "output_clamp_tokens = 1024",
            "[lane.subagent]\ncontext_tokens = 2048\ninput_tokens = 1536\n"
            "output_clamp_tokens = 512",
        )
        write_tree(self.root, PROVIDER, {"remote": remote_model})
        write_tree(self.root, LOCAL_PROVIDER, {"local": LOCAL_MODEL}, provider_id="local_fixture")
        (self.root / "models/local_fixture/local/weights.lock.json").write_text(LOCAL_LOCK)
        self.providers, items = definitions.load_definitions(self.root)
        self.models = {model["id"]: model for model in items}
        self.local = "local_fixture/local"
        self.remote = "fixture/remote"

    def allocation(self, sequences=1):
        return {
            "engines": {
                self.local: {
                    "context_tokens": 16384,
                    "sequences": sequences,
                    "total_context_tokens": 16384 * sequences,
                    "fingerprint": "fixture-capacity-identity",
                    "allocation": copy.deepcopy(
                        self.providers["local_fixture"]["execution"]["profile"]
                    ),
                    "capacity_strategy": "native_context",
                    "memory_management": "os_managed",
                    "native_context_tokens": 16384,
                }
            },
        }

    def resolve(self, primary=None, subagent=None, capacity=None):
        return policy.resolve(
            self.providers,
            self.models,
            {"primary": primary or self.local, "subagent": subagent or self.local},
            reserved_context_size=512,
            agent_lane="primary",
            capacity=capacity,
        )


class CapacityDefinitionTests(CapacityFixture):
    def test_local_capacity_explicitly_permits_no_fair_use_rules(self):
        provider = self.providers["local_fixture"]
        self.assertEqual(provider["rules"], [])
        self.assertEqual(provider["capacity"]["sequence_slots"], 1)
        self.assertEqual(provider["capacity"]["validation_status"], "pending_native_validation")
        self.assertEqual(self.models[self.local]["native_tokens"], 16384)

    def refuse_provider(self, replacement):
        path = self.root / "providers/local_fixture/provider.toml"
        path.write_text(replacement)
        with self.assertRaises(definitions.DefinitionError):
            definitions.load_definitions(self.root)

    def test_no_rules_must_be_explicit(self):
        self.refuse_provider(LOCAL_PROVIDER.replace("rule = []\n", ""))

    def test_native_context_allows_static_host_eligibility_floors(self):
        path = self.root / "models/local_fixture/local/model.toml"
        path.write_text(LOCAL_MODEL.replace("[requires]", "[requires]\nmemory_gib = 4"))
        providers, items = definitions.load_definitions(self.root)
        model = next(item for item in items if item["id"] == self.local)
        self.assertEqual(model["requires"]["memory_gib"], 4)
        self.assertEqual(providers[model["provider"]]["capacity"]["strategy"], "native_context")

    def test_derived_envelopes_refuse_unsupported_per_request_provider_rules(self):
        path = self.root / "providers/local_fixture/provider.toml"
        for kind in ("max_context_tokens", "max_output_tokens_per_request"):
            provider = LOCAL_PROVIDER.replace("rule = []\n", "") + (
                f'\n[[rule]]\nkind = "{kind}"\nscope = "model"\nlimit = 4096\n'
            )
            path.write_text(provider)
            with (
                self.subTest(kind=kind),
                self.assertRaisesRegex(
                    definitions.DefinitionError, f"engine-derived local envelopes.*{kind}"
                ),
            ):
                definitions.load_definitions(self.root)

    def test_capacity_can_still_combine_with_supported_provider_obligations(self):
        path = self.root / "providers/local_fixture/provider.toml"
        path.write_text(
            LOCAL_PROVIDER.replace("rule = []\n", "")
            + """
[[rule]]
kind = "max_concurrent_requests"
scope = "model"
limit = 2

[[rule]]
kind = "requests_per_minute"
scope = "provider"
limit = 10

[[rule]]
kind = "aggregate_context_fraction"
scope = "model"
percent = 75
"""
        )
        providers, _models = definitions.load_definitions(self.root)
        self.assertEqual(len(providers["local_fixture"]["rules"]), 3)

    def test_native_capacity_configuration_is_strict(self):
        for old, new in (
            ("sequence_slots = 1", "sequence_slots = 0"),
            ("sequence_slots = 1", "sequence_slots = 17"),
            ("sequence_slots = 1", "headroom_gib = 0\nsequence_slots = 1"),
            ("kv_unified = false", "kv_unified = true"),
            ("cache_ram_mib = 0", "cache_ram_mib = 1"),
            ("max_sequences = 16", "max_sequences = 64"),
            ('engine = "llama_cpp"', 'engine = "unknown_engine"'),
            ('protocol = "openai"', 'protocol = "anthropic"'),
        ):
            with self.subTest(replacement=new):
                self.refuse_provider(LOCAL_PROVIDER.replace(old, new))

    def test_validation_status_cannot_fabricate_evidence(self):
        self.refuse_provider(
            LOCAL_PROVIDER.replace(
                "sequence_slots = 1",
                'sequence_slots = 1\nvalidation_status = "validated"',
            )
        )

    def test_derived_envelopes_cannot_mix_static_caps(self):
        path = self.root / "models/local_fixture/local/model.toml"
        for old, new in (
            ('allocation = "engine"', 'allocation = "engine"\ncontext_tokens = 4096'),
            ("native_tokens = 16384", "advertised_tokens = 16384"),
        ):
            with self.subTest(replacement=new):
                path.write_text(LOCAL_MODEL.replace(old, new))
                with self.assertRaises(definitions.DefinitionError):
                    definitions.load_definitions(self.root)


class CapacityResolutionTests(CapacityFixture):
    def test_same_model_has_one_engine_and_one_shared_slot_gate(self):
        plan = self.resolve(capacity=self.allocation())
        self.assertEqual(plan["schema_version"], 3)
        self.assertNotIn("resources", plan)
        self.assertEqual(len(plan["engines"]), 1)
        counters = list(plan["counters"].values())
        self.assertEqual(len(counters), 1)
        self.assertEqual(counters[0]["scope"], "engine")
        self.assertEqual(counters[0]["lanes"], ["primary", "subagent"])
        self.assertEqual(counters[0]["max"], 1)
        self.assertEqual(plan["limits"]["subagent_concurrency"], 1)
        for lane in plan["lanes"].values():
            self.assertEqual(lane["reservation"], 16384)
            self.assertEqual(lane["input_tokens"], 15872)
            self.assertEqual(lane["output_clamp_tokens"], 16384)
            self.assertEqual(lane["output_mode"], "remaining_context")
        rendered = tomllib.loads(model_config.render(plan))
        self.assertEqual(rendered["models"]["localfixture-primary"]["max_context_size"], 16384)
        self.assertTrue(rendered["secondary_model"]["force"])

    def test_local_capacity_is_required_before_publishing_kimi_envelopes(self):
        with self.assertRaisesRegex(policy.ResolutionError, "native engine configuration"):
            self.resolve()

    def test_stale_memory_admission_and_profile_drift_fail_before_launch(self):
        mutations = [
            lambda a: a.update(resources={"host_ram": {"capacity_bytes": True}}),
            lambda a: a["engines"][self.local].update(allocations={"host_ram": 9 * 1024**3}),
            lambda a: a["engines"][self.local].update(allocations={"unknown": 1024}),
            lambda a: a["engines"][self.local].update(allocations={"host_ram": True}),
            lambda a: a["engines"][self.local]["allocation"].update(cache_type_k="q8_0"),
        ]
        for mutate in mutations:
            allocation = self.allocation()
            mutate(allocation)
            with self.subTest(allocation=allocation), self.assertRaises(policy.ResolutionError):
                self.resolve(capacity=allocation)

    def test_different_local_models_use_independent_native_engines(self):
        directory = self.root / "models/local_fixture/second"
        directory.mkdir()
        directory.joinpath("model.toml").write_text(
            LOCAL_MODEL.replace('slug = "localfixture"', 'slug = "secondfixture"')
            .replace('model = "fixture-local"', 'model = "fixture-second"')
            .replace("port = 9001", "port = 9002")
        )
        directory.joinpath("weights.lock.json").write_text(LOCAL_LOCK)
        self.providers, items = definitions.load_definitions(self.root)
        self.models = {model["id"]: model for model in items}
        allocation = self.allocation(sequences=1)
        allocation["engines"]["local_fixture/second"] = copy.deepcopy(
            allocation["engines"][self.local]
        )
        allocation["engines"]["local_fixture/second"].update(
            sequences=1, total_context_tokens=16384
        )
        plan = self.resolve(self.local, "local_fixture/second", allocation)
        self.assertEqual(len(plan["engines"]), 2)
        self.assertEqual(plan["limits"]["subagent_concurrency"], 1)
        self.assertTrue(
            set(plan["lanes"]["primary"]["counters"]).isdisjoint(
                plan["lanes"]["subagent"]["counters"]
            )
        )

    def test_largest_lane_needs_a_full_slot_for_every_admitted_sequence(self):
        for field, value in (
            ("total_context_tokens", 8192),
            ("context_tokens", 16385),
            ("context_tokens", 8193),
            ("sequences", True),
            ("sequences", 17),
            ("context_tokens", 512),
        ):
            allocation = self.allocation()
            allocation["engines"][self.local][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(policy.ResolutionError):
                self.resolve(capacity=allocation)

    def test_unselected_engine_allocation_is_refused(self):
        allocation = self.allocation()
        allocation["engines"]["local_fixture/unselected"] = copy.deepcopy(
            allocation["engines"][self.local]
        )
        with self.assertRaisesRegex(policy.ResolutionError, "exactly"):
            self.resolve(capacity=allocation)

    def test_remote_only_remains_a_v1_plan(self):
        baseline = self.resolve(self.remote, self.remote)
        self.assertEqual(baseline, self.resolve(self.remote, self.remote, capacity={"engines": {}}))
        self.assertEqual(baseline["schema_version"], 1)
        self.assertNotIn("engines", baseline)
        self.assertNotIn("output_mode", baseline["lanes"]["primary"])

    def test_local_primary_preserves_remote_subagent_allowance(self):
        baseline = self.resolve(self.remote, self.remote)
        mixed = self.resolve(self.local, self.remote, self.allocation(sequences=1))
        self.assertEqual(
            {
                key: value
                for key, value in mixed["lanes"]["subagent"].items()
                if key != "display_name"
            },
            {
                key: value
                for key, value in baseline["lanes"]["subagent"].items()
                if key != "display_name"
            },
        )
        self.assertEqual(
            mixed["limits"]["subagent_concurrency"], baseline["limits"]["subagent_concurrency"]
        )
        for key, counter in baseline["counters"].items():
            if "subagent" in counter["lanes"]:
                self.assertEqual(
                    {k: v for k, v in mixed["counters"][key].items() if k != "lanes"},
                    {k: v for k, v in counter.items() if k != "lanes"},
                )

    def test_local_subagent_slots_do_not_charge_remote_primary(self):
        mixed = self.resolve(self.remote, self.local, self.allocation(sequences=1))
        self.assertEqual(mixed["limits"]["subagent_concurrency"], 1)
        for counter_id in mixed["lanes"]["primary"]["counters"]:
            self.assertNotEqual(mixed["counters"][counter_id].get("scope"), "engine")
        for counter_id in mixed["lanes"]["subagent"]["counters"]:
            self.assertEqual(mixed["counters"][counter_id]["scope"], "engine")


if __name__ == "__main__":
    unittest.main()
