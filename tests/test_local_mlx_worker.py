"""The MLX worker's behaviour, executed rather than read.

Every case here runs the real ``mlx_worker.serve()`` against :mod:`tests.fakemlx`. That distinction
is the point: the previous round of this work shipped a worker whose pinned-API calls, prompt shape,
cache lifecycle and residency keys were all wrong, and the suite stayed green because the only tests
that touched the file matched it with a regex. Reading source text cannot see an argument in the
wrong order, a dictionary key at the wrong depth, or an attribute that does not exist.

Cases marked ``F<n>`` name the audit finding they exist to keep closed.
"""

from __future__ import annotations

import ast
import io
import json
import sys
import unittest
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
for _directory in (
    ROOT,
    ROOT / "tests",
    ROOT / "tools",
    ROOT / "tools" / "local_engines",
    ROOT / "proxy",
):
    if str(_directory) not in sys.path:
        sys.path.insert(0, str(_directory))

import fakemlx  # noqa: E402
import mlx_worker  # noqa: E402

WORKER = ROOT / "tools" / "local_engines" / "mlx_worker.py"
PROBE = ROOT / "tools" / "local_native_probe.py"

EOS = 99


def _reset_worker_import_cache() -> None:
    """Forget the ``mx``/``generate_step`` the worker memoised on its previous run."""
    mlx_worker._mx = None
    mlx_worker._generate_step = None


def plan_document(**overrides: Any) -> dict:
    """A resolved engine record shaped exactly like ``_write_plan_document``'s output.

    The residency key is nested on purpose: ``capacity.py::wiring_targets`` writes
    ``engine["wiring"]``, ``policy.py::_lane_table_lines`` reads it back correctly, and only the
    worker looked for a top-level
    ``wired_bytes`` — which does not exist, so it applied zero and its own fail-closed guard could
    never fire because both sides were consistently absent.
    """
    document = {
        "model_alias": "fixture-27b",
        "context_tokens": 4096,
        "sequences": 1,
        "total_context_tokens": 4096,
        "fingerprint": "f" * 64,
        "snapshot_id": "s" * 32,
        "serving_profile_id": "p" * 64,
        "runtime_identity": "r" * 64,
        "protocol_version": 1,
        "artifact_format": "mlx",
        "cache_allocation": "growing",
        "tokenizer_identity": None,
        "template_identity": None,
        "wiring": {
            "wired_bytes": 8 * 1024**3,
            "reason": "min(available, recommended)",
            "controllable": True,
        },
        "allocation": {
            "prefill_chunk_tokens": 2048,
            "cache_type_k": "f16",
            "cache_type_v": "f16",
            "allocator_cache_bytes": 0,
            "prefix_cache": "continuation_only",
        },
        "sampling": {"temperature": 0.0, "top_p": 0.0},
        "model_dir": "/fixture/artifact",
    }
    document.update(overrides)
    return document


class Worker:
    """One worker process lifetime: scripted stdin documents in, parsed stdout documents out."""

    def __init__(self, expected: dict, **fake: Any) -> None:
        self.expected = expected
        self.recording = fakemlx.install(**fake)
        self.requests: list[dict | str] = []

    def request(
        self, action: str, payload: object = None, sequence: int | None = None
    ) -> Worker:
        self.requests.append(
            {
                "sequence": len(self.requests) + 1 if sequence is None else sequence,
                "action": action,
                "payload": payload or {},
            },
        )
        return self

    def generate(self, messages: list | None = None, **payload: Any) -> Worker:
        return self.request(
            "generate", {"messages": messages or [{"role": "user", "content": "x"}], **payload}
        )

    def raw(self, line: str) -> Worker:
        """One literal stdin line, for a request this driver would never itself construct."""
        self.requests.append(line)
        return self

    def run(self) -> Worker:
        joined = "".join(
            (entry if isinstance(entry, str) else json.dumps(entry)) + "\n"
            for entry in self.requests
        )
        stdin = io.StringIO(joined)
        stdout = io.StringIO()
        self.exit_code = mlx_worker.serve(stdin, stdout, self.expected)
        self.documents = [
            json.loads(line) for line in stdout.getvalue().splitlines() if line.strip()
        ]
        return self

    def kinds(self) -> list[str]:
        return [document["kind"] for document in self.documents]

    def of(self, kind: str) -> list[dict]:
        return [document for document in self.documents if document["kind"] == kind]

    def text(self) -> str:
        return "".join(token["text"] for token in self.of("token"))


