"""Local capacity uses the loaded engine's tokenizer and serving allocation."""

import asyncio
import copy
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from tests import test_model_proxy as fixtures

P = fixtures.PROXY
ENGINE_ID = "fixture/local"
CONTEXT = 16384
ENGINE_COUNTER = f"count:engine:{ENGINE_ID}"


def local_plan():
    plan = fixtures.mutated()
    plan["schema_version"] = 4
    plan["providers"]["fixture"] = {
        "base_url": "http://fixture.invalid",
        "protocol": "openai",
        "sends_cache_salt": False,
        "credentials": {"default": {"secret_name": "fixture__default"}},
    }
    lane = plan["lanes"]["subagent"]
    lane.update(
        model_id=ENGINE_ID,
        provider="fixture",
        credential="default",
        provider_name="fixture-subagent",
        model="fixture-model",
        alias="fixture-subagent",
        base_url="http://fixture.invalid",
        context_tokens=CONTEXT,
        input_tokens=CONTEXT - plan["reserved_context_size"],
        output_clamp_tokens=CONTEXT,
        reservation=CONTEXT,
        engine_id=ENGINE_ID,
        output_mode="remaining_context",
        token_accounting="engine",
        counters=[ENGINE_COUNTER],
    )
    for counter in plan["counters"].values():
        counter["lanes"] = [name for name in counter["lanes"] if name != "subagent"]
    plan["counters"][ENGINE_COUNTER] = {
        "id": ENGINE_COUNTER,
        "family": "count",
        "kind": "max_concurrent_requests",
        "scope": "engine",
        "source": "engine_capacity",
        "subject": ENGINE_ID,
        "provider": "fixture",
        "max": 2,
        "lanes": ["subagent"],
    }
    plan["limits"]["subagent_concurrency"] = 2
    plan["engines"] = {
        ENGINE_ID: {
            "context_tokens": CONTEXT,
            "sequences": 2,
            "total_context_tokens": CONTEXT * 2,
            "build_commit": "fixturecommit",
            "model_alias": "fixture-model",
            "model_path": "/fixture/model.gguf",
            "capacity_strategy": "native_context",
            "memory_management": "os_managed",
            "native_context_tokens": CONTEXT,
        }
    }
    return plan


def props():
    return {
        "build_info": "fixture-build-fixturecommit",
        "model_alias": "fixture-model",
        "model_path": "/fixture/model.gguf",
        "total_slots": 2,
        "default_generation_settings": {"n_ctx": CONTEXT},
        "is_sleeping": False,
    }


class MetadataResponse:
    def __init__(self, payload, *, status=200, raw=None, declared=None, blocker=None):
        self.data = json.dumps(payload).encode() if raw is None else raw
        self.status = status
        self.headers = {}
        self.content_length = declared
        self.content = self
        self.blocker = blocker
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        self.closed = True

    async def iter_chunked(self, size):
        if self.blocker:
            await self.blocker.wait()
        for offset in range(0, len(self.data), size):
            yield self.data[offset : offset + size]


class MetadataSession:
    def __init__(self, count=10, properties=None, response=None):
        self.count = count
        self.properties = props() if properties is None else properties
        self.response = response
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if url.endswith("/props"):
            return MetadataResponse(self.properties)
        return self.response or MetadataResponse({"input_tokens": self.count})


