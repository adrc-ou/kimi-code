"""Native-context resolution does not admit or resize models using physical memory."""

import copy
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
# ``tools/`` holds no package; capacity adds it to sys.path itself, and the adapter types need the
# same treatment before they can be imported here.
if str(ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(ROOT / "tools"))

from local_engines.base import Artifact, EngineIdentity  # noqa: E402

from tools import capacity  # noqa: E402

PROFILE = {
    "context_granularity": 256,
    "max_sequences": 8,
    "batch_tokens": 2048,
    "microbatch_tokens": 512,
    "cache_type_k": "f16",
    "cache_type_v": "f16",
    "flash_attention": "auto",
    "load_mode": "auto",
    "lazy_mode": "auto",
    "gpu_layers": "all",
    "kv_unified": False,
    "cache_ram_mib": 0,
    "context_checkpoints": 0,
}


def bound(model: dict, engine: str = "llama_cpp", profile_id: str = "default") -> dict:
    """Attach the fields ``definitions._bind_serving_profile`` puts on a model.

    ``allocate`` reads the profile the *model* named rather than a provider default, so a fixture
    that omits them is describing a manifest the parser would never produce.
    """
    return {
        **model,
        "serving_profile": profile_id,
        "serving_engine": engine,
        "serving_settings": PROFILE,
    }


class AllocationTests(unittest.TestCase):
    def setUp(self):
        self.providers = {
            "local": {
                "execution": {
                    "kind": "local",
                    "engine": "llama_cpp",
                    "profile": PROFILE,
                    "profiles": {
                        "default": {"engine": "llama_cpp", "settings": PROFILE},
                    },
                    "default_profile": "default",
                    "memory": {},
                },
                "capacity": {"strategy": "native_context", "sequence_slots": 1},
            }
        }
        self.models = [
            bound({"id": "local/a", "provider": "local", "native_tokens": 4096}),
            bound({"id": "local/b", "provider": "local", "native_tokens": 8192}),
        ]

    def test_distinct_engines_keep_native_windows_and_one_slot(self):
        result = capacity.allocate(self.models, self.providers, 1024)
        self.assertNotIn("resources", result)
        for model in self.models:
            engine = result["engines"][model["id"]]
            self.assertEqual(engine["context_tokens"], model["native_tokens"])
            self.assertEqual(engine["total_context_tokens"], model["native_tokens"])
            self.assertEqual(engine["sequences"], 1)
            self.assertEqual(engine["memory_management"], "os_managed")
            self.assertNotIn("allocations", engine)
        self.assertEqual(
            result, capacity.allocate(list(reversed(self.models)), self.providers, 1024)
        )

    def test_shared_engine_is_deduplicated_and_remote_roles_are_independent(self):
        catalog = {m["id"]: m for m in self.models}
        local = capacity.selected(
            self.providers, catalog, {"primary": "local/a", "subagent": "local/a"}
        )
        self.assertEqual(len(local), 1)
        self.assertEqual(
            capacity.allocate(local, self.providers, 1024)["engines"]["local/a"]["sequences"], 1
        )
        self.assertEqual(
            capacity.selected(
                self.providers, catalog, {"primary": "local/a", "subagent": "hosted/r"}
            ),
            local,
        )

    def test_reserve_alignment_and_engine_capabilities_still_apply(self):
        for context, slots in ((1024, 1), (4097, 1), (4096, 9), (4096, True)):
            self.models[0]["native_tokens"] = context
            self.providers["local"]["capacity"]["sequence_slots"] = slots
            with (
                self.subTest(context=context, slots=slots),
                self.assertRaises(capacity.CapacityError),
            ):
                capacity.allocate(self.models, self.providers, 1024)

    def test_remote_only_preparation_never_probes_or_installs(self):
        with mock.patch.object(capacity.host_caps, "probe") as probe:
            self.assertEqual(capacity.prepare(ROOT, ROOT, {}, {}, {}, 1024), {"engines": {}})
            probe.assert_not_called()


