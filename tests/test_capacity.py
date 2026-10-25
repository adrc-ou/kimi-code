"""Native-context resolution does not admit or resize models using physical memory."""

import copy
import json
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
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


class AllocationTests(unittest.TestCase):
    def setUp(self):
        self.providers = {
            "local": {
                "execution": {"kind": "local", "profile": PROFILE},
                "capacity": {"strategy": "native_context", "sequence_slots": 1},
            }
        }
        self.models = [
            {"id": "local/a", "provider": "local", "native_tokens": 4096},
            {"id": "local/b", "provider": "local", "native_tokens": 8192},
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