class MlxWorkerTestCase(unittest.TestCase):
    """Base for every case that drives the real worker.

    ``mlx_worker._mlx()`` memoises the import in module globals, so a worker that has run once in
    this process keeps the first test's ``mx`` object — and therefore the first test's recorder,
    script and model — forever. Both sides have to be released between cases or the suite passes
    one configuration and asserts about another.
    """

    def setUp(self):
        self.addCleanup(fakemlx.uninstall)
        self.addCleanup(_reset_worker_import_cache)
        _reset_worker_import_cache()

    def worker(self, **fake: Any) -> Worker:
        return Worker(plan_document(), **fake)

    def assertNoFatal(self, worker: Worker) -> None:
        fatal = worker.of("fatal")
        self.assertEqual(fatal, [], f"worker died: {fatal}")
        self.assertEqual(worker.kinds()[0], "ready", worker.documents)


class CompletionTests(MlxWorkerTestCase):
    def test_a_generated_request_completes_instead_of_dying(self):
        worker = self.worker(script=[1, 2, 3, EOS])
        worker.generate(max_tokens=8).run()
        self.assertNoFatal(worker)
        self.assertEqual(worker.exit_code, 0, worker.documents)
        self.assertTrue(worker.of("end"), worker.documents)

    def test_a_model_that_emits_nothing_ends_cleanly(self):
        # A script with no tokens at all: the step generator yields nothing, so the trailing
        # response is the only thing the worker sees. The stub used to reach for a loop variable
        # that was never bound on this path, which is an UnboundLocalError inside the fake rather
        # than a verdict about the worker.
        worker = self.worker(script=[])
        worker.generate(max_tokens=8).run()
        self.assertNoFatal(worker)
        self.assertEqual(worker.text(), "", worker.documents)
        self.assertTrue(worker.of("end"), worker.documents)

    def test_residency_reaches_the_executor_from_the_nested_plan_key(self):
        """F9a: the resolver writes ``wiring.wired_bytes``; a top-level read applies zero."""
        worker = self.worker().request("status").run()
        self.assertNoFatal(worker)
        calls = worker.recording.with_name("set_wired_limit")
        self.assertTrue(calls, "set_wired_limit never ran, so nothing bounds device residency")
        self.assertEqual(calls[0][1][0], 8 * 1024**3)

    def test_status_never_reports_a_wired_limit_it_only_requested(self):
        """F9c: mlx 0.32.4 has no wired getter, so 'observed' cannot be filled with the request."""
        worker = self.worker().request("status").run()
        self.assertNoFatal(worker)
        status = worker.of("ready")[0]["status"]
        self.assertEqual(status.get("wired_bytes_requested"), 8 * 1024**3, status)
        self.assertIsNone(
            status.get("wired_bytes_observed"),
            f"reported an observation the platform cannot make: {status}",
        )


class CacheTests(MlxWorkerTestCase):
    def test_each_request_gets_its_own_cache(self):
        """F6: one cache for the process lifetime appends each new prompt onto the last."""
        worker = self.worker(script=[1, EOS])
        worker.generate(max_tokens=4)
        worker.generate(max_tokens=4)
        worker.run()
        self.assertNoFatal(worker)
        created = worker.recording.cache_created
        self.assertEqual(len(created), 2, f"expected one cache per request, saw {created}")
        self.assertEqual(
            len(set(created)), 2, f"one cache served two requests: {created}"
        )

    def test_a_model_without_make_cache_still_gets_a_per_layer_list(self):
        """F4c: a bare KVCache is not subscriptable, so the per-layer list is mandatory."""
        worker = Worker(plan_document(), model=fakemlx.ModelWithoutMakeCache(), script=[1, EOS])
        worker.generate(max_tokens=4).run()
        self.assertNoFatal(worker)


