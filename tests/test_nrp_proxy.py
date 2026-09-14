import asyncio
import contextlib
import importlib.util
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
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
SPEC = importlib.util.spec_from_file_location("nrp_proxy", ROOT / "proxy" / "nrp_proxy.py")
assert SPEC and SPEC.loader
PROXY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PROXY)


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
        self.assertEqual(
            routes,
            {
                ("GET", "/healthz"),
                ("HEAD", "/healthz"),
                ("POST", "/primary/v1/chat/completions"),
                ("GET", "/primary/v1/models"),
                ("HEAD", "/primary/v1/models"),
                ("POST", "/long/v1/chat/completions"),
                ("GET", "/long/v1/models"),
                ("HEAD", "/long/v1/models"),
                ("POST", "/subagent/v1/chat/completions"),
                ("GET", "/subagent/v1/models"),
                ("HEAD", "/subagent/v1/models"),
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


class FairUseGateTests(unittest.IsolatedAsyncioTestCase):
    async def test_waiting_primary_blocks_new_subagent(self):
        gate = PROXY.FairUseGate(2)
        events = []
        release_first = asyncio.Event()

        async def first_subagent():
            async with gate.slot("subagent"):
                events.append("subagent-one-enter")
                await release_first.wait()
                events.append("subagent-one-exit")

        async def primary():
            async with gate.slot("primary"):
                events.append("primary")

        async def second_subagent():
            async with gate.slot("subagent"):
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
        async with gate.slot("subagent"):
            task = asyncio.create_task(self._take_slot(gate, "primary"))
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
            async with gate.slot("subagent"):
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

    @staticmethod
    async def _take_slot(gate, lane):
        async with gate.slot(lane):
            return


if __name__ == "__main__":
    unittest.main()
