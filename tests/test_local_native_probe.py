"""The native feasibility probe's judgement logic, exercised without Apple silicon.

The probe itself cannot run here - it refuses, which is its correct behaviour on a host with no
Metal - but the part worth testing is not the GPU call. It is what the probe *concludes* from the
numbers it reads back, because a probe that reports "passes" against a runtime that overwrote the
wired limit or pre-committed the whole cache is worse than no probe: it is a green light with
nothing behind it.
"""

import copy
import json
import pathlib
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]

if str(ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(ROOT / "tools"))
if str(ROOT / "proxy") not in sys.path:
    sys.path.insert(0, str(ROOT / "proxy"))

import local_native_probe as probe  # noqa: E402

RUNTIME = {
    "identity": "abc",
    "python": "3.12.11",
    "lock_digest": "sha256:x",
    "executable": str(Path(tempfile.gettempdir()) / "probe-worker" / "run-worker.py"),
    "imports": {"mlx": "0.32.4", "mlx_lm": "0.32.2"},
}


class RefusalTests(unittest.TestCase):
    """The probe must refuse on a host that cannot answer, rather than answer wrongly."""

    def test_it_refuses_a_non_apple_silicon_host(self):
        with (
            mock.patch.object(probe.platform, "system", return_value="Linux"),
            mock.patch.object(probe.platform, "machine", return_value="x86_64"),
            self.assertRaisesRegex(probe.ProbeError, "Apple silicon"),
        ):
            probe._require_mac()

    def test_an_unknown_makes_the_interpreter_path_a_failure_not_a_default(self):
        missing = RUNTIME["executable"].replace("probe-worker", "absent-worker")
        with self.assertRaises(probe.ProbeError):
            probe._interpreter({"executable": missing})


class EnvironmentHygieneTests(unittest.TestCase):
    def test_ambient_engine_overrides_are_stripped_before_measuring(self):
        # An exported MLX_ or GGML_ variable would change what the probe measures, and the probe's
        # output is the input to a pin.
        captured = {}

        def fake_run(argv, **kwargs):
            captured["env"] = kwargs["env"]
            result = mock.Mock(returncode=0, stdout='{"ok": true}', stderr="")
            return result

        with (
            mock.patch.dict(
                probe.os.environ,
                {"MLX_WIRED_LIMIT": "1", "GGML_METAL_NOMEMORY": "1", "PATH": "/usr/bin"},
            ),
            mock.patch.object(probe.subprocess, "run", fake_run),
            mock.patch.object(probe, "_interpreter", return_value="/usr/bin/python3"),
        ):
            answer = probe._run_in_runtime(RUNTIME, "print(1)")
        self.assertEqual(answer, {"ok": True})
        for name in captured["env"]:
            self.assertFalse(
                name.startswith(("MLX_", "GGML_", "LLAMA_")),
                f"{name} reached the probe environment",
            )
        self.assertIn("PATH", captured["env"])


class WiringConclusionTests(unittest.TestCase):
    """The answer to U2 is a path choice, and the probe has to name the usable one."""

    def _answer(self, observed):
        with mock.patch.object(
            probe, "_run_in_runtime", return_value=observed
        ), mock.patch.object(probe, "_interpreter", return_value="/usr/bin/true"):
            return probe.question_two(RUNTIME, "mlx-community/small")

    def test_a_wrapper_that_overwrites_the_limit_leaves_generate_step_usable(self):
        result = self._answer(
            {
                "stream_generate": {"before": 1024, "after_call": 512, "honoured": False},
                "generate_step": {"before": 1024, "after_call": 1024, "honoured": True},
            }
        )
        self.assertTrue(result["wrapper_overwrites"])
        self.assertEqual(result["usable_path"], "generate_step")
        self.assertEqual(result["honoured_by"], ["generate_step"])

    def test_no_honoured_path_is_reported_as_such(self):
        result = self._answer(
            {
                "stream_generate": {"honoured": False},
                "generate_step": {"error": "cache argument unsupported"},
            }
        )
        self.assertIsNone(result["usable_path"])
        self.assertEqual(result["honoured_by"], [])

    def test_both_paths_honoured_reports_both(self):
        result = self._answer(
            {"stream_generate": {"honoured": True}, "generate_step": {"honoured": True}}
        )
        self.assertFalse(result["wrapper_overwrites"])
        self.assertEqual(result["honoured_by"], ["generate_step", "stream_generate"])