class PreparationTests(unittest.TestCase):
    def test_memory_and_projection_results_are_not_launch_inputs(self):
        from tests.test_capacity_policy import CapacityFixture

        fixture = CapacityFixture()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        model = fixture.models[fixture.local]
        selection = {"primary": fixture.local, "subagent": fixture.local}
        observations = [
            {"memory_bytes": 1, "available_memory_bytes": 1},
            {"memory_bytes": 128 * capacity.GIB, "available_memory_bytes": 100 * capacity.GIB},
            {"memory_bytes": 0, "available_memory_bytes": 0},
            {},
            OSError("measurement unavailable"),
        ]
        with (
            mock.patch.object(capacity.local_llm, "is_ready", return_value=True),
            mock.patch.object(capacity.local_llm, "engine_binary", return_value=Path("fixture")),
            mock.patch.object(
                capacity.local_llm,
                "primary_weights_path",
                return_value=fixture.root.resolve() / "fixture.gguf",
            ),
            # prepare() reaches runtime, identity and artifact through the adapter now, so the
            # adapter is the seam a test stands in for. The pinned macOS binary cannot execute on
            # this host, and no fixture here is a real GGUF tree.
            mock.patch.object(
                type(capacity.adapter_for("llama_cpp")),
                "identity",
                return_value=EngineIdentity(
                    kind="llama_cpp",
                    build_info="fixture b11846 abcdef0",
                    source_revision="0" * 40,
                    version="b11846",
                    build_commit="abcdef0",
                ),
            ),
            mock.patch.object(
                type(capacity.adapter_for("llama_cpp")),
                "validate_artifact",
                return_value=Artifact(
                    format="gguf",
                    variant="Q4",
                    primary=fixture.root.resolve() / "fixture.gguf",
                    paths=(),
                    tensor_bytes=1 << 30,
                    total_bytes=1 << 30,
                ),
            ),
            mock.patch.object(
                capacity, "engine_identity", return_value={"build_commit": "abcdef0"}
            ),
            mock.patch.object(capacity.platform, "system", return_value="Darwin"),
            mock.patch.object(capacity.platform, "machine", return_value="arm64"),
            mock.patch.object(capacity.host_caps, "probe", side_effect=observations),
            mock.patch.object(
                capacity, "project", side_effect=AssertionError("must not estimate")
            ) as project,
            mock.patch.object(
                capacity, "projection_engine", side_effect=AssertionError("no estimator required")
            ) as estimator,
        ):
            for _ in observations:
                plan = capacity.prepare(
                    fixture.root, fixture.root, fixture.providers, fixture.models, selection, 512
                )
                engine = plan["engines"][fixture.local]
                self.assertEqual(engine["context_tokens"], model["native_tokens"])
                self.assertEqual(engine["sequences"], 1)
                self.assertNotIn("resources", plan)
                self.assertIn("fingerprint", engine)
                self.assertIn("serving_profile_id", engine)
            with mock.patch.object(capacity.local_llm, "ROOT", fixture.root):
                capacity.local_llm.validate_launch(model, engine)
                for changes in (
                    {"context_tokens": 8192},
                    {"sequences": 2},
                    {"memory_management": "bounded"},
                ):
                    changed = dict(engine, **changes)
                    with (
                        self.subTest(changes=changes),
                        self.assertRaises(capacity.local_llm.EngineError),
                    ):
                        capacity.local_llm.validate_launch(model, changed)
        project.assert_not_called()
        estimator.assert_not_called()
        self.assertFalse((fixture.root / "capacity-projections.jsonl").exists())

    def test_unverified_weights_still_refuse_launch(self):
        with mock.patch.object(capacity.local_llm, "is_ready", return_value=False):
            with self.assertRaisesRegex(capacity.CapacityError, "verify"):
                capacity.prepare(
                    ROOT,
                    ROOT,
                    {"local": {"requires": ["metal"], "execution": {"kind": "local"}}},
                    {"local/a": {"id": "local/a", "provider": "local"}},
                    {"primary": "local/a"},
                    1024,
                )


