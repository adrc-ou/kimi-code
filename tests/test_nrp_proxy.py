import asyncio
import contextlib
import copy
import importlib.util
import json
import os
import sys
import tempfile
import time
import tomllib
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
os.environ.update(
    {
        "NRP_UPSTREAM_ORIGIN": "https://example.invalid",
        "NRP_UPSTREAM_MODEL": "upstream-model",
        "NRP_API_KEY": "provider-fixture",
        "NRP_INTERNAL_TOKEN": "i" * 32,
        "NRP_CACHE_SALT": "c" * 43,
        "KIMI_CONFIG_PATH": str(ROOT / "runtime" / "config.toml"),
    }
)


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # dataclasses resolves string annotations through sys.modules, so the module
    # has to be registered before it is executed.
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


PROXY = load_module("nrp_proxy", ROOT / "proxy" / "nrp_proxy.py")
CONFIG_MERGE = load_module("kimi_config_merge", ROOT / "tools" / "kimi_config_merge.py")

BASE_CONFIG = tomllib.loads((ROOT / "runtime" / "config.toml").read_text())
SUBAGENT_RESERVATION = 64000
PRIMARY_RESERVATION = 262144


def write_config(directory: str, mutate=None) -> str:
    """Emit a mutated copy of the checked-in policy for loader tests."""
    document = copy.deepcopy(BASE_CONFIG)
    if mutate is not None:
        mutate(document)
    path = Path(directory) / "config.toml"
    path.write_text(CONFIG_MERGE.emit(document))
    return str(path)


def reset_policy_state() -> None:
    PROXY._policy_cache = None
    PROXY._policy_error = None
    PROXY._policy_error_logged = False