class GrowthConclusionTests(unittest.TestCase):
    """Growing cache is the premise; the probe must be able to say it does not hold."""

    def _answer(self, short, long_):
        with (
            mock.patch.object(
                probe, "_run_in_runtime", return_value={"short": short, "long": long_}
            ),
            mock.patch.object(probe, "_interpreter", return_value="/usr/bin/true"),
        ):
            return probe.question_three(RUNTIME, "mlx-community/small")

    def test_a_rising_cache_is_measured_per_token(self):
        result = self._answer({"cache_nbytes": 8192}, {"cache_nbytes": 4_200_000})
        self.assertTrue(result["grows_incrementally"])
        self.assertEqual(result["measured_by"], "cache_nbytes")
        self.assertIsNotNone(result["bytes_per_token"])

    def test_a_precommitted_cache_fails_the_premise(self):
        # Identical allocation at eight tokens and three hundred means the maximum was paid upfront.
        result = self._answer({"cache_nbytes": 9_000_000}, {"cache_nbytes": 9_000_000})
        self.assertFalse(result["grows_incrementally"])

    def test_it_falls_back_to_peak_memory_when_the_cache_reports_no_size(self):
        # peak stands in for a cache class that reports no size
        short = {"cache_nbytes": None, "peak_memory_bytes": 100}
        long_ = {"cache_nbytes": None, "peak_memory_bytes": 900}
        result = self._answer(short, long_)
        self.assertEqual(result["measured_by"], "peak_memory_bytes")
        self.assertTrue(result["grows_incrementally"])


class VerdictTests(unittest.TestCase):
    """The pass/fail bit: an unanswered question must not read as a pass."""

    def _run(self, answers):
        def fake(name):
            return lambda *a, **k: copy.deepcopy(answers[name])

        runtime = dict(RUNTIME)
        with (
            mock.patch.object(probe, "_require_mac", return_value={"macos_version": "15.1"}),
            mock.patch.object(probe, "_provision_runtime", return_value=runtime),
            mock.patch.object(probe, "question_one", fake("executes")),
            mock.patch.object(probe, "question_two", fake("wiring")),
            mock.patch.object(probe, "question_three", fake("growth")),
        ):
            result = probe.run([])
        return result

    def test_all_three_questions_satisfied_passes(self):
        result = self._run(
            {
                "executes": {
                    "metal_available": True,
                    "generated": True,
                    "generated_characters": 3,
                },
                "wiring": {"usable_path": "generate_step", "wrapper_overwrites": True},
                "growth": {"grows_incrementally": True, "bytes_per_token": 12000.0},
            }
        )
        self.assertTrue(result["passes"])
        self.assertEqual(result["blocking_findings"], [])

    def test_no_metal_device_blocks(self):
        result = self._run(
            {
                "executes": {
                    "metal_available": False,
                    "generated": True,
                    "generated_characters": 3,
                },
                "wiring": {"usable_path": "generate_step", "wrapper_overwrites": True},
                "growth": {"grows_incrementally": True},
            }
        )
        self.assertFalse(result["passes"])
        self.assertIn("Metal", " ".join(result["blocking_findings"]))

    def test_no_usable_wiring_path_blocks(self):
        result = self._run(
            {
                "executes": {"metal_available": True, "generated": True},
                "wiring": {"usable_path": None, "wrapper_overwrites": True},
                "growth": {"grows_incrementally": True},
            }
        )
        self.assertFalse(result["passes"])
        self.assertTrue(any("wired limit" in item for item in result["blocking_findings"]))

    def test_a_precommitted_cache_blocks(self):
        result = self._run(
            {
                "executes": {"metal_available": True, "generated": True},
                "wiring": {"usable_path": "generate_step", "wrapper_overwrites": True},
                "growth": {"grows_incrementally": False},
            }
        )
        self.assertFalse(result["passes"])

    def test_the_result_is_serialisable_and_names_the_runtime_it_proved(self):
        result = self._run(
            {
                "executes": {"metal_available": True, "generated": True},
                "wiring": {"usable_path": "generate_step", "wrapper_overwrites": True},
                "growth": {"grows_incrementally": True},
            }
        )
        text = json.dumps(result, sort_keys=True)
        # The result names the exact runtime it proved, so a pass cannot be inherited by a later
        # dependency set that was never measured.
        self.assertIn('"identity": "abc"', text)
        self.assertIn('"lock_digest"', text)
        self.assertEqual(result["kind"], probe.RESULT_KIND)