class ProjectionTests(unittest.TestCase):
    def test_identity_pins_the_commit_in_addition_to_release_number(self):
        commit = "a" * 40
        with (
            mock.patch.object(
                capacity.local_llm,
                "engine_plan",
                return_value={"version": "b123", "commit": commit},
            ),
            mock.patch.object(
                capacity.subprocess,
                "run",
                return_value=mock.Mock(
                    stdout="version: test (build 123, commit aaaaaaa)\n", stderr=""
                ),
            ),
        ):
            self.assertEqual(capacity.engine_identity(Path("fixture"))["source_commit"], commit)
        with (
            mock.patch.object(
                capacity.local_llm,
                "engine_plan",
                return_value={"version": "b123", "commit": "b" * 40},
            ),
            mock.patch.object(
                capacity.subprocess,
                "run",
                return_value=mock.Mock(
                    stdout="version: test (build 123, commit aaaaaaa)\n", stderr=""
                ),
            ),
            self.assertRaisesRegex(capacity.CapacityError, "commit"),
        ):
            capacity.engine_identity(Path("fixture"))

    def event(self):
        return {
            "type": "fit_memory_breakdown",
            "data": {
                "unit": "MiB",
                "rows": [
                    {"kind": "device", "name": "Metal", "self": 10, "total": 128},
                    {"kind": "host", "name": "Host", "self": 2},
                ],
            },
        }

    def test_structured_projection_ignores_prose_and_rounds_total_rows_up(self):
        output = "Metal 10 0 0\n" + json.dumps(self.event()) + "\nHost 2 0 0\n"
        result = capacity.parse_projection(output)
        self.assertEqual(result["host_bytes"], 14 * capacity.MIB)
        self.assertEqual(result["devices"]["Metal"], 11 * capacity.MIB)

    def test_duplicate_unknown_and_malformed_projection_fails_closed(self):
        event = self.event()
        duplicate = json.dumps(event) + "\n" + json.dumps(event)
        bad = copy.deepcopy(event)
        bad["data"]["rows"][0]["total"] = 0
        for output in (duplicate, "{}", "{broken", json.dumps(bad)):
            with self.subTest(output=output), self.assertRaises(capacity.CapacityError):
                capacity.parse_projection(output)


if __name__ == "__main__":
    unittest.main()