class SecretTests(unittest.TestCase):
    def test_short_provider_key_is_accepted_at_startup(self):
        self.assertEqual(PROXY.API_KEY, "provider-fixture")

    def test_file_secret_takes_precedence(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fixture-key"
            path.write_text("provider-fixture\n")
            with patch.dict(os.environ, {"TEST_KEY_FILE": str(path), "TEST_KEY": "fallback"}):
                self.assertEqual(
                    PROXY.secret("TEST_KEY_FILE", "TEST_KEY", min_length=1),
                    "provider-fixture",
                )
                with self.assertRaisesRegex(RuntimeError, "at least 32"):
                    PROXY.secret("TEST_KEY_FILE", "TEST_KEY")
            path.write_text(" \n")
            with patch.dict(os.environ, {"TEST_KEY_FILE": str(path), "TEST_KEY": "fallback"}):
                with self.assertRaises(RuntimeError):
                    PROXY.secret("TEST_KEY_FILE", "TEST_KEY", min_length=1)

    def test_empty_provider_key_and_short_internal_secrets_are_rejected(self):
        for value in ("", " ", "\n"):
            with self.subTest(value=value), patch.dict(
                os.environ, {"TEST_KEY_FILE": "", "TEST_KEY": value}
            ), self.assertRaises(RuntimeError):
                PROXY.secret("TEST_KEY_FILE", "TEST_KEY", min_length=1)
        with patch.dict(os.environ, {"TEST_KEY_FILE": "", "TEST_KEY": "too-short"}):
            with self.assertRaisesRegex(RuntimeError, "at least 32") as error:
                PROXY.secret("TEST_KEY_FILE", "TEST_KEY")
            self.assertNotIn("too-short", str(error.exception))


class Response:
    def __init__(self, headers):
        self.headers = headers


class Request:
    def __init__(self, method="POST", content_type="application/json", headers=None):
        self.method = method
        self.content_type = content_type
        self.headers = headers or {}


class RequestValidationTests(unittest.TestCase):
    def test_only_explicit_proxy_routes_are_registered(self):
        app = PROXY.create_app()
        routes = {(route.method, route.resource.canonical) for route in app.router.routes()}

        # aiohttp reports a dynamic resource by its canonical template; it does
        # not expand the lane regex into one canonical path per allowed value.
        # add_get also registers the corresponding HEAD route automatically.
        self.assertEqual(
            routes,
            {
                ("GET", "/healthz"),
                ("HEAD", "/healthz"),
                ("POST", "/{lane}/v1/chat/completions"),
                ("GET", "/{lane}/v1/models"),
                ("HEAD", "/{lane}/v1/models"),
            },
        )

        # The canonical form omits the variable's regex, so verify separately
        # that both dynamic resources retain the intended lane restriction.
        dynamic_patterns = {
            route.resource.get_info()["pattern"].pattern
            for route in app.router.routes()
            if route.resource.canonical.startswith("/{lane}/")
        }
        self.assertEqual(
            dynamic_patterns,
            {
                r"/(?P<lane>primary|long|subagent)/v1/chat/completions",
                r"/(?P<lane>primary|long|subagent)/v1/models",
            },
        )

    def test_model_is_rewritten(self):
        body = PROXY.rewrite_model(
            json.dumps({"model": "local-alias", "messages": []}).encode(),
            Request(),
        )
        self.assertEqual(json.loads(body)["model"], "upstream-model")
        self.assertEqual(json.loads(body)["cache_salt"], "c" * 43)

    def test_malformed_json_is_rejected(self):
        with self.assertRaises(PROXY.web.HTTPBadRequest):
            PROXY.rewrite_model(b"{not-json", Request())

    def test_incorrect_internal_bearer_is_rejected(self):
        with self.assertRaises(PROXY.web.HTTPUnauthorized):
            PROXY.authorize_client(Request(headers={"Authorization": "Bearer wrong"}))

    def test_valid_internal_bearer_is_accepted(self):
        PROXY.authorize_client(Request(headers={"Authorization": f"Bearer {'i' * 32}"}))

    def test_conflicting_output_limits_are_rejected(self):
        with self.assertRaises(PROXY.web.HTTPBadRequest):
            PROXY.rewrite_model(
                json.dumps({"messages": [], "max_tokens": 2, "max_completion_tokens": 3}).encode(),
                Request(),
            )

    def test_connection_nominated_header_is_removed(self):
        request = Request(headers={"Connection": "X-Remove", "X-Remove": "secret", "X-Keep": "yes"})
        headers = PROXY.filtered_request_headers(request)
        self.assertNotIn("X-Remove", headers)
        self.assertEqual(headers["X-Keep"], "yes")

    def test_upstream_requests_ask_for_identity_encoding(self):
        # Usage is read out of the response stream, so it must not be compressed.
        headers = PROXY.filtered_request_headers(Request(headers={"Accept-Encoding": "gzip"}))
        self.assertEqual(headers["Accept-Encoding"], "identity")

    def test_policy_rejects_out_of_range_percentage(self):
        with patch.object(PROXY, "FAIR_USE_PERCENT", 101):
            with self.assertRaisesRegex(RuntimeError, "must not exceed 100"):
                PROXY.validate_policy()

    def test_policy_rejects_nonpositive_values(self):
        with patch.object(PROXY, "SUBAGENT_LIMIT", 0):
            with self.assertRaisesRegex(RuntimeError, "must be positive"):
                PROXY.validate_policy()


class RetryDelayTests(unittest.TestCase):
    def test_numeric_retry_after_is_clamped(self):
        self.assertEqual(PROXY.retry_delay(Response({"Retry-After": "900"}), 1), 300)

    def test_http_date_retry_after(self):
        with patch.object(PROXY.time, "time", return_value=1_000.0):
            delay = PROXY.retry_delay(Response({"Retry-After": "Thu, 01 Jan 1970 00:18:20 GMT"}), 1)
        self.assertEqual(delay, 100)

    def test_epoch_millisecond_reset(self):
        now = time.time()
        with patch.object(PROXY.time, "time", return_value=now):
            delay = PROXY.retry_delay(Response({"x-ratelimit-reset": str((now + 45) * 1000)}), 1)
        self.assertAlmostEqual(delay, 45, places=3)


class LanePolicyTests(unittest.TestCase):
    """The enforced Kimi configuration is the single source of lane capacity."""

    def test_shipped_config_yields_expected_reservations(self):
        policy = PROXY.load_runtime_policy()
        self.assertEqual(policy.lanes["primary"].reservation, PRIMARY_RESERVATION)
        self.assertEqual(policy.lanes["subagent"].reservation, SUBAGENT_RESERVATION)
        self.assertEqual(policy.lanes["long"].reservation, 965536)
        self.assertEqual(policy.forced_alias, "qwen3-subagent")

    def test_long_lane_is_exclusive_under_the_35_percent_rule(self):
        policy = PROXY.load_runtime_policy()
        gate = PROXY.FairUseGate(5)
        self.assertTrue(gate.is_exclusive(policy.reservation("long")))
        self.assertFalse(gate.is_exclusive(policy.reservation("primary")))
        self.assertFalse(gate.is_exclusive(policy.reservation("subagent")))

    def test_every_lane_input_allowance_fits_its_own_window(self):
        policy = PROXY.load_runtime_policy()
        for lane, item in policy.lanes.items():
            with self.subTest(lane=lane):
                self.assertLessEqual(item.max_input + item.output_clamp, item.context)
                self.assertLessEqual(item.max_input + item.reserved, item.context)

    def test_secondary_model_must_stay_forced(self):
        with tempfile.TemporaryDirectory() as directory:

            def mutate(document):
                document["secondary_model"]["force"] = False

            path = write_config(directory, mutate)
            with self.assertRaisesRegex(PROXY.PolicyDrift, "force must be true"):
                PROXY.load_runtime_policy(path)

    def test_forced_secondary_model_must_use_the_subagent_provider(self):
        with tempfile.TemporaryDirectory() as directory:

            def mutate(document):
                document["secondary_model"]["default_model"] = "qwen3-primary"

            path = write_config(directory, mutate)
            with self.assertRaisesRegex(PROXY.PolicyDrift, "nrp-subagent"):
                PROXY.load_runtime_policy(path)

    def test_lane_provider_must_have_exactly_one_model(self):
        with tempfile.TemporaryDirectory() as directory:

            def mutate(document):
                document["models"]["qwen3-primary-duplicate"] = dict(
                    document["models"]["qwen3-primary"]
                )

            path = write_config(directory, mutate)
            with self.assertRaisesRegex(PROXY.PolicyDrift, "exactly one model"):
                PROXY.load_runtime_policy(path)

    def test_subagent_input_plus_reserve_must_fit_the_window(self):
        with tempfile.TemporaryDirectory() as directory:

            def mutate(document):
                document["models"]["qwen3-subagent"]["max_input_size"] = 63999

            path = write_config(directory, mutate)
            with self.assertRaisesRegex(PROXY.PolicyDrift, "exceeds window"):
                PROXY.load_runtime_policy(path)

    def test_input_plus_output_clamp_must_fit_a_small_window(self):
        with tempfile.TemporaryDirectory() as directory:

            def mutate(document):
                # Reserve is satisfied, but the 65,536 primary output clamp is not.
                document["models"]["qwen3-primary"]["max_context_size"] = 210000
                document["models"]["qwen3-primary"]["max_input_size"] = 205000
                document["loop_control"]["reserved_context_size"] = 2048

            path = write_config(directory, mutate)
            with self.assertRaisesRegex(PROXY.PolicyDrift, "output clamp exceeds window"):
                PROXY.load_runtime_policy(path)

    def test_startup_rejects_a_lane_that_could_never_be_admitted(self):
        with tempfile.TemporaryDirectory() as directory:

            def mutate(document):
                # 274,464 + 65,536 = 340,000, which is under the 350,000 fair-use
                # ceiling and therefore not exclusive, yet over the 320,000 budget.
                document["models"]["qwen3-primary"]["max_context_size"] = 400000
                document["models"]["qwen3-primary"]["max_input_size"] = 274464
                document["loop_control"]["reserved_context_size"] = 8192

            path = write_config(directory, mutate)
            policy = PROXY.load_runtime_policy(path)
            with self.assertRaisesRegex(RuntimeError, "can never be admitted"):
                PROXY.validate_policy(policy)

    def test_startup_rejects_a_subagent_batch_over_the_parallel_budget(self):
        with patch.object(PROXY, "SUBAGENT_LIMIT", 6):
            with self.assertRaisesRegex(RuntimeError, "exceeds parallel context budget"):
                PROXY.validate_policy()

    def test_startup_rejects_concurrency_above_the_model_ceiling(self):
        with patch.object(PROXY, "SUBAGENT_LIMIT", 17):
            with self.assertRaisesRegex(RuntimeError, "exceeds NRP model concurrency"):
                PROXY.validate_policy()

    def test_startup_rejects_a_budget_above_the_fair_use_ceiling(self):
        with patch.object(PROXY, "PARALLEL_CONTEXT_BUDGET", 400000):
            with self.assertRaisesRegex(RuntimeError, "exceeds the configured fair-use limit"):
                PROXY.validate_policy()

    def test_startup_rejects_headroom_outside_one_to_one_hundred(self):
        with patch.object(PROXY, "OUTPUT_RATE_HEADROOM", 0):
            with self.assertRaisesRegex(RuntimeError, "HEADROOM_PERCENT must be positive"):
                PROXY.validate_policy()
        with patch.object(PROXY, "OUTPUT_RATE_HEADROOM", 101):
            with self.assertRaisesRegex(RuntimeError, "HEADROOM_PERCENT must be between"):
                PROXY.validate_policy()


class PolicyDriftTests(unittest.TestCase):
    """Drift in the agent-writable config stops traffic instead of being ignored."""

    def setUp(self):
        reset_policy_state()
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = write_config(self.directory.name)
        patcher = patch.object(PROXY, "KIMI_CONFIG_PATH", self.path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def touch(self, mutate):
        path = Path(self.path)
        path.write_text(CONFIG_MERGE.emit(mutate(tomllib.loads(path.read_text()))))
        # Guarantee a different mtime+size stamp so the cache cannot return stale data.
        future = int(path.stat().st_mtime_ns) + 1_000_000_000
        os.utime(self.path, ns=(future, future))

    def test_valid_policy_is_served_and_cached(self):
        first = PROXY.current_policy()
        self.assertIs(PROXY.current_policy(), first)

    def test_becoming_invalid_fails_closed_then_recovers(self):
        self.assertEqual(PROXY.current_policy().forced_alias, "qwen3-subagent")

        def break_it(document):
            document["secondary_model"]["force"] = False
            return document

        self.touch(break_it)
        with self.assertRaises(PROXY.PolicyDrift):
            PROXY.current_policy()

        def fix_it(document):
            document["secondary_model"]["force"] = True
            return document

        self.touch(fix_it)
        self.assertEqual(PROXY.current_policy().forced_alias, "qwen3-subagent")

    def test_reservations_follow_the_live_file(self):
        def shrink(document):
            document["models"]["qwen3-subagent"]["max_input_size"] = 30000
            return document

        self.touch(shrink)
        self.assertEqual(PROXY.current_policy().reservation("subagent"), 38192)


class FairUseGateTests(unittest.IsolatedAsyncioTestCase):
    async def test_waiting_primary_blocks_new_subagent(self):
        gate = PROXY.FairUseGate(2)
        events = []
        release_first = asyncio.Event()

        async def first_subagent():
            async with gate.slot("subagent", SUBAGENT_RESERVATION):
                events.append("subagent-one-enter")
                await release_first.wait()
                events.append("subagent-one-exit")

        async def primary():
            async with gate.slot("primary", PRIMARY_RESERVATION):
                events.append("primary")

        async def second_subagent():
            async with gate.slot("subagent", SUBAGENT_RESERVATION):
                events.append("subagent-two")

        first = asyncio.create_task(first_subagent())
        while gate.active_subagents != 1:
            await asyncio.sleep(0)
        primary_task = asyncio.create_task(primary())
        while gate.waiting_primary != 1:
            await asyncio.sleep(0)
        second = asyncio.create_task(second_subagent())
        await asyncio.sleep(0)
        release_first.set()
        await asyncio.gather(first, primary_task, second)
        self.assertLess(events.index("primary"), events.index("subagent-two"))

    async def test_cancelled_primary_releases_waiter_count(self):
        gate = PROXY.FairUseGate(1)
        async with gate.slot("subagent", SUBAGENT_RESERVATION):
            task = asyncio.create_task(self._take_slot(gate, "primary", PRIMARY_RESERVATION))
            while gate.waiting_primary != 1:
                await asyncio.sleep(0)
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            self.assertEqual(gate.waiting_primary, 0)

    async def test_subagent_concurrency_is_capped(self):
        gate = PROXY.FairUseGate(2)
        active = 0
        maximum = 0
        entered = asyncio.Event()
        release = asyncio.Event()

        async def subagent():
            nonlocal active, maximum
            async with gate.slot("subagent", SUBAGENT_RESERVATION):
                active += 1
                maximum = max(maximum, active)
                if active == 2:
                    entered.set()
                await release.wait()
                active -= 1

        tasks = [asyncio.create_task(subagent()) for _ in range(3)]
        await entered.wait()
        await asyncio.sleep(0)
        self.assertEqual(gate.active_subagents, 2)
        release.set()
        await asyncio.gather(*tasks)
        self.assertEqual(maximum, 2)

    async def test_shipped_budget_keeps_primary_and_subagents_apart(self):
        # 262,144 + 64,000 exceeds the 320,000 aggregate allowance, so the lanes
        # are mutually exclusive as a consequence of policy, not hard-coding.
        self.assertEqual(
            await self._peak(
                ["primary", *["subagent"] * 5],
                budget=PROXY.PARALLEL_CONTEXT_BUDGET,
            ),
            1,
        )

    async def test_five_subagents_is_the_maximum_the_budget_allows(self):
        self.assertEqual(
            await self._peak(["subagent"] * 8, budget=PROXY.PARALLEL_CONTEXT_BUDGET),
            5,
        )

    async def test_a_wider_budget_admits_one_subagent_beside_the_primary(self):
        self.assertEqual(
            await self._peak(["primary", *["subagent"] * 5], budget=350000),
            2,
        )

    async def test_an_exclusive_lane_runs_alone(self):
        exclusive = PROXY.fair_use_ceiling() + 1
        self.assertEqual(
            await self._peak(
                ["long", "subagent", "subagent", "primary"],
                lane_reservations={"long": exclusive},
            ),
            1,
        )

    async def test_model_concurrency_caps_total_requests(self):
        self.assertEqual(
            await self._peak(["subagent"] * 4, model_limit=2, subagent_limit=4),
            2,
        )

    async def test_released_reservation_frees_the_budget(self):
        gate = PROXY.FairUseGate(5, model_limit=16, budget=320000)
        entered = asyncio.Event()
        release = asyncio.Event()

        async def primary():
            async with gate.slot("primary", PRIMARY_RESERVATION):
                entered.set()
                await release.wait()

        async def subagent():
            async with gate.slot("subagent", SUBAGENT_RESERVATION):
                return

        running = asyncio.create_task(primary())
        await entered.wait()
        blocked = asyncio.create_task(subagent())
        await asyncio.sleep(0)
        self.assertEqual(gate.active, 1)
        release.set()
        await running
        await blocked
        self.assertEqual((gate.active, gate.reserved), (0, 0))

    async def _peak(self, lanes, *, budget=320000, model_limit=16, subagent_limit=5, **extra):
        reservations = {
            "primary": PRIMARY_RESERVATION,
            "subagent": SUBAGENT_RESERVATION,
            "long": 965536,
            **extra.pop("lane_reservations", {}),
        }
        gate = PROXY.FairUseGate(
            subagent_limit,
            model_limit=model_limit,
            budget=budget,
            ceiling=PROXY.fair_use_ceiling(),
        )
        live = 0
        peak = 0
        release = asyncio.Event()

        async def one(lane):
            nonlocal live, peak
            async with gate.slot(lane, reservations[lane]):
                live += 1
                peak = max(peak, live)
                await release.wait()
                live -= 1

        tasks = [asyncio.create_task(one(lane)) for lane in lanes]
        while gate.active == 0:
            await asyncio.sleep(0)
        await asyncio.sleep(0.01)
        release.set()
        await asyncio.gather(*tasks)
        return peak

    @staticmethod
    async def _take_slot(gate, lane, reservation):
        async with gate.slot(lane, reservation):
            return


class IngressTests(unittest.IsolatedAsyncioTestCase):
    async def test_ingress_rejects_beyond_active_and_queue_bound(self):
        ingress = PROXY.IngressGate(active=1, queued=1)
        release = asyncio.Event()

        async def occupy():
            async with ingress.slot():
                await release.wait()

        active = asyncio.create_task(occupy())
        while ingress.pending != 1:
            await asyncio.sleep(0)
        queued = asyncio.create_task(occupy())
        while ingress.pending != 2:
            await asyncio.sleep(0)
        with self.assertRaises(PROXY.web.HTTPServiceUnavailable):
            async with ingress.slot():
                pass
        release.set()
        await asyncio.gather(active, queued)


class ChatRouteTests(unittest.IsolatedAsyncioTestCase):
    """The whole chat handler, so status constructions are exercised for real."""

    def setUp(self):
        reset_policy_state()
        self.sent = []

        async def capture(_request, lane, body, _started, _policy, _inbound):
            self.sent.append((lane, json.loads(body)))
            return PROXY.web.Response(text="ok")

        self.forward = patch.object(PROXY, "forward_chat", capture)
        self.forward.start()
        self.addCleanup(self.forward.stop)

    def request(self, body: bytes, lane="subagent", headers=None):
        merged = {"Authorization": f"Bearer {'i' * 32}"}
        merged.update(headers or {})
        return SimpleNamespace(
            method="POST",
            content_type="application/json",
            headers=merged,
            match_info={"lane": lane},
            query_string="",
            rel_url="/subagent/v1/chat/completions",
            transport=SimpleNamespace(is_closing=lambda: False),
            app={},
            read=lambda: asyncio.sleep(0, result=body),
        )

    async def test_legal_request_is_forwarded_with_the_lane_model(self):
        body = json.dumps({"model": "anything", "messages": [{"content": "hi"}]}).encode()
        response = await PROXY.chat(self.request(body))
        self.assertEqual(response.status, 200)
        lane, sent = self.sent[0]
        self.assertEqual(lane, "subagent")
        self.assertEqual(sent["model"], "upstream-model")
        self.assertEqual(sent["max_completion_tokens"], PROXY.MAX_OUTPUT_TOKENS["subagent"])

    async def test_gross_oversize_input_is_rejected_before_the_upstream(self):
        guard = PROXY.BASELINE_POLICY.lanes["subagent"].max_input
        body = json.dumps({"messages": [{"content": "x" * (guard * 8)}]}).encode()
        with self.assertRaises(PROXY.web.HTTPRequestEntityTooLarge):
            await PROXY.chat(self.request(body))
        self.assertEqual(self.sent, [])

    async def test_unverifiable_policy_stops_traffic(self):
        body = json.dumps({"messages": [{"content": "hi"}]}).encode()

        def mutate(document):
            document["secondary_model"]["force"] = False

        with tempfile.TemporaryDirectory() as directory:
            path = write_config(directory, mutate)
            with patch.object(PROXY, "KIMI_CONFIG_PATH", path):
                with self.assertRaises(PROXY.web.HTTPServiceUnavailable) as refused:
                    await PROXY.chat(self.request(body))
        self.assertEqual(refused.exception.headers.get("Retry-After"), "5")
        self.assertEqual(self.sent, [])

    async def test_unauthenticated_and_compressed_requests_are_refused(self):
        body = json.dumps({"messages": []}).encode()
        without_token = self.request(body, headers={"Authorization": "Bearer nope"})
        with self.assertRaises(PROXY.web.HTTPUnauthorized):
            await PROXY.chat(without_token)
        compressed = self.request(body, headers={"Content-Encoding": "gzip"})
        with self.assertRaises(PROXY.web.HTTPUnsupportedMediaType):
            await PROXY.chat(compressed)
        self.assertEqual(self.sent, [])


class OutputRateLedgerTests(unittest.TestCase):
    """Rule 1: 200,000 output tokens per minute is the gateway's only hard limit."""

    def setUp(self):
        self.now = 1000.0
        self.ledger = PROXY.OutputRateLedger(180000, clock=lambda: self.now)

    def advance(self, seconds: float) -> None:
        self.now += seconds

    def test_reservation_books_the_full_lane_allowance(self):
        booking = self.ledger.reserve(65536)
        self.assertIsNotNone(booking)
        self.assertEqual(self.ledger.committed, 65536)
        self.assertEqual(self.ledger.available(), 114464)

    def test_admission_stops_at_capacity(self):
        self.assertIsNotNone(self.ledger.reserve(120000))
        # A booking that would push the window past capacity is refused, but an
        # exact fill is allowed.
        self.assertIsNone(self.ledger.reserve(60001))
        self.assertIsNotNone(self.ledger.reserve(60000))
        self.assertEqual(self.ledger.committed, 180000)
        self.assertIsNone(self.ledger.reserve(1))
        self.assertGreater(self.ledger.wait_seconds(), 0)

    def test_settling_to_measured_usage_reopens_capacity_immediately(self):
        booking = self.ledger.reserve(65536)
        self.ledger.settle(booking, 1200)
        self.assertEqual(self.ledger.committed, 1200)

    def test_a_failed_attempt_gives_the_whole_booking_back(self):
        booking = self.ledger.reserve(65536)
        self.ledger.settle(booking, 0)
        self.assertEqual(self.ledger.committed, 0)

    def test_unknown_usage_keeps_the_pessimistic_booking(self):
        booking = self.ledger.reserve(65536)
        self.ledger.settle(booking, None)
        self.assertEqual(self.ledger.committed, 65536)

    def test_booking_is_settled_once(self):
        booking = self.ledger.reserve(65536)
        self.ledger.settle(booking, 100)
        self.ledger.settle(booking, 99999)
        self.assertEqual(self.ledger.committed, 100)

    def test_settling_an_expired_booking_does_not_double_count(self):
        booking = self.ledger.reserve(65536)
        self.advance(61)
        self.assertEqual(self.ledger.projected(), 0)
        self.ledger.settle(booking, 5000)
        self.assertEqual(self.ledger.projected(), 5000)

    def test_window_rolls_after_sixty_seconds(self):
        booking = self.ledger.reserve(180000)
        self.assertIsNotNone(booking)
        self.assertIsNone(self.ledger.reserve(1))
        self.advance(60.5)
        self.assertIsNotNone(self.ledger.reserve(180000))

    def test_gateway_remaining_quota_wins(self):
        self.ledger.note_server({"x-ratelimit-remaining": "2000", "x-ratelimit-reset": "12"})
        self.assertEqual(self.ledger.available(), 2000)
        self.assertIsNone(self.ledger.reserve(2001))
        self.assertIsNotNone(self.ledger.reserve(2000))

    def test_gateway_quota_is_forgotten_once_its_window_passes(self):
        self.ledger.note_server({"x-ratelimit-remaining": "10", "x-ratelimit-reset": "5"})
        self.advance(6)
        self.assertEqual(self.ledger.available(), 180000)

    def test_unparsable_quota_headers_are_ignored(self):
        self.ledger.note_server({"x-ratelimit-remaining": "soon"})
        self.assertIsNone(self.ledger.server_remaining)
        self.assertEqual(self.ledger.available(), 180000)

    def test_two_equal_bookings_are_independent(self):
        first = self.ledger.reserve(65536)
        second = self.ledger.reserve(65536)
        self.ledger.settle(first, 0)
        self.assertEqual(self.ledger.committed, 65536)
        self.ledger.settle(second, 0)
        self.assertEqual(self.ledger.committed, 0)


class UsageScannerTests(unittest.TestCase):
    @staticmethod
    def chunk(text: str) -> bytes:
        return text.encode()

    def test_usage_survives_a_split_mid_event(self):
        scanner = PROXY.UsageScanner()
        payload = (
            'data: {"choices":[{"delta":{"content":"hello"}}]}\n\n'
            'data: {"choices":[{"delta":{"content":" world"}}],'
            '"usage":{"prompt_tokens":1234,"completion_tokens":57}}\n\n'
            "data: [DONE]\n\n"
        )
        blob = self.chunk(payload)
        for index in range(0, len(blob), 7):
            scanner.feed(blob[index : index + 7])
        scanner.finish()
        self.assertEqual(scanner.completion_tokens, 57)
        self.assertEqual(scanner.prompt_tokens, 1234)
        self.assertEqual(scanner.text_chars, len("hello world"))
        self.assertIsNone(scanner.estimated_output())

    def test_reasoning_only_stream_is_still_measured(self):
        scanner = PROXY.UsageScanner()
        scanner.feed(
            self.chunk(
                'data: {"choices":[{"delta":{"reasoning_content":"thinking hard"}}]}\n\n'
            )
        )
        scanner.finish()
        self.assertEqual(scanner.estimated_output(), 4)  # 13 characters, no usage reported

    def test_events_without_usage_are_not_parsed_as_json(self):
        scanner = PROXY.UsageScanner()
        scanner.feed(self.chunk("data: not-json-at-all\n\n"))
        scanner.finish()
        self.assertIsNone(scanner.completion_tokens)

    def test_buffer_cap_degrades_instead_of_growing(self):
        scanner = PROXY.UsageScanner(cap=64)
        scanner.feed(self.chunk("x" * 5000))
        self.assertTrue(scanner.overflowed)
        scanner.feed(self.chunk('data: {"usage":{"completion_tokens":9}}\n\n'))
        self.assertIsNone(scanner.completion_tokens)
        self.assertIsNone(scanner.estimated_output())

    def test_json_completion_response_is_scanned(self):
        scanner = PROXY.UsageScanner()
        scanner.feed(
            self.chunk('data: {"choices":[{"message":{"content":"answer"}}],'
                       '"usage":{"completion_tokens":2}}\n\n')
        )
        scanner.finish()
        self.assertEqual(scanner.completion_tokens, 2)


class InputGuardTests(unittest.TestCase):
    def test_base64_media_is_charged_as_media_not_as_text(self):
        payload = {"messages": [{"content": "data:image/png;base64," + "A" * 4_000_000}]}
        estimate, media = PROXY.estimate_input_tokens(json.dumps(payload).encode())
        self.assertEqual(media, 1)
        self.assertLess(estimate, 10000)

    def test_guard_rejects_only_gross_oversize_requests(self):
        policy = PROXY.load_runtime_policy()
        lane = policy.lanes["subagent"]
        guard = lane.max_input * PROXY.INPUT_GUARD_PERCENT // 100
        oversized = json.dumps({"messages": [{"content": "x" * (guard * 4 + 4000)}]}).encode()
        estimate, _ = PROXY.estimate_input_tokens(oversized)
        self.assertGreater(estimate, guard)

    def test_a_full_but_legal_subagent_request_passes(self):
        policy = PROXY.load_runtime_policy()
        lane = policy.lanes["subagent"]
        guard = lane.max_input * PROXY.INPUT_GUARD_PERCENT // 100
        legal = json.dumps({"messages": [{"content": "x" * (lane.max_input * 4)}]}).encode()
        estimate, _ = PROXY.estimate_input_tokens(legal)
        self.assertLessEqual(estimate, guard)


class StreamOptionTests(unittest.TestCase):
    def setUp(self):
        patcher = patch.object(PROXY, "USAGE_SUPPORTED", True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def request(self):
        return Request()

    def test_usage_is_requested_only_for_streams(self):
        streamed = json.loads(
            PROXY.rewrite_model(
                json.dumps({"messages": [], "stream": True}).encode(), self.request(), "subagent"
            )
        )
        self.assertEqual(streamed["stream_options"], {"include_usage": True})

        plain = json.loads(
            PROXY.rewrite_model(json.dumps({"messages": []}).encode(), self.request(), "subagent")
        )
        self.assertNotIn("stream_options", plain)

    def test_client_stream_options_are_not_overwritten(self):
        body = json.loads(
            PROXY.rewrite_model(
                json.dumps(
                    {"messages": [], "stream": True, "stream_options": {"include_usage": False}}
                ).encode(),
                self.request(),
                "subagent",
            )
        )
        self.assertEqual(body["stream_options"], {"include_usage": False})

    def test_injection_stops_after_the_gateway_rejects_it(self):
        with patch.object(PROXY, "USAGE_SUPPORTED", False):
            body = json.loads(
                PROXY.rewrite_model(
                    json.dumps({"messages": [], "stream": True}).encode(),
                    self.request(),
                    "subagent",
                )
            )
        self.assertNotIn("stream_options", body)

    def test_only_a_bad_request_can_disable_usage(self):
        self.assertTrue(
            PROXY.body_rejects_stream_usage(400, b'{"error":"Unsupported stream_options"}')
        )
        self.assertFalse(PROXY.body_rejects_stream_usage(429, b"Unsupported stream_options"))
        self.assertFalse(PROXY.body_rejects_stream_usage(400, b'{"error":"bad model"}'))


class ForwardChatTests(unittest.IsolatedAsyncioTestCase):
    """Backoff must never occupy a fair-use permit or an output-token booking."""

    def setUp(self):
        reset_policy_state()
        self.policy = PROXY.load_runtime_policy()
        PROXY.rate_ledger.bookings.clear()
        PROXY.rate_ledger.committed = 0
        PROXY.rate_ledger.server_remaining = None

    def request(self):
        transport = SimpleNamespace(is_closing=lambda: False)
        return SimpleNamespace(
            method="POST",
            content_type="application/json",
            transport=transport,
            app={"client": SimpleNamespace()},
            headers={},
        )

    async def test_retry_sleeps_with_no_permit_and_no_booking(self):
        observations = []
        calls = {"count": 0}
        clamp = self.policy.output_clamp("subagent")
        first_response = SimpleNamespace()

        async def fake_attempt(_request, _session, lane, _body, policy, _attempt):
            async with PROXY.gate.slot(lane, policy.reservation(lane)):
                calls["count"] += 1
                observations.append(
                    ("attempt", PROXY.gate.active, PROXY.rate_ledger.committed)
                )
                if calls["count"] >= 2:
                    return PROXY.Attempt(
                        response=first_response, output_tokens=7, prompt_tokens=11
                    )
                return PROXY.Attempt(retry_after=0.0)

        real_sleep = asyncio.sleep

        async def watchful_sleep(delay, *_args, **_kwargs):
            observations.append(("sleep", PROXY.gate.active, PROXY.rate_ledger.committed))
            return await real_sleep(0)

        with (
            patch.object(PROXY, "stream_attempt", fake_attempt),
            patch.object(PROXY.asyncio, "sleep", watchful_sleep),
        ):
            result = await PROXY.forward_chat(
                self.request(),
                "subagent",
                b"{}",
                time.monotonic(),
                self.policy,
                b"{}",
            )

        self.assertIs(result, first_response)
        attempts = [item for item in observations if item[0] == "attempt"]
        waits = [item for item in observations if item[0] == "sleep"]
        self.assertEqual(len(attempts), 2)
        self.assertEqual(len(waits), 1)

        # Each attempt ran while holding exactly one permit and one full booking.
        for _tag, active, committed in attempts:
            self.assertEqual(active, 1)
            self.assertEqual(committed, clamp)

        # The backoff sleep itself held neither.
        for _tag, active, committed in waits:
            self.assertEqual(active, 0)
            self.assertEqual(committed, 0)

        self.assertEqual(PROXY.rate_ledger.committed, 7)

    async def test_request_deadline_stops_retrying(self):
        async def never_finish(*_args, **_kwargs):
            return PROXY.Attempt(retry_after=1.0)

        started = time.monotonic() - PROXY.MAX_REQUEST_SECONDS - 1
        with patch.object(PROXY, "stream_attempt", never_finish):
            with self.assertRaises(PROXY.web.HTTPBadGateway):
                await PROXY.forward_chat(
                    self.request(), "primary", b"{}", started, self.policy, b"{}"
                )
        self.assertEqual(PROXY.rate_ledger.committed, 0)

    async def test_measured_output_settles_the_booking(self):
        response = SimpleNamespace()

        async def completed(*_args, **_kwargs):
            return PROXY.Attempt(response=response, output_tokens=1500, prompt_tokens=9000)

        with patch.object(PROXY, "stream_attempt", completed):
            result = await PROXY.forward_chat(
                self.request(), "subagent", b"{}", time.monotonic(), self.policy, b"{}"
            )
        self.assertIs(result, response)
        self.assertEqual(PROXY.rate_ledger.committed, 1500)

    async def test_gateway_rejection_of_usage_is_retried_without_injection(self):
        calls = []

        async def reject_then_succeed(_request, _session, lane, body, policy, attempt):
            calls.append(body)
            if len(calls) == 1:
                return PROXY.Attempt(retry_after=0.0, rejected_usage=True)
            return PROXY.Attempt(
                response=SimpleNamespace(), output_tokens=10, prompt_tokens=20
            )

        inbound = b'{"messages":[],"stream":true}'
        with (
            patch.object(PROXY, "USAGE_SUPPORTED", True),
            patch.object(PROXY, "REQUEST_USAGE", True),
            patch.object(PROXY, "stream_attempt", reject_then_succeed),
        ):
            result = await PROXY.forward_chat(
                self.request(),
                "subagent",
                PROXY.rewrite_model(inbound, Request(), "subagent"),
                time.monotonic(),
                self.policy,
                inbound,
            )
            # The self-heal has to persist for the life of the process, not just
            # this request; the patch restores the original value on exit.
            self.assertFalse(PROXY.USAGE_SUPPORTED)

        self.assertIsNotNone(result)
        self.assertEqual(len(calls), 2)
        self.assertIn("stream_options", json.loads(calls[0]))
        self.assertNotIn("stream_options", json.loads(calls[1]))


class ConfigurationSourceTests(unittest.TestCase):
    def test_live_policy_matches_the_checked_in_arithmetic(self):
        with tempfile.TemporaryDirectory() as directory:
            path = write_config(directory)
            self.assertEqual(
                PROXY.load_runtime_policy(path).reservation("primary"), PRIMARY_RESERVATION
            )


if __name__ == "__main__":
    unittest.main()