class LocalPlanTests(unittest.TestCase):
    def test_stale_physical_capacity_plan_requires_relaunch(self):
        plan = local_plan()
        plan["schema_version"] = 3
        with self.assertRaisesRegex(P.PolicyDrift, "relaunch"):
            fixtures.loaded_policy(plan)

    def test_a_mixed_plan_retains_the_nrp_counters_and_separate_local_slots(self):
        plan = local_plan()
        policy = fixtures.loaded_policy(plan)
        P.validate_policy(policy)
        self.assertEqual(policy.lanes["subagent"].reservation, CONTEXT)
        self.assertEqual(policy.lanes["primary"], fixtures.PRIMARY)
        self.assertEqual(policy.lanes["long"], fixtures.LONG)
        self.assertEqual(policy.counters[fixtures.CONTEXT_ID]["budget"], fixtures.BUDGET)
        self.assertEqual(policy.counters[fixtures.RATE_ID]["capacity"], fixtures.RATE_CAPACITY)

    def test_inconsistent_native_context_slots_and_stale_budgets_fail_closed(self):
        mutations = [
            lambda p: p["engines"][ENGINE_ID].update(total_context_tokens=CONTEXT),
            lambda p: p["engines"][ENGINE_ID].update(allocations={"missing": 1}),
            lambda p: p["engines"][ENGINE_ID].update(allocations={"host_ram": 2049}),
            lambda p: p["engines"][ENGINE_ID].update(allocations={"host_ram": True}),
            lambda p: p["counters"][ENGINE_COUNTER].update(max=3),
            lambda p: p["lanes"]["subagent"].update(token_accounting="estimate"),
            lambda p: p["lanes"]["subagent"].update(context_tokens=CONTEXT // 2),
            lambda p: p["lanes"]["subagent"].update(engine_id="fixture/missing"),
        ]
        for mutate in mutations:
            plan = local_plan()
            mutate(plan)
            with self.subTest(mutation=mutate), self.assertRaises((RuntimeError, P.PolicyDrift)):
                policy = fixtures.loaded_policy(plan)
                P.validate_policy(policy)

    def test_an_unreferenced_engine_is_rejected_without_memory_accounting(self):
        plan = local_plan()
        engine = copy.deepcopy(plan["engines"][ENGINE_ID])
        plan["engines"]["fixture/other"] = engine
        with self.assertRaises(RuntimeError):
            P.validate_policy(fixtures.loaded_policy(plan))


class LocalPreparationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        fixtures.reset_policy_state()
        fixtures.clean_counters()
        self.addCleanup(fixtures.clean_counters)
        self.plan = local_plan()
        mounted = fixtures.live(self.plan)
        mounted.__enter__()
        self.addCleanup(mounted.__exit__, None, None, None)
        self.policy = P.current_policy()
        self.lane = self.policy.lanes["subagent"]
        self.engine = self.policy.engines[ENGINE_ID]
        self.manager = P.LocalEngines()
        self.session = MetadataSession()
        self.request = SimpleNamespace(
            method="POST",
            content_type="application/json",
            headers={},
            transport=SimpleNamespace(is_closing=lambda: False),
            app={"client": self.session},
        )
        patcher = patch.object(P, "upstream_credential", return_value="fixture-key")
        patcher.start()
        self.addCleanup(patcher.stop)

    def body(self, **values):
        payload = {"model": "wrong-alias", "messages": [{"role": "user", "content": "Hi"}]}
        payload.update(values)
        return P.rewrite_model(json.dumps(payload).encode(), self.request, self.lane)

    async def prepare(self, **values):
        return await self.manager.prepare(self.request, self.lane, self.engine, self.body(**values))

    async def test_token_count_includes_bound_model_tools_and_unicode_without_byte_guard(self):
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "read",
                    "description": "文" * (CONTEXT * 3),
                    "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
                },
            }
        ]
        self.session.count = 80
        body, count, output = await self.prepare(tools=tools)
        self.assertEqual((count, output), (80, CONTEXT - 80))
        counted = json.loads(self.session.calls[1][2]["data"])
        self.assertEqual(counted["model"], "fixture-model")
        self.assertEqual(counted["tools"], tools)
        self.assertNotIn("cache_salt", counted)
        self.assertEqual(json.loads(body)["tools"], counted["tools"])
        self.assertEqual(self.session.calls[1][2]["headers"]["Authorization"], "Bearer fixture-key")
        self.assertFalse(self.session.calls[1][2]["allow_redirects"])
        self.assertEqual(P.enforcement.gates[ENGINE_COUNTER].active, 0)

    async def test_chat_uses_exact_count_once_and_prices_the_effective_output(self):
        self.request.match_info = {"lane": "subagent"}
        self.request.query_string = ""
        self.request.headers = {"Authorization": f"Bearer {fixtures.INTERNAL_TOKEN}"}
        inbound = json.dumps(
            {"messages": [{"role": "user", "content": "文" * CONTEXT}], "max_tokens": 100}
        ).encode()
        self.request.read = AsyncMock(return_value=inbound)
        self.session.count = CONTEXT - 50
        forward = AsyncMock(return_value=P.web.Response(text="ok"))
        with (
            patch.object(P, "local_engines", self.manager),
            patch.object(P, "forward_chat", forward),
            patch.object(P, "log_prompt", AsyncMock()),
        ):
            response = await P.chat(self.request)
        self.assertEqual(response.status, 200)
        self.assertEqual(len(self.session.calls), 2)
        args = forward.await_args.args
        self.assertEqual(json.loads(args[2])["max_tokens"], 50)
        self.assertEqual((args[5].input, args[5].output, args[6]), (CONTEXT - 50, 50, CONTEXT))

    async def test_dispatch_rechecks_properties_and_a_remote_lane_uses_no_engine(self):
        await self.manager.before_dispatch(self.request, fixtures.PRIMARY)
        self.assertEqual(self.session.calls, [])
        self.session.properties["total_slots"] = 1
        with self.assertRaises(P.web.HTTPServiceUnavailable):
            await self.manager.before_dispatch(self.request, self.lane)
        self.assertEqual(P.enforcement.gates[ENGINE_COUNTER].active, 0)

    async def test_remaining_context_and_explicit_generation_limit(self):
        self.session.count = CONTEXT - 1
        body, count, output = await self.prepare()
        self.assertEqual((count, output), (CONTEXT - 1, 1))
        self.assertEqual(json.loads(body)["max_completion_tokens"], 1)
        self.session.count = 0
        body, _count, output = await self.prepare(max_tokens=7, max_completion_tokens=7)
        self.assertEqual(output, 7)
        self.assertEqual(json.loads(body)["max_tokens"], 7)
        self.assertEqual(json.loads(body)["max_completion_tokens"], 7)

    async def test_input_at_or_above_context_is_refused(self):
        for count in (CONTEXT, CONTEXT + 1):
            self.session.count = count
            with self.subTest(count=count), self.assertRaises(P.web.HTTPRequestEntityTooLarge):
                await self.prepare()

    async def test_unavailable_or_malformed_count_has_no_estimate_fallback(self):
        cases = [
            MetadataResponse({}, status=503),
            MetadataResponse({}, raw=b"broken"),
            MetadataResponse({"input_tokens": True}),
            MetadataResponse({"input_tokens": -1}),
            MetadataResponse({"input_tokens": 1.5}),
            MetadataResponse({}),
            MetadataResponse({}, raw=b'{"input_tokens":1,"input_tokens":2}'),
            MetadataResponse({}, raw=b"x" * (P.LOCAL_METADATA_BYTES + 1)),
            MetadataResponse({}, declared=P.LOCAL_METADATA_BYTES + 1),
        ]
        for response in cases:
            self.session.response = response
            with (
                self.subTest(data=response.data[:30]),
                self.assertRaises(P.web.HTTPServiceUnavailable),
            ):
                await self.prepare()
            self.assertTrue(response.closed)
        self.assertEqual(P.enforcement.gates[ENGINE_COUNTER].active, 0)

    async def test_readiness_mismatch_only_rejects_the_local_engine(self):
        for key, value in (
            ("total_slots", 1),
            ("model_alias", "another"),
            ("model_path", "/another.gguf"),
            ("build_info", "unknown"),
            ("is_sleeping", True),
            ("total_slots", 2.0),
            ("default_generation_settings", {"n_ctx": float(CONTEXT)}),
            ("default_generation_settings", {"n_ctx": 4096}),
        ):
            properties = props()
            properties[key] = value
            self.session.properties = properties
            with self.subTest(key=key), self.assertRaises(P.web.HTTPServiceUnavailable):
                await self.prepare()
            self.assertEqual(P.current_policy().lanes["primary"], fixtures.PRIMARY)
        self.assertIsNone(P._policy_error)

    async def test_preparation_serializes_counting_and_cancel_releases_the_semaphore(self):
        blocker = asyncio.Event()
        self.session.response = MetadataResponse({"input_tokens": 3}, blocker=blocker)
        first = asyncio.create_task(self.prepare())
        await asyncio.sleep(0)
        second = asyncio.create_task(self.prepare())
        await asyncio.sleep(0)
        self.assertEqual(len(self.session.calls), 2)
        self.assertEqual(P.enforcement.gates[ENGINE_COUNTER].active, 0)
        second.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await second
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first
        self.assertEqual(self.manager.semaphore(ENGINE_ID)._value, 1)
        self.assertTrue(self.session.response.closed)