class WiringTests(unittest.TestCase):
    """Residency is bounded from measurements, and never becomes an admission test."""

    def engines(self, *specs: tuple[str, int | None, str]) -> dict:
        return {
            model_id: {
                "engine_kind": kind,
                "tensor_bytes": tensor_bytes,
                "context_tokens": 4096,
                "sequences": 1,
            }
            for model_id, tensor_bytes, kind in specs
        }

    def providers(self, memory: dict | None = None) -> dict:
        memory = memory or {
            "strategy": "bounded_wiring",
            "target": "min_available_and_recommended",
            "distribution": "verified_weight_bytes",
            "unknown_measurements": "unwired",
        }
        return {
            "local": {
                "execution": {"kind": "local", "memory": memory},
                "capacity": {"strategy": "native_context", "sequence_slots": 1},
            }
        }

    def measured(self, available: int | None, recommended: int | None) -> dict:
        return {
            "available_memory_bytes": available,
            "metal": {"recommended_working_set_bytes": recommended},
        }

    def test_the_target_is_the_minimum_of_the_two_measurements(self):
        engines = self.engines(("local/a", 1000, "mlx_lm"))
        wiring = capacity.wiring_targets(engines, self.providers(), self.measured(500, 900))
        self.assertEqual(wiring["local/a"]["wired_bytes"], 500)

    def test_wiring_never_exceeds_the_weights_it_is_bounding(self):
        engines = self.engines(("local/a", 100, "mlx_lm"))
        wiring = capacity.wiring_targets(engines, self.providers(), self.measured(10_000, 10_000))
        self.assertEqual(wiring["local/a"]["wired_bytes"], 100)

    def test_two_workers_split_the_aggregate_by_verified_bytes(self):
        engines = self.engines(("local/a", 300, "mlx_lm"), ("local/b", 100, "mlx_lm"))
        wiring = capacity.wiring_targets(engines, self.providers(), self.measured(400, 400))
        self.assertEqual(wiring["local/a"]["wired_bytes"], 300)
        self.assertEqual(wiring["local/b"]["wired_bytes"], 100)

    def test_rounding_is_deterministic_and_fully_allocated(self):
        engines = self.engines(
            ("local/a", 1, "mlx_lm"), ("local/b", 1, "mlx_lm"), ("local/c", 1, "mlx_lm")
        )
        first = capacity.wiring_targets(engines, self.providers(), self.measured(10, 10))
        last = capacity.wiring_targets(
            dict(reversed(list(engines.items()))), self.providers(), self.measured(10, 10)
        )
        self.assertEqual(first, last, "dict order must not decide residency")
        # Three one-byte models cannot use ten bytes: every worker is at its cap and the surplus
        # stays pageable rather than becoming permission to pin a growing cache.
        self.assertEqual([item["wired_bytes"] for item in first.values()], [1, 1, 1])

    def test_unavailable_measurements_disable_wiring_rather_than_inventing_it(self):
        engines = self.engines(("local/a", 1000, "mlx_lm"))
        for measured in (self.measured(None, None), self.measured(0, 0), {}):
            with self.subTest(measured=measured):
                wiring = capacity.wiring_targets(engines, self.providers(), measured)
                self.assertEqual(wiring["local/a"]["wired_bytes"], 0)
                self.assertIn("unavailable", wiring["local/a"]["reason"])

    def test_refuse_policy_stops_the_launch_instead_of_guessing(self):
        providers = self.providers(
            {
                "strategy": "bounded_wiring",
                "target": "min_available_and_recommended",
                "distribution": "verified_weight_bytes",
                "unknown_measurements": "refuse",
            }
        )
        with self.assertRaisesRegex(capacity.CapacityError, "unknown_measurements"):
            capacity.wiring_targets(
                self.engines(("local/a", 1000, "mlx_lm")), providers, self.measured(None, None)
            )

    def test_an_uncontrolled_peer_zeroes_everyone_else(self):
        # llama.cpp decides its own Metal residency inside the library, so an aggregate target that
        # included it would be a bound the harness cannot actually enforce.
        engines = self.engines(("local/a", 1000, "mlx_lm"), ("local/b", 1000, "llama_cpp"))
        wiring = capacity.wiring_targets(engines, self.providers(), self.measured(9000, 9000))
        self.assertEqual(wiring["local/a"]["wired_bytes"], 0)
        self.assertTrue(wiring["local/a"]["controllable"])
        self.assertIn("another selected engine", wiring["local/a"]["reason"])
        self.assertFalse(wiring["local/b"]["controllable"])

    def test_missing_tensor_bytes_falls_back_to_unwired(self):
        engines = self.engines(("local/a", None, "mlx_lm"))
        wiring = capacity.wiring_targets(engines, self.providers(), self.measured(9000, 9000))
        self.assertEqual(wiring["local/a"]["wired_bytes"], 0)
        self.assertIn("tensor bytes", wiring["local/a"]["reason"])

    def test_conflicting_provider_policies_are_refused(self):
        providers = self.providers()
        providers["other"] = {
            "execution": {"kind": "local", "memory": {"strategy": "uncontrolled"}},
            "capacity": {"strategy": "native_context", "sequence_slots": 1},
        }
        engines = {
            "local/a": {"engine_kind": "mlx_lm", "tensor_bytes": 10, "sequences": 1},
            "other/b": {"engine_kind": "mlx_lm", "tensor_bytes": 10, "sequences": 1},
        }
        with self.assertRaisesRegex(capacity.CapacityError, "different"):
            capacity.wiring_targets(engines, providers, self.measured(100, 100))

    def test_wiring_cannot_resize_a_window_or_a_slot_count(self):
        # The invariant the whole design rests on: every memory measurement resolves the same plan.
        from tests.test_capacity_policy import CapacityFixture

        fixture = CapacityFixture()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        selection = {"primary": fixture.local, "subagent": fixture.local}
        observed = []
        for available in (1, 100 * capacity.GIB, 0):
            with (
                mock.patch.object(capacity.local_llm, "is_ready", return_value=True),
                mock.patch.object(capacity.local_llm, "engine_binary", return_value=Path("f")),
                mock.patch.object(
                    capacity.local_llm, "primary_weights_path",
                    return_value=fixture.root.resolve() / "fixture.gguf",
                ),
                mock.patch.object(
                    type(capacity.adapter_for("llama_cpp")), "identity",
                    return_value=EngineIdentity(
                        kind="llama_cpp", build_info="fixture abcdef0",
                        source_revision="0" * 40, version="b1", build_commit="abcdef0",
                    ),
                ),
                mock.patch.object(
                    type(capacity.adapter_for("llama_cpp")), "validate_artifact",
                    return_value=Artifact(
                        format="gguf", variant="Q4",
                        primary=fixture.root.resolve() / "fixture.gguf",
                        paths=(), tensor_bytes=1 << 30, total_bytes=1 << 30,
                    ),
                ),
                mock.patch.object(capacity.platform, "system", return_value="Darwin"),
                mock.patch.object(capacity.platform, "machine", return_value="arm64"),
                mock.patch.object(
                    capacity.host_caps, "probe",
                    return_value={"memory_bytes": 8, "available_memory_bytes": available},
                ),
            ):
                plan = capacity.prepare(
                    fixture.root, fixture.root, fixture.providers, fixture.models, selection, 512
                )
                engine = plan["engines"][fixture.local]
                # The fingerprint covers the launch snapshot id, which is random per launch by
                # design; what must not move with available RAM is the capacity itself.
                observed.append(
                    (engine["context_tokens"], engine["sequences"], engine["total_context_tokens"])
                )
        self.assertEqual(len(set(observed)), 1, f"memory changed the plan: {observed}")