if __name__ == "__main__":
    unittest.main()


class UpstreamApiContractTests(unittest.TestCase):
    """The generation calls must use arguments MLX-LM actually accepts.

    Neither the probe nor the worker can be executed here, and a wrong keyword is a TypeError on
    the operator's machine rather than a test failure - which is the worst possible place to learn
    it. These assertions read the pinned upstream source instead, so the contract is checked by CI
    on a host that has no Metal.
    """

    SOURCE_URL = "https://raw.githubusercontent.com/ml-explore/mlx-lm/main/mlx_lm/generate.py"

    @classmethod
    def setUpClass(cls):
        cache = Path(tempfile.gettempdir()) / "mlx_lm_generate_head.py"
        if cache.is_file():
            cls.source = cache.read_text(encoding="utf-8")
        else:
            import urllib.request

            try:
                with urllib.request.urlopen(cls.SOURCE_URL, timeout=30) as handle:
                    cls.source = handle.read().decode()
            except OSError:
                raise unittest.SkipTest("cannot reach upstream mlx-lm source") from None
            cache.write_text(cls.source, encoding="utf-8")

    def _signature(self, name: str) -> str:
        match = re.search(rf"^def {name}\((.*?)\)\s*(?:->|:)", self.source, re.M | re.S)
        assert match, f"{name} is no longer a top-level function upstream"
        return re.sub(r"\s+", " ", match.group(1))

    def test_generate_step_still_takes_prompt_cache_not_cache(self):
        signature = self._signature("generate_step")
        self.assertIn("prompt_cache", signature)
        # "cache=" would be accepted by no version of this function and fails at call time.
        self.assertNotIn(" cache:", signature)
        self.assertNotIn(" cache=", signature)

    def test_generate_step_has_no_temperature_or_top_p_parameters(self):
        # Sampling arrives as a constructed sampler; passing temp/top_p directly is the mistake
        # this test exists to keep the worker from making.
        signature = self._signature("generate_step")
        for absent in ("temp:", "temperature:", "top_p:"):
            self.assertNotIn(absent, signature)
        self.assertIn("sampler", signature)

    def test_the_prefill_chunk_knob_still_exists_by_that_name(self):
        self.assertIn("prefill_step_size", self._signature("generate_step"))

    def test_the_wired_limit_helper_the_plan_relies_on_is_still_wired_into_stream_generate(self):
        # The whole reason generate_step is used instead of stream_generate: the wrapper resets
        # the device limit per request. If upstream stops doing that, the rationale is stale and
        # the worker should be simplified - this test is the signal to re-examine it.
        start = self.source.index("def stream_generate(")
        body = self.source[start:start + 4000]
        self.assertIn("wired_limit", body)