class GenerationSemanticsTests(MlxWorkerTestCase):
    def test_generation_stops_at_end_of_sequence_and_says_so(self):
        """F5a: the pinned step function has no EOS handling; the wrapper's is required."""
        words = {1: "a", 2: "b", EOS: "", 4: "y", 5: "z"}
        worker = self.worker(script=[1, 2, EOS, 4, 5], words=words)
        worker.generate(max_tokens=16).run()
        self.assertNoFatal(worker)
        end = worker.of("end")
        self.assertTrue(end, worker.documents)
        self.assertEqual(end[0]["finish_reason"], "stop", end[0])
        self.assertNotIn("z", worker.text(), f"text was produced after EOS: {worker.text()!r}")

    def test_budget_exhaustion_reports_length_not_stop(self):
        """F5b: labelling a truncated completion 'stop' is a lie a client can act on."""
        script = [1, 2, 3, 4, 5, 6, 7, 8]
        worker = self.worker(script=script, words={i: f"w{i}" for i in range(1, 20)})
        worker.generate(max_tokens=3).run()
        self.assertNoFatal(worker)
        end = worker.of("end")
        self.assertTrue(end, worker.documents)
        self.assertEqual(end[0]["finish_reason"], "length", end[0])

    def test_the_prompt_reaches_the_generator_one_dimensional(self):
        """F4b: a batched prompt reads as length 1, so the configured chunked prefill never runs."""
        worker = self.worker(script=[1, EOS]).generate(max_tokens=4).run()
        self.assertNoFatal(worker)
        self.assertEqual(
            worker.recording.with_name("generate_step_bad_prompt_ndim"),
            [],
            "the prompt reached the generator with a batch axis",
        )

    def test_a_character_split_across_two_tokens_is_not_emitted_in_pieces(self):
        """F5a: per-token decode() cannot represent a byte-level token on its own.

        0xc3 0xa9 is "é" in UTF-8. Decoding either byte alone is not valid UTF-8, so a loop that
        decodes one id at a time emits replacement characters where an incremental detokenizer
        emits the letter once the second byte arrives.
        """
        worker = self.worker(script=[0xC3, 0xA9, EOS])
        worker.generate(max_tokens=8).run()
        self.assertNoFatal(worker)
        self.assertNotIn(
            "\ufffd", worker.text(), f"partial characters emitted separately: {worker.text()!r}"
        )
        self.assertIn("\u00e9", worker.text(), f"character never reassembled: {worker.text()!r}")


