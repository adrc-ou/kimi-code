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
    plan["schema_version"] = 3
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
        plan["schema_version"] = 2
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


if __name__ == "__main__":
    unittest.main()
