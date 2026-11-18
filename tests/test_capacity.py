"""Native-context resolution does not admit or resize models using physical memory."""

import ast
import contextlib
import copy
import json
import re
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
# ``tools/`` holds no package; capacity adds it to sys.path itself, and the adapter types need the
# same treatment before they can be imported here.
if str(ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(ROOT / "tools"))

from tools import capacity  # noqa: E402
from tools.local_engines.base import (  # noqa: E402
    Artifact,
    EngineAdapterError,
    EngineIdentity,
)

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
                # Faithful to what a version-3 parse actually emits: with named profiles the
                # singular ``engine``/``profile``/``default_profile`` keys are None, because a
                # provider offering several engines has no default to fall back to. Declaring a
                # populated ``profile`` here is what let launch validation pass while the shipped
                # provider failed it.
                "execution": {
                    "kind": "local",
                    "engine": None,
                    "profile": None,
                    "profiles": {
                        "default": {"engine": "llama_cpp", "settings": PROFILE},
                    },
                    "default_profile": None,
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


class MemoryPolicyTests(unittest.TestCase):
    """One device is bounded by one declaration.

    ``_memory_policy`` reads the block off every selected engine's provider and requires agreement:
    a mixed pair would mean two aggregate wiring targets for the same memory, and whichever one
    won, the other engine would be wired by a rule nobody chose. Refusing is the whole behaviour,
    and none of it was executed by any test before this class.
    """

    PROVIDERS = {
        "local": {"execution": {"memory": {"strategy": "bounded_wiring", "target": "none"}}},
        "other": {"execution": {"memory": {"strategy": "uncontrolled", "target": "none"}}},
    }

    def test_engines_sharing_one_declaration_get_it(self):
        engines = {"local/a": {}, "local/b": {}}
        self.assertEqual(
            capacity._memory_policy(engines, self.PROVIDERS),
            self.PROVIDERS["local"]["execution"]["memory"],
        )

    def test_two_different_declarations_are_refused(self):
        with self.assertRaisesRegex(
            capacity.CapacityError, "cannot be bounded by two aggregate targets"
        ):
            capacity._memory_policy({"local/a": {}, "other/b": {}}, self.PROVIDERS)

    def test_a_provider_naming_no_memory_block_declares_an_empty_one(self):
        # Absent means "wire nothing deliberately", not "this provider is missing": the first is a
        # choice the manifest made, the second would be a loader bug surfacing as a capacity error.
        providers = {"local": {"execution": {}}, "other": {"execution": {}}}
        self.assertEqual(capacity._memory_policy({"local/a": {}, "other/b": {}}, providers), {})

    def test_no_selected_engine_is_not_an_error(self):
        self.assertEqual(capacity._memory_policy({}, {}), {})


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


class ShippedDefinitionLaunchTests(unittest.TestCase):
    """Launch validation against the definitions that actually ship.

    Every other case in this file resolves against a hand-written provider dictionary, and that is
    how a schema migration shipped broken: the fixture declared a populated singular ``profile``
    while a version-3 provider emits ``None`` for it, so validation compared ``None`` against the
    resolver's allocation and the shipped models were rejected at serve time while the suite stayed
    green. Only the real tree can prove the reader and the writer agree.
    """

    @staticmethod
    def shipped() -> list[dict]:
        from tools import definitions

        _, models = definitions.load_definitions(ROOT)
        return [model for model in models if model["provider"] == "local_mps"]

    @staticmethod
    def execution(model: dict) -> dict:
        from tools import definitions

        providers, _ = definitions.load_definitions(ROOT)
        return providers[model["provider"]]["execution"]

    @contextlib.contextmanager
    def prepared(self, model: dict):
        """Resolve one shipped model and validate against it under the same synthetic runtime.

        The patches have to stay live while ``validate_launch`` runs, because it re-reads the
        binary identity and the weights path: exiting first would validate against a host that has
        neither the pinned binary nor the artifact installed.
        """
        import tempfile

        from tools import definitions

        providers, model_list = definitions.load_definitions(ROOT)
        models = {entry["id"]: entry for entry in model_list}
        identity = EngineIdentity(
            kind="llama_cpp",
            build_info="fixture b11846 abcdef0",
            source_revision="0" * 40,
            version="b11846",
            build_commit="abcdef0",
        )
        adapter = type(capacity.adapter_for("llama_cpp"))
        local_llm = capacity.local_llm
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binary = root / "fixture.gguf"
            binary.write_bytes(b"x" * 1024)
            artifact = Artifact(
                format="gguf",
                variant="Q4",
                primary=binary,
                paths=(binary,),
                tensor_bytes=1 << 30,
                total_bytes=1 << 30,
            )
            stack = contextlib.ExitStack()
            for target, attribute, value in (
                (local_llm, "is_ready", True),
                (local_llm, "engine_binary", Path("fixture")),
                (
                    local_llm,
                    "engine_plan",
                    {"version": "b11846", "commit": "0" * 40},
                ),
                (local_llm, "primary_weights_path", binary),
                (adapter, "identity", identity),
                (adapter, "validate_artifact", artifact),
                (
                    capacity,
                    "engine_identity",
                    {
                        "build_info": "fixture b11846 abcdef0",
                        "build_commit": "abcdef0",
                        "source_commit": "0" * 40,
                    },
                ),
                (capacity.platform, "system", "Darwin"),
                (capacity.platform, "machine", "arm64"),
            ):
                stack.enter_context(mock.patch.object(target, attribute, return_value=value))
            stack.enter_context(
                mock.patch.object(
                    capacity.host_caps,
                    "probe",
                    return_value={
                        "memory_bytes": 128 * capacity.GIB,
                        "available_memory_bytes": 100 * capacity.GIB,
                    },
                )
            )
            with stack:
                plan = capacity.prepare(
                    ROOT,
                    root / "runtime",
                    providers,
                    models,
                    {"primary": model["id"], "subagent": model["id"]},
                    512,
                )
                bound = models[model["id"]]
                engine = plan["engines"][model["id"]]
                yield bound, engine

    def test_every_shipped_local_model_survives_launch_validation(self):
        shipped = self.shipped()
        self.assertTrue(shipped, "no local_mps models are defined to validate")
        for model in shipped:
            with self.subTest(model=model["id"]), self.prepared(model) as (bound, engine):
                self.assertIsNone(
                    # The provider offers more than one engine, so it declares no default; the
                    # profile a model is served with is bound onto the model, and validation must
                    # read it from there.
                    self.execution(bound)["profile"],
                    f"{model['id']}: expected a provider with no default profile",
                )
                self.assertEqual(bound["serving_profile"], "llama_cpp_q4")
                capacity.local_llm.validate_launch(bound, engine)

    def test_launch_validation_still_catches_drift(self):
        model = self.shipped()[0]
        with self.prepared(model) as (bound, engine):
            for changes in (
                {"context_tokens": 8192},
                {"sequences": 2},
                {"memory_management": "bounded"},
                {"engine_kind": "mlx_lm"},
                {"allocation": dict(engine["allocation"], prefill_chunk_tokens=99)},
                {"serving_profile_id": "0" * 64},
            ):
                with self.subTest(changes=sorted(changes)):
                    with self.assertRaises(capacity.local_llm.EngineError):
                        capacity.local_llm.validate_launch(bound, dict(engine, **changes))


class ResolvedRecordShapeTests(unittest.TestCase):
    """The shared test fixture must cover every field the resolver actually writes.

    Four separate defects hid behind a hand-written engine dict that omitted ``engine_kind`` or
    the nested ``wiring`` block: the launcher, the readiness validator, the smoke tool and the
    proxy each read a field the fixture had never heard of, and the suite passed. This compares
    the fixture against a real resolution, so the next mandatory field opens the suite instead of
    hiding a bug.
    """

    def test_shared_fixture_matches_a_real_resolution(self):
        import tempfile
        from unittest import mock

        from tests.helpers import RESOLVED_ENGINE_KEYS, resolved_engine_record
        from tools import definitions

        providers, model_list = definitions.load_definitions(ROOT)
        models = {entry["id"]: entry for entry in model_list}
        model = next(m for m in model_list if m["provider"] == "local_mps")
        identity = EngineIdentity(
            kind="llama_cpp",
            build_info="fixture b11846 abcdef0",
            source_revision="0" * 40,
            version="b11846",
            build_commit="abcdef0",
        )
        local_llm = capacity.local_llm
        adapter = type(capacity.adapter_for("llama_cpp"))
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / "fixture.gguf"
            binary.write_bytes(b"x")
            artifact = Artifact(
                format="gguf",
                variant="Q4",
                primary=binary,
                paths=(binary,),
                tensor_bytes=1 << 30,
                total_bytes=1 << 30,
            )
            # One ExitStack: `with stack, (a, b):` parses as two items where the second is a
            # plain tuple, which raises TypeError rather than entering anything, and it only
            # showed up in a full run because the isolated invocation failed earlier.
            with contextlib.ExitStack() as stack:
                stack.enter_context(
                    mock.patch.multiple(
                        local_llm,
                        is_ready=lambda _m: True,
                        engine_binary=lambda **_k: Path("fixture"),
                        engine_plan=lambda **_k: {"version": "b11846", "commit": "0" * 40},
                        primary_weights_path=lambda _m: binary,
                    )
                )
                for target, attribute, value in (
                    (adapter, "identity", identity),
                    (adapter, "validate_artifact", artifact),
                    (capacity.platform, "system", "Darwin"),
                    (capacity.platform, "machine", "arm64"),
                ):
                    stack.enter_context(mock.patch.object(target, attribute, return_value=value))
                stack.enter_context(
                    mock.patch.object(
                        capacity.host_caps,
                        "probe",
                        return_value={
                            "memory_bytes": 128 * capacity.GIB,
                            "available_memory_bytes": 100 * capacity.GIB,
                        },
                    )
                )
                plan = capacity.prepare(
                    ROOT,
                    Path(directory) / "runtime",
                    providers,
                    models,
                    {"primary": model["id"], "subagent": model["id"]},
                    512,
                )
            produced = set(plan["engines"][model["id"]])
        self.assertTrue(
            produced, "the resolver produced no engine record to compare against"
        )
        # Two directions matter: the fixture must not omit a field production writes, and it
        # must not
        # invent one, or a consumer can read a field no real plan carries and nothing notices.
        self.assertEqual(
            RESOLVED_ENGINE_KEYS,
            frozenset(produced),
            "resolver fields and RESOLVED_ENGINE_KEYS drifted; update the shared fixture",
        )
        self.assertEqual(
            set(resolved_engine_record()),
            set(produced),
            "shared fixture keys differ from a real record",
        )


class DeviceObservationTests(unittest.TestCase):
    """S15e: the device recommendation arrives through the engine adapter, not an env var.

    ``LOCAL_MPS_METAL_PROBE`` named a script nothing in the launcher ever set, so the second input
    to ``min_available_and_recommended`` was absent on every real launch. These assert the
    replacement: the resolver asks the adapter, and what the adapter says is what the wiring
    decision is given.
    """

    def collect(self, *, per_engine, available=100 * capacity.GIB):
        import tempfile

        from local_engines.base import Artifact, EngineIdentity

        from tools import definitions

        providers, model_list = definitions.load_definitions(ROOT)
        models = {entry["id"]: entry for entry in model_list}
        local = [m for m in model_list if m["provider"] == "local_mps"]
        self.assertTrue(local, "no shipped local model to resolve")
        adapter = type(capacity.adapter_for("llama_cpp"))
        local_llm = capacity.local_llm
        identity = EngineIdentity(
            kind="llama_cpp", build_info="fixture b11846 abcdef0",
            source_revision="0" * 40, version="b11846", build_commit="abcdef0",
        )
        per_engine = list(per_engine)
        calls = iter(per_engine)
        seen: dict[str, dict] = {}

        # Bound *before* patching: calling capacity.wiring_targets from inside the replacement
        # re-enters the spy forever, which is the one way this helper could fail noisily rather
        # than quietly, so it is worth naming.
        original = capacity.wiring_targets

        def spy(engines, provider_map, measured):
            seen["measured"] = dict(measured)
            return original(engines, provider_map, measured)

        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / "fixture.gguf"
            binary.write_bytes(b"x")
            artifact = Artifact(
                format="gguf", variant="Q4", primary=binary, paths=(binary,),
                tensor_bytes=1 << 30, total_bytes=1 << 30,
            )
            # One engine per observation supplied, so the disagreeing case really has two.
            wanted = local[: len(per_engine)]
            self.assertEqual(
                len(wanted), len(per_engine), "not enough shipped local models for this case"
            )
            selection = {"primary": wanted[0]["id"]}
            for extra in wanted[1:]:
                # A second lane key, so two distinct models really are resolved: one engine per
                # observation is what the disagreeing case is about.
                selection["subagent"] = extra["id"]
            with (
                mock.patch.object(local_llm, "is_ready", return_value=True),
                mock.patch.object(local_llm, "engine_binary", return_value=Path("fixture")),
                mock.patch.object(
                    local_llm, "engine_plan",
                    return_value={"version": "b11846", "commit": "0" * 40},
                ),
                mock.patch.object(local_llm, "primary_weights_path", return_value=binary),
                mock.patch.object(adapter, "identity", return_value=identity),
                mock.patch.object(adapter, "validate_artifact", return_value=artifact),
                mock.patch.object(adapter, "device_observations",
                                  side_effect=lambda _r: next(calls)),
                mock.patch.object(capacity.platform, "system", return_value="Darwin"),
                mock.patch.object(capacity.platform, "machine", return_value="arm64"),
                mock.patch.object(
                    capacity.host_caps, "probe",
                    return_value={"memory_bytes": 128 * capacity.GIB,
                                  "available_memory_bytes": available},
                ),
                mock.patch.object(capacity, "wiring_targets", side_effect=spy),
            ):
                plan = capacity.prepare(
                    ROOT, Path(directory) / "runtime", providers, models, selection, 512
                )
        return plan, seen.get("measured", {})

    def test_a_reported_recommendation_reaches_the_wiring_decision(self):
        eight = 8 * capacity.GIB
        _plan, measured = self.collect(per_engine=[{"recommended_max_working_set_size": eight}])
        self.assertEqual(
            measured["metal"]["recommended_working_set_bytes"], eight, measured
        )
        # Provenance is recorded, not implied: a reader can see which engine said so.
        self.assertTrue(
            measured["metal"]["source"].startswith("engine:"), measured["metal"]
        )

    def test_an_engine_that_cannot_say_leaves_the_recommendation_absent(self):
        # Absent, not zero: the difference is what the unknown_measurements policy keys on.
        _plan, measured = self.collect(per_engine=[{}])
        self.assertNotIn(
            "recommended_working_set_bytes",
            (measured.get("metal") or {}),
            measured,
        )

    def test_two_engines_that_disagree_are_refused_rather_than_arbitrated(self):
        gib = capacity.GIB
        per_engine = [
            {"recommended_max_working_set_size": 8 * gib},
            {"recommended_max_working_set_size": 16 * gib},
        ]
        with self.assertRaisesRegex(capacity.CapacityError, "different device working sets"):
            self.collect(per_engine=per_engine)


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

    def test_a_single_missing_measurement_unwires_rather_than_minning_the_remainder(self):
        """F9f: `unknown_measurements = "unwired"` was consulted only when *all* inputs were absent.

        With one number missing, `min()` over the survivor silently turned
        `target = "min_available_and_recommended"` into whichever input happened to arrive, so a
        host that could not report a Metal recommendation was asked to pin its entire available
        memory — which is reachable on every real launch, because nothing supplies the probe.
        """
        engines = self.engines(("local/a", 60 * capacity.GIB, "mlx_lm"))
        providers = self.providers()
        for label, available, recommended in (
            ("available reported as zero", 0, 100 * capacity.GIB),
            ("recommendation absent", 100 * capacity.GIB, None),
            ("recommendation reported as zero", 100 * capacity.GIB, 0),
        ):
            with self.subTest(case=label):
                decision = capacity.wiring_targets(
                    dict(engines), providers, self.measured(available, recommended)
                )["local/a"]
                self.assertEqual(decision["wired_bytes"], 0, decision)
                self.assertIn("measurement unavailable", decision["reason"])

    def test_a_target_naming_one_measurement_accepts_that_one_alone(self):
        # The policy is per *named* input, not a blanket demand for every measurement the module
        # knows how to read; a provider that promises available-memory-only must be honourable.
        engines = self.engines(("local/a", 60 * capacity.GIB, "mlx_lm"))
        providers = self.providers(
            {
                "strategy": "bounded_wiring",
                "target": "available_memory",
                "distribution": "verified_weight_bytes",
                "unknown_measurements": "unwired",
            }
        )
        decision = capacity.wiring_targets(
            dict(engines), providers, self.measured(100 * capacity.GIB, None)
        )["local/a"]
        self.assertEqual(decision["wired_bytes"], 60 * capacity.GIB, decision)

    def test_refuse_still_refuses_for_one_missing_measurement(self):
        engines = self.engines(("local/a", 60 * capacity.GIB, "mlx_lm"))
        providers = self.providers(
            {
                "strategy": "bounded_wiring",
                "target": "min_available_and_recommended",
                "distribution": "verified_weight_bytes",
                "unknown_measurements": "refuse",
            }
        )
        with self.assertRaisesRegex(capacity.CapacityError, "refuse"):
            capacity.wiring_targets(
                dict(engines), providers, self.measured(100 * capacity.GIB, None)
            )

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


class AdapterRegistrationTests(unittest.TestCase):
    """An adapter that exists on disk has to be one the registry knows."""

    PACKAGE = ROOT / "tools" / "local_engines"
    KIND = re.compile(r'^ {4}kind = "([a-z_]+)"', re.MULTILINE)

    def declared(self) -> dict[str, str]:
        """Each adapter module in the package, keyed by the kind its own class declares."""
        found = {}
        for path in sorted(self.PACKAGE.glob("*.py")):
            text = path.read_text(encoding="utf-8")
            if "def register(" in text:
                for kind in self.KIND.findall(text):
                    found[kind] = path.name
        return found

    def test_the_discovery_probe_sees_the_adapters_that_exist(self):
        # Without this, a change to the marker above would leave the next test asserting nothing.
        declared = self.declared()
        self.assertGreaterEqual(len(declared), 2, declared)

    def test_every_adapter_module_registers_its_kind(self):
        # capacity.py imports each adapter inside `except ImportError: continue`. That is right for
        # an optional dependency and wrong for a broken one: an adapter that fails to import on a
        # host which does support it simply vanishes, and the only symptom is a refusal that reads
        # like the operator chose an engine this platform cannot run.
        for kind, module in self.declared().items():
            with self.subTest(kind=kind, module=module):
                self.assertIn(kind, capacity.engine_kinds())

    def test_no_tools_module_binds_a_sibling_by_its_flat_name(self):
        # A module writing `import local_llm` binds the flat copy even when the package was
        # loaded as tools.local_engines, which drags a second copy of the engine module into the
        # process -- its own EngineError, its own module state. A caller or a test then patches one
        # copy while the adapter reads the other, and a fixture impersonating a harness root read
        # the real repository through an unpatched ROOT while still reporting green. Checked
        # against the source rather than by calling sibling_module(): a test of the helper would
        # keep passing while a call site went back to the flat name. Scope is the whole
        # tools/ tree, so a new sibling import cannot arrive unguarded at the far end.
        siblings = {
            path.stem for path in (ROOT / "tools").glob("*.py")
        } | {"local_engines"}
        offenders = []
        for path in sorted((ROOT / "tools").rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            guarded = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.If) and "__package__" in ast.unparse(node.test):
                    for inner in ast.walk(node):
                        guarded.add(id(inner))
            for node in ast.walk(tree):
                names = ([a.name for a in node.names] if isinstance(node, ast.Import)
                         else [node.module] if isinstance(node, ast.ImportFrom) and not node.level
                         and node.module else [])
                for name in names:
                    if name.split(".")[0] in siblings and id(node) not in guarded:
                        offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}: import {name}")
        self.assertEqual(offenders, [], "\n".join(offenders))

    def test_a_registered_adapter_raises_the_error_a_caller_can_catch(self):
        # The adapters bind EngineAdapterError from their own package. When those imports mixed a
        # relative and a flat spelling, importing `tools.capacity` loaded `base` twice, the adapters
        # raised the other copy, and catching the class named above let the error escape with two
        # classes printing an identical name.
        for kind in capacity.engine_kinds():
            with self.subTest(kind=kind):
                adapter = capacity.adapter_for(kind)
                module = __import__(type(adapter).__module__, fromlist=["EngineAdapterError"])
                self.assertIs(module.EngineAdapterError, EngineAdapterError)

    def test_an_unregistered_kind_is_refused_naming_the_registered_ones(self):
        with self.assertRaises(EngineAdapterError) as caught:
            capacity.adapter_for("no-such-engine")
        self.assertIn("no-such-engine", str(caught.exception))
        self.assertIn("registered:", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