class RequestFieldTests(MlxWorkerTestCase):
    def test_a_line_that_is_not_a_request_object_is_answered_not_fatal(self):
        # One bad request costs a refusal and nothing else, which is what this loop promises. Valid
        # JSON that is not an object reached .get() on a list instead, so a single stray line killed
        # the worker and every request after it.
        worker = self.worker().raw("[1, 2]").raw("null").request("status").run()
        self.assertNoFatal(worker)
        self.assertEqual(
            [doc["code"] for doc in worker.of("refused")],
            [mlx_worker.WorkerError.MALFORMED_REQUEST] * 2,
            worker.documents,
        )
        self.assertEqual(worker.kinds()[-1], "status", worker.documents)
        self.assertEqual(worker.exit_code, 0, worker.documents)

    def test_a_payload_that_is_not_an_object_is_refused_not_reported_as_damage(self):
        # The command loop distinguishes WorkerRefused from device damage, and an AttributeError out
        # of the validator landed in the second branch: a fatal document and exit 2, which retires a
        # healthy engine over a request it was only ever meant to refuse.
        for action in ("input_tokens", "generate"):
            with self.subTest(action=action):
                worker = self.worker().request(action, "not-an-object").run()
                self.assertEqual(worker.kinds(), ["ready", "refused"], worker.documents)
                self.assertEqual(
                    worker.of("refused")[0]["code"],
                    str(mlx_worker.WorkerError.MALFORMED_REQUEST),
                    worker.documents,
                )
                self.assertEqual(worker.exit_code, 0, worker.documents)

    def test_a_field_the_engine_cannot_honour_is_refused_not_ignored(self):
        for field in ("tool_choice", "seed", "presence_penalty", "frequency_penalty"):
            with self.subTest(field=field):
                worker = self.worker(script=[EOS])
                worker.generate(max_tokens=2, **{field: "anything"}).run()
                self.assertEqual(worker.kinds(), ["ready", "refused"], worker.documents)

    def test_a_sampling_field_the_manifest_honours_is_not_refused_from_a_client(self):
        """top_k and min_p reach the sampler from the profile but were refused from a request."""
        for field, value in (("top_k", 5), ("min_p", 0.05)):
            with self.subTest(field=field):
                worker = self.worker(script=[EOS])
                worker.generate(max_tokens=2, **{field: value}).run()
                self.assertNotIn("refused", worker.kinds(), worker.documents)


class StopSequenceTests(MlxWorkerTestCase):
    """``stop`` was accepted and ignored; it is now implemented, so it needs its own proof."""

    def test_a_stop_sequence_truncates_the_completion_and_reports_stop(self):
        words = {1: "alpha", 2: " STOP beta", EOS: ""}
        worker = self.worker(script=[1, 2, EOS], words=words)
        worker.generate(max_tokens=16, stop="STOP").run()
        self.assertNoFatal(worker)
        self.assertEqual(worker.text(), "alpha ", worker.documents)
        self.assertEqual(worker.of("end")[0]["finish_reason"], "stop", worker.documents)

    def test_a_stop_sequence_split_across_two_segments_does_not_leak(self):
        # Held back at the boundary rather than emitted and then retracted.
        words = {1: "keep<|im_", 2: "end|>drop", EOS: ""}
        worker = self.worker(script=[1, 2, EOS], words=words)
        worker.generate(max_tokens=16, stop="<|im_end|>").run()
        self.assertNoFatal(worker)
        self.assertEqual(worker.text(), "keep", worker.documents)

    def test_a_malformed_stop_list_is_refused_before_generation(self):
        for value in ("", ["ok", ""], 5, {"a": 1}):
            with self.subTest(value=value):
                worker = self.worker(script=[EOS])
                worker.generate(max_tokens=2, stop=value).run()
                self.assertEqual(worker.kinds(), ["ready", "refused"], worker.documents)

    def test_an_empty_stop_list_means_no_sequences(self):
        # Reusing the "no stop" meaning rather than refusing it, because that is what the field
        # means on the OpenAI surface the proxy forwards.
        words = {1: "alpha", 2: " beta", EOS: ""}
        worker = self.worker(script=[1, 2, EOS], words=words)
        worker.generate(max_tokens=16, stop=[]).run()
        self.assertNoFatal(worker)
        self.assertEqual(worker.text(), "alpha beta", worker.documents)

    def test_no_stop_field_is_unchanged_behaviour(self):
        words = {1: "alpha", 2: " beta", EOS: ""}
        worker = self.worker(script=[1, 2, EOS], words=words)
        worker.generate(max_tokens=16).run()
        self.assertNoFatal(worker)
        self.assertEqual(worker.text(), "alpha beta", worker.documents)