class FatalLatchTests(unittest.TestCase):
    """A latched engine stays latched, and a liveness observation is not evidence otherwise.

    The recorded incident was a backend that could no longer compute while `/props` kept answering.
    Every test here is one way an unhealthy engine could otherwise be read back as healthy.
    """

    def setUp(self):
        self.addCleanup(fixtures.clean_counters)
        self.plan = local_plan()
        mounted = fixtures.live(self.plan)
        mounted.__enter__()
        self.addCleanup(mounted.__exit__, None, None, None)
        self.policy = P.current_policy()
        self.lane = self.policy.lanes["subagent"]
        self.engine = self.policy.engines[ENGINE_ID]
        self.manager = P.LocalEngines()
        self.manager.sync(self.policy)
        patcher = patch.object(P, "current_policy", return_value=self.policy)
        patcher.start()
        self.addCleanup(patcher.stop)
        credentials = patch.object(P, "upstream_credential", return_value="fixture-key")
        credentials.start()
        self.addCleanup(credentials.stop)

    def latch(self, reason="metal could not allocate"):
        self.manager.latch_fatal(self.policy, ENGINE_ID, reason)

    def test_metadata_success_does_not_clear_the_latch(self):
        # This is the exact hole in the previous behaviour: prepare() wrote status "ready" after a
        # healthy /props, and a healthy /props is the one thing a dead backend still provides.
        self.latch()
        asyncio.run(self.manager.verify(MetadataSession(), self.lane, self.engine))
        self.manager.sync(self.policy)
        self.assertIsNotNone(self.manager.is_fatal(self.policy, ENGINE_ID))

    def test_a_policy_refresh_for_the_same_engine_is_not_a_relaunch(self):
        self.latch()
        refreshed = copy.deepcopy(self.policy)
        self.manager.sync(refreshed)
        self.assertIsNotNone(self.manager.is_fatal(self.policy, ENGINE_ID))

    def test_a_genuine_relaunch_with_new_identity_clears_the_latch(self):
        # The latch must not outlive the process that earned it, or one bad launch poisons the host.
        self.latch()
        relaunched = copy.deepcopy(self.policy)
        relaunched.engines[ENGINE_ID] = dict(relaunched.engines[ENGINE_ID], fingerprint="new")
        self.manager.sync(P.RuntimePolicy(**{**vars(self.policy), "engines": relaunched.engines}))
        self.assertIsNone(
            self.manager.is_fatal(
                P.RuntimePolicy(**{**vars(self.policy), "engines": relaunched.engines}), ENGINE_ID
            )
        )

    def test_a_latched_engine_is_refused_before_the_wire(self):
        self.latch()
        session = MetadataSession()
        with patch.object(self.manager, "verify", AsyncMock()) as verify:
            with self.assertRaises(P.web.HTTPServiceUnavailable):
                asyncio.run(
                    self.manager.prepare(
                        SimpleNamespace(app={"client": session}),
                        self.lane,
                        self.engine,
                        b'{"messages":[]}',
                    )
                )
        verify.assert_not_called()
        self.assertEqual(session.calls, [], "a latched engine must not be contacted at all")

    def test_the_terminal_refusal_withholds_the_retry_hint(self):
        # A Retry-After header is an instruction to keep looping; the plan refuses to assume that
        # wording alone ends a client configured to retry forever.
        self.manager.latch_fatal(self.policy, ENGINE_ID, "dead")
        terminal = self.manager.refusal(
            self.lane, "local engine failed permanently", retryable=False
        )
        self.assertNotIn("Retry-After", terminal.headers)
        temporary = self.manager.refusal(self.lane, "local engine unavailable", retryable=True)
        self.assertIn("Retry-After", temporary.headers)

    def test_an_unavailability_report_never_demotes_a_fatal_record(self):
        self.latch("metal could not allocate")
        self.manager.failed(self.lane, P.LocalEngineUnavailable("props timed out"))
        self.assertEqual(self.manager.status[ENGINE_ID]["status"], "fatal")

    def test_a_hang_only_latches_once_the_evidence_accumulates(self):
        # One timeout is a slow engine. Latching on the first would make a long prefill fatal,
        # which is the opposite mistake in a different costume.
        self.assertFalse(
            asyncio.run(self.manager.note_suspected_hang(self.policy, ENGINE_ID, "no headers", 3))
        )
        self.assertIsNone(self.manager.is_fatal(self.policy, ENGINE_ID))
        asyncio.run(self.manager.note_suspected_hang(self.policy, ENGINE_ID, "no headers", 3))
        self.assertTrue(
            asyncio.run(self.manager.note_suspected_hang(self.policy, ENGINE_ID, "no headers", 3))
        )
        self.assertIsNotNone(self.manager.is_fatal(self.policy, ENGINE_ID))

    def test_a_completion_resets_suspicion_but_never_the_latch(self):
        self.manager.suspect[ENGINE_ID] = 2
        self.latch()
        self.manager.note_computed(ENGINE_ID)
        self.assertNotIn(ENGINE_ID, self.manager.suspect)
        self.assertIsNotNone(self.manager.is_fatal(self.policy, ENGINE_ID))

    def test_attempt_exhaustion_latches_and_names_the_engine(self):
        # terminal_local_failure is module level and drives the shared manager, which is the object
        # the request path actually consults, so that is the latch under test here.
        with patch.object(P, "local_engines", self.manager):
            raised = P.terminal_local_failure(self.lane, 4)
        self.assertIs(type(raised), P.web.HTTPServiceUnavailable)
        self.assertNotIn("Retry-After", raised.headers)
        self.assertIn(ENGINE_ID, raised.text)
        self.assertIsNotNone(self.manager.is_fatal(self.policy, ENGINE_ID))


    def test_default_configuration_never_latches_on_a_single_timeout(self):
        # The two bounds default to zero, which must mean "behave as before", not "fail the engine
        # on the first blip". A latched engine is refused for the rest of the session, so an
        # over-eager default would turn a slow prefill into a dead local lane.
        self.assertEqual(P.LOCAL_DISPATCH_ATTEMPTS, 0)
        self.assertEqual(P.LOCAL_PROGRESS_LEASE_SECONDS, 0.0)
        self.assertGreater(P.LOCAL_HANG_OBSERVATIONS, 1)
        latch = AsyncMock()
        with patch.object(self.manager, "latch_fatal", latch):
            for _ in range(P.LOCAL_HANG_OBSERVATIONS - 1):
                asyncio.run(
                    self.manager.note_suspected_hang(self.policy, ENGINE_ID, "no headers", 3)
                )
        latch.assert_not_called()

    def test_the_latch_needs_the_stated_number_of_observations(self):
        with patch.object(P, "LOCAL_HANG_OBSERVATIONS", 2):
            asyncio.run(self.manager.note_suspected_hang(self.policy, ENGINE_ID, "no headers", 2))
            self.assertIsNone(self.manager.is_fatal(self.policy, ENGINE_ID))
            asyncio.run(self.manager.note_suspected_hang(self.policy, ENGINE_ID, "no headers", 2))
        self.assertIsNotNone(self.manager.is_fatal(self.policy, ENGINE_ID))

    def test_health_reports_liveness_alongside_last_preparation(self):
        report = self.manager.liveness(ENGINE_ID)
        self.assertEqual(report["active_permits"], 0)
        self.assertIsNone(
            report["seconds_since_last_completion"],
            "an engine that has served nothing must not look freshly healthy",
        )
        self.assertFalse(report["fatal"])
        self.manager.note_computed(ENGINE_ID)
        self.assertIsNotNone(self.manager.liveness(ENGINE_ID)["seconds_since_last_completion"])