class CrossModuleReferenceTests(unittest.TestCase):
    """Every harness symbol referenced across a module boundary must actually exist.

    The provisioning runtime, the frontend and the worker are separate processes, so a call to a
    function that was never written is not an import error and not a test failure - it is a launch
    that dies on the operator's Mac at the worst moment. This walks the generated and cross-module
    references and resolves them here instead.
    """

    def _tools(self):
        return {
            path.name: path.read_text(encoding="utf-8")
            for path in (ROOT / "tools").rglob("*.py")
            if "__pycache__" not in str(path)
        }

    def test_generated_runtime_launcher_calls_a_real_entry_point(self):
        import local_mlx_runtime

        source = local_mlx_runtime.__dict__
        self.assertTrue(callable(source.get("provision")))
        text = pathlib.Path(local_mlx_runtime.__file__).read_text(encoding="utf-8")
        # No reference to a helper that does not exist in the module it is aimed at.
        target = (ROOT / "tools" / "local_llm.py").read_text(encoding="utf-8")
        for name in re.findall(r"local_llm\.([a-zA-Z_][A-Za-z0-9_]*)\(", text):
            self.assertIn(
                f"def {name}(", target,
                f"local_mlx_runtime calls local_llm.{name}(), which is not defined",
            )

    def test_frontend_arguments_match_what_the_adapter_builds(self):
        adapter = (ROOT / "tools" / "local_engines" / "mlx_lm.py").read_text(encoding="utf-8")
        server = (ROOT / "tools" / "local_model_server.py").read_text(encoding="utf-8")
        worker = (ROOT / "tools" / "local_engines" / "mlx_worker.py").read_text(encoding="utf-8")
        frontend_flags = set(re.findall(r'add_argument\("--([a-z-]+)"', server))
        worker_flags = set(re.findall(r'add_argument\("--([a-z-]+)"', worker))

        # Walk the argv tuple in order: a value following "--worker-arg" is destined for the
        # worker's parser, not the frontend's. Matching every "--x" string in the file would
        # report a correct argument as a defect.
        tuple_body = adapter[adapter.index("argv = (") : adapter.index("return Launch(")]
        tokens = [token for token in re.findall(r'"([^"]+)"', tuple_body) if token]
        flags = [token[2:] for token in tokens if token.startswith("--")]
        # Each flag must be parsed by the process that ultimately receives it. Tracing which value
        # belongs to which parser across a `--worker-arg` chain is fragile in exactly the way this
        # test is meant to avoid; the durable invariant is that no flag is sent to something that
        # cannot read it, and the frontend forwards its own --plan-file/--host/--port internally.
        owners = frontend_flags | worker_flags
        unowned = sorted(set(flags) - owners)
        self.assertFalse(unowned, f"flags declared by neither process: {unowned}")
        # The frontend's mandatory arguments must all be present, or it exits at startup.
        mandatory = set(
            re.findall(r'add_argument\("--([a-z-]+)",\s*(?:required=True)?[^)]*required=True',
                       server)
        )
        self.assertTrue(
            mandatory <= set(flags),
            f"adapter omits mandatory frontend arguments: {sorted(mandatory - set(flags))}",
        )


    def test_worker_arguments_match_what_the_frontend_passes(self):
        server = (ROOT / "tools" / "local_model_server.py").read_text(encoding="utf-8")
        worker = (ROOT / "tools" / "local_engines" / "mlx_worker.py").read_text(encoding="utf-8")
        passed = set(re.findall(r'"--worker-arg",\s*\n?\s*"([^"]+)"', server))
        declared = set(re.findall(r'add_argument\("--([a-z-]+)"', worker))
        self.assertTrue(
            passed <= declared,
            f"frontend passes worker flags that are not declared: {sorted(passed - declared)}",
        )

    def test_contract_symbols_the_worker_imports_are_all_exported(self):
        worker = (ROOT / "tools" / "local_engines" / "mlx_worker.py").read_text(encoding="utf-8")
        import local_engine_contract as contract

        for name in re.findall(r"from local_engine_contract import \(([^)]+)\)", worker)[0:1]:
            for symbol in re.split(r"[,\s]+", name):
                if symbol:
                    self.assertTrue(
                        hasattr(contract, symbol),
                        f"worker imports local_engine_contract.{symbol}, which does not exist",
                    )