class PinnedSurfaceTests(unittest.TestCase):
    """The bug class behind F4 and F11b: code written against a remembered API.

    These need no GPU and no MLX — they only compare names against what the pinned releases export,
    so they run on every platform.

    The probe is the harder half: its MLX calls live inside f-strings that are *sent to another
    interpreter as source code*, so a scan over the module's own AST finds nothing and the test
    would pass vacuously. Each embedded program is therefore parsed on its own.
    """

    @staticmethod
    def attributes(tree: ast.AST, prefix: str) -> set[str]:
        found: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and _render(node.value) == prefix:
                found.add(node.attr)
        return found

    @staticmethod
    def keywords(tree: ast.AST, functions: set[str]) -> set[str]:
        found: set[str] = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if name not in functions:
                continue
            for keyword in node.keywords:
                if keyword.arg:
                    found.add(keyword.arg)
                elif isinstance(keyword.value, ast.Dict):
                    # **{"cache": cache} hides the same mistake from the named-keyword path.
                    found.update(
                        key.value
                        for key in keyword.value.keys
                        if isinstance(key, ast.Constant) and isinstance(key.value, str)
                    )
        return found

    @staticmethod
    def literal_text(node: ast.JoinedStr) -> str:
        """Rebuild an f-string as parseable source, with each interpolation as a name.

        The probe's MLX calls are inside f-strings, so they are ``JoinedStr`` nodes rather than
        string constants and a plain constant walk misses every one of them. ``{model!r}`` becomes a
        placeholder identifier, which keeps the surrounding code syntactically intact.
        """
        parts: list[str] = []
        counter = 0
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                parts.append(value.value)
            else:
                counter += 1
                parts.append(f"_interpolated_{counter}_")
        return "".join(parts)

    @staticmethod
    def trees(source: Path) -> list[ast.AST]:
        """The module itself plus every embedded program in it.

        An embedded program is a string constant or f-string that parses as Python. Snippets that do
        not parse are skipped: plenty of them are shell fragments and messages, and the point is to
        catch names, not to police prose.
        """
        text = source.read_text(encoding="utf-8")
        module = ast.parse(text)
        found = [module]
        candidates: list[str] = []
        for node in ast.walk(module):
            if isinstance(node, ast.JoinedStr):
                candidates.append(PinnedSurfaceTests.literal_text(node))
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                candidates.append(node.value)
        for candidate in candidates:
            if "\n" not in candidate:
                continue
            try:
                found.append(ast.parse(candidate))
            except SyntaxError:
                continue
        return found

    def _attributes(self, source: Path, prefixes: tuple[str, ...]) -> set[str]:
        names: set[str] = set()
        for tree in self.trees(source):
            for prefix in prefixes:
                names |= self.attributes(tree, prefix)
        return names

    def _keywords(self, source: Path, functions: set[str]) -> set[str]:
        names: set[str] = set()
        for tree in self.trees(source):
            names |= self.keywords(tree, functions)
        return names

    def test_every_mlx_attribute_the_worker_names_exists_in_the_pinned_release(self):
        used = self._attributes(WORKER, ("self._mx", "mx"))
        self.assertTrue(used, "the worker names no mlx attribute; retarget this test")
        unknown = used - fakemlx.MLX_CORE_NAMES
        self.assertFalse(
            unknown,
            f"mlx.{sorted(unknown)} is not exported by mlx {fakemlx.MLX_VERSION}",
        )

    def test_every_mlx_attribute_the_probe_names_exists_in_the_pinned_release(self):
        # The probe is the instrument a promotion rests on, and its per-case exception handler turns
        # an AttributeError into a *negative finding*: a wrong name here does not merely crash, it
        # fabricates evidence against the design.
        used = self._attributes(PROBE, ("mx", "mlx_core"))
        self.assertTrue(
            used,
            "the probe's embedded sources were not scanned; this test proves nothing",
        )
        unknown = used - fakemlx.MLX_CORE_NAMES - {"metal"}
        self.assertFalse(
            unknown,
            f"mx.{sorted(unknown)} is not exported by mlx {fakemlx.MLX_VERSION}",
        )

    def test_generate_step_and_stream_generate_are_called_only_with_pinned_keywords(self):
        for source in (WORKER, PROBE):
            for function, accepted in (
                ("generate_step", fakemlx.GENERATE_STEP_ACCEPTED_KEYWORDS),
                ("stream_generate", fakemlx.STREAM_GENERATE_ACCEPTED_KEYWORDS),
            ):
                with self.subTest(source=source.name, function=function):
                    found = self._keywords(source, {function})
                    if not found:
                        continue  # this file never calls that one
                    unknown = found - set(accepted)
                    self.assertFalse(
                        unknown,
                        f"{sorted(unknown)} are not parameters of {function} in "
                        f"mlx-lm {fakemlx.MLX_LM_VERSION}",
                    )

    def test_something_was_actually_checked(self):
        # A guard that silently stops matching anything is worse than no guard: it advertises
        # coverage of the exact bug class that produced F4 and F11b while checking nothing.
        checked = (
            self._attributes(WORKER, ("self._mx", "mx"))
            | self._attributes(PROBE, ("mx", "mlx_core"))
            | self._keywords(WORKER, {"generate_step"})
            | self._keywords(PROBE, {"generate_step"})
        )
        self.assertGreaterEqual(
            len(checked), 8, f"the pinned-surface scan found only {sorted(checked)}"
        )

    def test_the_fixture_does_not_invent_the_names_that_are_absent_upstream(self):
        recording = fakemlx.install()
        try:
            import mlx.core as mx

            for name in fakemlx.MLX_CORE_ABSENT_NAMES:
                self.assertFalse(
                    hasattr(mx, name),
                    f"the stub supplies {name}, which would launder the bug under test",
                )
        finally:
            fakemlx.uninstall()
        self.assertIsNotNone(recording)


