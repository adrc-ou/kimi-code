import asyncio
import contextlib
import importlib.util
import json
import os
import time
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
os.environ.update(
    {
        "NRP_UPSTREAM_ORIGIN": "https://example.invalid",
        "NRP_UPSTREAM_MODEL": "upstream-model",
        "NRP_API_KEY": "test-only",
        "KIMI_CONFIG_PATH": str(ROOT / "runtime" / "config.toml"),
    }
)
SPEC = importlib.util.spec_from_file_location(
    "nrp_proxy", ROOT / "proxy" / "nrp_proxy.py"
)
assert SPEC and SPEC.loader
PROXY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PROXY)


class Response:
    def __init__(self, headers):
        self.headers = headers


class Request:
    def __init__(self, method="POST", content_type="application/json", headers=None):
        self.method = method
        self.content_type = content_type
        self.headers = headers or {}


class RequestValidationTests(unittest.TestCase):
    def test_model_is_rewritten(self):
        body = PROXY.rewrite_model(
            json.dumps({"model": "local-alias", "messages": []}).encode(),
            Request(),
        )
        self.assertEqual(json.loads(body)["model"], "upstream-model")

    def test_malformed_json_is_unchanged(self):
        body = b"{not-json"
        self.assertEqual(PROXY.rewrite_model(body, Request()), body)

    def test_incorrect_internal_bearer_is_rejected(self):
        with self.assertRaises(PROXY.web.HTTPUnauthorized):
            PROXY.authorize_client(Request(headers={"Authorization": "Bearer wrong"}))

    def test_valid_internal_bearer_is_accepted(self):
        PROXY.authorize_client(
            Request(headers={"Authorization": "Bearer proxy-only"})
        )

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
            delay = PROXY.retry_delay(
                Response({"Retry-After": "Thu, 01 Jan 1970 00:18:20 GMT"}), 1
            )
        self.assertEqual(delay, 100)

    def test_epoch_millisecond_reset(self):
        now = time.time()
        with patch.object(PROXY.time, "time", return_value=now):
            delay = PROXY.retry_delay(
                Response({"x-ratelimit-reset": str((now + 45) * 1000)}), 1
            )
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

    @staticmethod
    async def _take_slot(gate, lane):
        async with gate.slot(lane):
            return


if __name__ == "__main__":
    unittest.main()