class ScannerOutcomeTests(unittest.TestCase):
    """The stream reader has to distinguish an answer from a response.

    Upstream's ``UsageScanner`` is the single parser: it validates framing, in-band error
    objects and emptiness, and raises before anything unusable is delivered. ``local_outcome``
    then names that verdict in the vocabulary the launcher and the worker also speak. These
    cases assert the pair, because the classification is what the fatal latch and the progress
    lease are built on.
    """

    DONE = b"data: [DONE]\n\n"

    @staticmethod
    def sse(payload):
        return b"data: " + json.dumps(payload).encode() + b"\n\n"

    @staticmethod
    def delta(**fields):
        return {"choices": [{"delta": fields}]}

    @classmethod
    def scan(cls, chunks, **kwargs):
        scanner = P.UsageScanner(**kwargs)
        for chunk in chunks:
            scanner.feed(chunk)
        scanner.finish()
        return scanner

    @classmethod
    def classify(cls, chunks, **kwargs):
        return P.local_outcome(cls.scan(chunks, **kwargs))

    def test_an_error_object_in_a_200_stream_is_damage_not_success(self):
        # Before this the event carried neither usage nor content and was skipped, so a stream
        # that said nothing but "error" read as a completed request.
        scanner = self.scan([self.sse({"error": {"message": "metal out of memory"}})])
        self.assertIsNotNone(scanner.error)
        self.assertEqual(P.local_outcome(scanner), P.Outcome.ERROR_OBJECT)
        self.assertEqual(
            P.partition_for_outcome(P.Outcome.ERROR_OBJECT), P.EngineFailureClass.FATAL
        )

    def test_a_tool_only_completion_is_usable(self):
        scanner = self.scan(
            [
                self.sse(
                    self.delta(
                        tool_calls=[
                            {
                                "index": 0,
                                "id": "call_1",
                                "function": {"name": "read_file", "arguments": "{\"p\":\"a\"}"},
                            }
                        ]
                    )
                ),
                self.sse({"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]}),
                self.DONE,
            ]
        )
        scanner.validate()
        self.assertEqual(P.local_outcome(scanner), P.Outcome.TOOL_ONLY)
        self.assertTrue(P.is_usable_completion(P.Outcome.TOOL_ONLY))

    def test_prose_and_tools_together_are_still_an_answer(self):
        scanner = self.scan(
            [
                self.sse(self.delta(content="sure", tool_calls=[{"index": 0}])),
                self.sse({"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]}),
                self.DONE,
            ]
        )
        self.assertEqual(P.local_outcome(scanner), P.Outcome.CONTENT)

    def test_thinking_with_no_answer_behind_it_is_not_usable(self):
        scanner = self.scan(
            [
                self.sse(self.delta(reasoning_content="hmm")),
                self.sse({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}),
                self.DONE,
            ]
        )
        self.assertEqual(P.local_outcome(scanner), P.Outcome.REASONING_ONLY)
        self.assertFalse(P.is_usable_completion(P.Outcome.REASONING_ONLY))

    def test_an_empty_completion_is_reported_apart_from_damage(self):
        # An immediate end of stream is a model behaviour, not proof the device broke. Latching
        # on it would turn a bad prompt into a dead session.
        scanner = self.scan(
            [self.sse({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}), self.DONE]
        )
        self.assertEqual(P.local_outcome(scanner), P.Outcome.EMPTY)
        self.assertIsNone(P.partition_for_outcome(P.Outcome.EMPTY))

    def test_truncation_at_the_output_bound_is_named_as_such(self):
        scanner = self.scan(
            [
                self.sse(self.delta(content="partial")),
                self.sse({"choices": [{"index": 0, "delta": {}, "finish_reason": "length"}]}),
                self.DONE,
            ]
        )
        self.assertEqual(P.local_outcome(scanner), P.Outcome.TRUNCATED)
        self.assertFalse(P.is_usable_completion(P.Outcome.TRUNCATED))

    def test_keepalives_are_progress_on_the_wire_and_not_from_the_model(self):
        # The distinction the progress lease exists to make: a wedged engine keeps emitting
        # comments, and a byte-idle timeout cannot tell that from work.
        live = self.scan([self.sse(self.delta(content="hi")), self.DONE])
        quiet = self.scan([b": keepalive\n\n", b"\n"])
        self.assertGreater(live.events, 0)
        self.assertEqual(
            quiet.events, 0, "a comment frame must not register as a model event"
        )

    def test_a_stream_that_never_completed_is_never_reported_as_content(self):
        # The case worth guarding: content arrived, then the connection ended with no finish
        # reason and no [DONE]. A client that acted on this could be obeying a truncated tool
        # call, so it must not be classified as a completed answer.
        scanner = self.scan(
            [
                self.sse(self.delta(content="half a sentence")),
                self.sse({"choices": [{"index": 0, "delta": {}, "finish_reason": None}]}),
            ]
        )
        self.assertEqual(P.local_outcome(scanner), P.Outcome.TRUNCATED)
        self.assertFalse(P.is_usable_completion(P.Outcome.TRUNCATED))

    def test_a_non_streaming_body_is_a_completion_when_read_as_one(self):
        scanner = P.UsageScanner()
        scanner.observe(
            {"choices": [{"index": 0, "message": {"content": "hi"}, "finish_reason": "stop"}]},
            streaming=False,
        )
        scanner.done = True
        self.assertEqual(P.local_outcome(scanner), P.Outcome.CONTENT)

    def test_unparseable_frame_is_a_framing_failure(self):
        scanner = self.scan([b"data: {\"choices\": [oops\n\n"])
        with self.assertRaises(P.UpstreamProtocolError):
            scanner.validate()
        self.assertEqual(P.local_outcome(scanner), P.Outcome.FRAMING_FAILURE)

    def test_overflowing_the_buffer_is_a_framing_failure(self):
        scanner = P.UsageScanner(cap=16)
        scanner.feed(b"data: " + b"x" * 200)
        scanner.finish()
        self.assertEqual(P.local_outcome(scanner), P.Outcome.FRAMING_FAILURE)

    def test_remote_lanes_never_receive_a_local_outcome(self):
        # The classification exists because HTTP 200 was proven meaningless for a local engine.
        # Applying it to a remote lane would change what is booked against a provider allowance.
        self.assertIsNone(P.Attempt().outcome)
        self.assertFalse(P.Attempt().unusable_local_completion)
        # An unusable *local* outcome is what the property reports; a remote attempt has no
        # outcome at all and so is never flagged.
        self.assertTrue(P.Attempt(outcome=P.Outcome.EMPTY).unusable_local_completion)
        self.assertFalse(P.Attempt(outcome=P.Outcome.CONTENT).unusable_local_completion)


if __name__ == "__main__":
    unittest.main()