def _render(node: ast.AST) -> str:
    """``self._mx.set_wired_limit`` -> ``self._mx``, so attribute bases can be compared."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _render(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    return ""


class CancellationBoundTests(MlxWorkerTestCase):
    """How much device work a cancelled request can still cause.

    The proxy releases its execution permit when the HTTP exchange ends, and on a client disconnect
    there is no response left to hold open, so the permit cannot literally be retained until the
    engine reports itself finished. What *is* achievable, and what this pins, is the bound: the
    worker stops at the next segment boundary and says that it stopped. Before the cancel channel
    the bound was "the whole remaining generation" — minutes of GPU work for an audience of none.
    """

    SCRIPT = list(range(1, 40)) + [EOS]
    WORDS = {i: f"w{i}" for i in range(1, 41)}

    def test_the_full_budget_is_used_when_nothing_is_cancelled(self):
        worker = self.worker(script=self.SCRIPT, words=self.WORDS)
        worker.generate(max_tokens=32).run()
        self.assertNoFatal(worker)
        self.assertEqual(worker.of("end")[0]["finish_reason"], "length", worker.documents)

    def test_a_cancellation_stops_generation_at_the_next_segment_boundary(self):
        # The flag is exactly what the watcher thread sets on a byte from the frontend, so raising
        # it here exercises the real interruption path rather than a reimplementation of it.
        fakemlx.install(script=self.SCRIPT, words=self.WORDS)
        self.addCleanup(fakemlx.uninstall)
        _reset_worker_import_cache()
        engine = mlx_worker.Engine("/fixture", plan_document())
        events = []
        for index, event in enumerate(
            engine.generate({"messages": [{"role": "user", "content": "x"}], "max_tokens": 32})
        ):
            events.append(event)
            if index == 1:
                engine.cancel_requested = True
        terminal = [entry for entry in events if entry["kind"] == "terminal"]
        self.assertTrue(terminal, events)
        self.assertTrue(terminal[0]["cancelled"], terminal)
        self.assertEqual(terminal[0]["finish_reason"], "cancelled", terminal)
        segments = [entry for entry in events if entry["kind"] == "segment"]
        self.assertLessEqual(
            len(segments), 3, f"generation ran on after the cancel: {segments}"
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
