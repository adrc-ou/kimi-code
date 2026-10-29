"""Fault injection for interrupted generations and recovery across client reconnects."""

import asyncio
import dataclasses
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aiohttp import ClientPayloadError, ClientSession, ClientTimeout, web

from tests.test_model_proxy import (
    CACHE_SALT,
    PROVIDER_KEY,
    PROXY,
    RATE_CAPACITY,
    RATE_UNIT,
    SUBAGENT,
    clean_counters,
    reset_policy_state,
)


def frame(delta=None, reason=None, **extra):
    return (
        b"data: "
        + json.dumps(
            {
                "choices": [{"index": 0, "delta": delta or {}, "finish_reason": reason}],
                **extra,
            }
        ).encode()
        + b"\n\n"
    )


COMPLETE = frame({"content": "recovered"}) + frame(reason="stop") + b"data: [DONE]\n\n"


class ProtocolTests(unittest.TestCase):
    def test_crlf_multiline_and_arbitrary_chunk_boundaries(self):
        raw = (
            b': ping\r\n\r\ndata: {"choices":\r\ndata: '
            b'[{"index":0,"delta":{"content":"answer"},"finish_reason":"stop"}]}\r\n\r\n'
            b"data: [DONE]\r\n\r\n"
        )
        scanner = PROXY.UsageScanner()
        forwarded = []
        for byte in raw:
            forwarded.extend(scanner.feed(bytes([byte])))
        scanner.validate()
        self.assertEqual(b"".join(forwarded), raw)
        self.assertTrue(scanner.useful)

    def test_incomplete_and_invalid_successes_are_rejected(self):
        for raw in [
            frame({"content": "partial"}),
            b"data: nope\n\n",
            frame(reason="stop") + b"data: [DONE]\n\n",
            COMPLETE + frame({"content": "after done"}),
            b'data: {"error":{"message":"backend broke","code":503}}\n\n',
        ]:
            with self.subTest(raw=raw):
                scanner = PROXY.UsageScanner()
                scanner.feed(raw)
                with self.assertRaises(PROXY.UpstreamProtocolError):
                    scanner.validate()

    def test_output_limit_and_filter_are_valid_completion_reasons(self):
        for reason in ["length", "content_filter"]:
            scanner = PROXY.UsageScanner()
            scanner.feed(
                frame({"content": "partial"} if reason == "length" else {}, reason)
                + b"data: [DONE]\n\n"
            )
            scanner.validate()

    def test_tool_arguments_are_counted_and_partial_usage_is_not_trusted(self):
        scanner = PROXY.UsageScanner()
        scanner.feed(frame({"tool_calls": [{"index": 0, "id": "call", "type": "function"}]}))
        scanner.feed(
            frame(
                {"tool_calls": [{"function": {"name": "Read", "arguments": "x" * 40}}]},
                "tool_calls",
            )
            + b"data: [DONE]\n\n"
        )
        scanner.validate()
        self.assertEqual(scanner.estimated_output(), 11)
        scanner = PROXY.UsageScanner()
        scanner.feed(b'data: {"usage":{"completion_tokens":1}}')
        scanner.finish()
        self.assertIsNone(scanner.completion_tokens)

    def test_partial_output_estimate_does_not_refund_unknown_dispatched_usage(self):
        cost = PROXY.Cost(10, SUBAGENT.output_clamp)
        self.assertIsNone(
            cost.credit(
                "output_tokens",
                PROXY.Attempt(
                    dispatched=True,
                    failure="interrupted",
                    estimated_output=1,
                ),
            )
        )
        self.assertEqual(
            cost.credit(
                "output_tokens",
                PROXY.Attempt(
                    dispatched=True,
                    failure="interrupted",
                    output_tokens=9,
                ),
            ),
            9,
        )

    def test_unknown_usage_renews_an_expired_reservation(self):
        now = [0.0]
        ledger = PROXY.RateLedger(RATE_CAPACITY, RATE_UNIT, clock=lambda: now[0])
        booking = ledger.reserve(100)
        now[0] = 90
        self.assertEqual(ledger.projected(), 0)
        ledger.settle(booking, None)
        self.assertEqual(ledger.projected(), 100)
        now[0] = 149
        self.assertEqual(ledger.projected(), 100)
        now[0] = 151
        self.assertEqual(ledger.projected(), 0)


class RecoveryTrackerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        clean_counters()
        self.now = 1000.0
        self.tracker = PROXY.RecoveryTracker(clock=lambda: self.now, capacity=2, idle_seconds=30)
        self.body = b'{"messages":[{"role":"user","content":"private request"}]}'

    def record_failure(self, entry, **kwargs):
        self.tracker.fail(entry, PROXY.Attempt(failure="transport_error", **kwargs))

    async def test_reconnect_keeps_cooldown_and_only_one_probe_runs(self):
        first = self.tracker.acquire(SUBAGENT, self.body)
        for _ in range(3):
            self.record_failure(first)
        self.tracker.release(first)
        second = self.tracker.acquire(SUBAGENT, self.body)
        self.assertIs(first, second)
        self.assertGreaterEqual(second.next_attempt, self.now + 30)
        self.now = second.next_attempt
        await self.tracker.admit(second)
        waiting = asyncio.create_task(self.tracker.admit(second))
        await asyncio.sleep(0)
        self.assertFalse(waiting.done())
        self.tracker.end_attempt(second)
        self.tracker.succeed(second)
        await asyncio.wait_for(waiting, 1)
        self.assertEqual(second.failures, 0)
        self.assertIsNotNone(second.last_completed)
        self.tracker.release(second)

    async def test_cooldown_is_bounded_and_long_retry_after_is_honored(self):
        entry = self.tracker.acquire(SUBAGENT, self.body)
        for _ in range(12):
            self.record_failure(entry)
            self.assertLessEqual(entry.next_attempt - self.now, 300.000001)
            self.now = entry.next_attempt
        self.record_failure(entry, retry_after=900)
        self.assertEqual(entry.next_attempt - self.now, 900)

    async def test_permanent_rejection_requires_changed_credential_policy_or_request(self):
        entry = self.tracker.acquire(SUBAGENT, self.body)
        self.record_failure(entry, status=401, permanent=True)
        with self.assertRaises(PROXY.RecoveryActionRequired):
            await self.tracker.admit(entry)
        with patch.object(PROXY, "upstream_credential", return_value="rotated-fixture"):
            changed = self.tracker.acquire(SUBAGENT, self.body)
            self.assertIsNot(entry, changed)
        self.tracker.release(changed)
        self.tracker.release(entry)

    def test_bound_expiry_active_entries_and_private_snapshot(self):
        first = self.tracker.acquire(SUBAGENT, self.body)
        self.record_failure(first)
        second = self.tracker.acquire(SUBAGENT, b'{"messages":[]}')
        with self.assertRaises(web.HTTPServiceUnavailable):
            self.tracker.acquire(SUBAGENT, b'{"messages":["different"]}')
        text = json.dumps(self.tracker.snapshot())
        for secret in [CACHE_SALT, PROVIDER_KEY, "private request"]:
            self.assertNotIn(secret, text)
        self.tracker.release(second)
        third = self.tracker.acquire(SUBAGENT, b'{"messages":["different"]}')
        self.now += 40
        self.tracker.prune(self.now)
        self.assertEqual(len(self.tracker.entries), 2)  # Both still have active users.
        self.tracker.release(first)
        self.tracker.release(third)
        self.now += 31
        self.assertEqual(self.tracker.snapshot()["tracked"], 0)


class StreamingFaultTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        clean_counters()
        reset_policy_state()
        self.storage = tempfile.TemporaryDirectory()
        self.addCleanup(self.storage.cleanup)
        self.patches = [
            patch.object(PROXY, "LOG_DIR", Path(self.storage.name)),
            patch.object(PROXY, "retry_delay", return_value=0.0),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)
        self.upstream = ClientSession(timeout=ClientTimeout(total=3, sock_read=1))
        self.addAsyncCleanup(self.upstream.close)
        self.client = ClientSession(timeout=ClientTimeout(total=3))
        self.addAsyncCleanup(self.client.close)
        self.calls = 0
        self.outputs = []
        self.raw_handler = None
        self.raw = await asyncio.start_server(self.handle_raw, "127.0.0.1", 0)
        self.addAsyncCleanup(self.close_raw)
        port = self.raw.sockets[0].getsockname()[1]
        self.lane = dataclasses.replace(SUBAGENT, base_url=f"http://127.0.0.1:{port}")
        app = web.Application()
        app["client"] = self.upstream
        app.router.add_post("/chat", self.handle_proxy)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        self.addAsyncCleanup(self.runner.cleanup)
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.url = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/chat"

    async def close_raw(self):
        self.raw.close()
        await self.raw.wait_closed()

    async def handle_raw(self, reader, writer):
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            length = next(
                int(x.split(b":", 1)[1])
                for x in head.splitlines()
                if x.lower().startswith(b"content-length:")
            )
            await reader.readexactly(length)
            self.calls += 1
            if self.raw_handler is not None:
                await self.raw_handler(reader, writer)
            else:
                output = self.outputs.pop(0)
                writer.write(output)
                await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    async def handle_proxy(self, request):
        body = await request.read()
        return await PROXY.forward_chat(
            request,
            self.lane,
            body,
            PROXY.time.monotonic(),
            body,
            PROXY.Cost(10, self.lane.output_clamp),
            100,
        )

    @staticmethod
    def http(body, status="200 OK", headers=b""):
        return (
            f"HTTP/1.1 {status}\r\nContent-Type: text/event-stream\r\n"
            f"Content-Length: {len(body)}\r\nConnection: close\r\n".encode()
            + headers
            + b"\r\n"
            + body
        )

    async def request(self, stream=True):
        return await self.client.post(self.url, json={"messages": [], "stream": stream})

    async def test_failure_before_first_frame_retries_without_delivering_bad_bytes(self):
        self.outputs = [b"HTTP/1.1 200 OK\r\nContent-Length: 1000\r\n\r\nbad", self.http(COMPLETE)]
        async with await self.request() as response:
            self.assertEqual(await response.read(), COMPLETE)
        self.assertEqual(self.calls, 2)
        self.assertEqual(next(iter(PROXY.recovery.entries.values())).failures, 0)

    async def test_malformed_chunking_and_truncated_error_body_recover(self):
        self.outputs = [
            b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\nZZ\r\n",
            b"HTTP/1.1 503 Unavailable\r\nContent-Length: 1000\r\n\r\nbroken",
            self.http(COMPLETE),
        ]
        async with await self.request() as response:
            self.assertEqual(await response.read(), COMPLETE)
        self.assertEqual(self.calls, 3)

    async def test_502_504_and_embedded_error_do_not_count_as_success(self):
        # Each case has a fresh tracker, so cooldown wall time is covered by tracker tests.
        for bad in [
            self.http(b"bad gateway", "502 Bad Gateway"),
            self.http(b"timed out", "504 Gateway Timeout"),
            self.http(b'data: {"error":{"code":503}}\n\n'),
        ]:
            PROXY.recovery = PROXY.RecoveryTracker()
            self.outputs = [bad, self.http(COMPLETE)]
            async with await self.request() as response:
                self.assertEqual(await response.read(), COMPLETE)
            self.assertEqual(next(iter(PROXY.recovery.entries.values())).failures, 0)

    async def test_partial_stream_is_dropped_and_next_client_can_recover(self):
        first = frame({"content": "partial"})
        self.outputs = [self.http(first), self.http(COMPLETE)]
        async with await self.request() as response:
            with self.assertRaises(ClientPayloadError):
                await response.read()
        entry = next(iter(PROXY.recovery.entries.values()))
        self.assertEqual(entry.failures, 1)
        self.assertGreater(
            PROXY.enforcement.ledgers[next(iter(PROXY.enforcement.ledgers))].committed, 0
        )
        async with await self.request() as response:
            self.assertEqual(await response.read(), COMPLETE)
        self.assertEqual(entry.failures, 0)
        self.assertEqual(self.calls, 2)

    async def test_done_is_not_delivered_when_http_body_is_truncated_after_it(self):
        self.outputs = [b"HTTP/1.1 200 OK\r\nContent-Length: 99999\r\n\r\n" + COMPLETE]
        received = bytearray()
        async with await self.request() as response:
            with self.assertRaises(ClientPayloadError):
                async for chunk in response.content.iter_any():
                    received.extend(chunk)
        self.assertNotIn(b"[DONE]", received)
        self.assertEqual(next(iter(PROXY.recovery.entries.values())).failures, 1)

    async def test_nonstreaming_json_validation_and_terminal_rejection_latch(self):
        raw = json.dumps(
            {
                "choices": [{"message": {"content": "answer"}, "finish_reason": "stop"}],
                "usage": {"completion_tokens": 5, "prompt_tokens": 10},
            }
        ).encode()
        self.outputs = [self.http(b"not json"), self.http(raw)]
        async with await self.request(stream=False) as response:
            self.assertEqual(await response.read(), raw)
        self.outputs = [self.http(b'{"error":"invalid key"}', "401 Unauthorized")]
        async with await self.request() as response:
            self.assertEqual(response.status, 401)
            await response.read()
        calls = self.calls
        async with await self.request() as response:
            self.assertEqual(response.status, 401)
            self.assertIn("operator action", await response.text())
        self.assertEqual(self.calls, calls)

    async def test_cancel_during_upstream_read_releases_permit_and_accounts_unknown_usage(self):
        started = asyncio.Event()
        release = asyncio.Event()
        self.addCleanup(release.set)

        async def hang(_reader, writer):
            writer.write(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n")
            await writer.drain()
            started.set()
            await release.wait()

        self.raw_handler = hang
        task = asyncio.create_task(self.request())
        await asyncio.wait_for(started.wait(), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        gate = PROXY.enforcement.gates[next(iter(PROXY.enforcement.gates))]
        for _ in range(20):
            if not gate.active:
                break
            await asyncio.sleep(0.05)
        self.assertEqual(gate.active, 0)
        self.assertEqual(self.calls, 1)
        self.assertFalse(any(e.users for e in PROXY.recovery.entries.values()))

    async def test_cancel_during_cooldown_books_no_capacity(self):
        entry = PROXY.recovery.acquire(
            self.lane, json.dumps({"messages": [], "stream": True}).encode()
        )
        for _ in range(3):
            PROXY.recovery.fail(entry, PROXY.Attempt(failure="transport_error"))
        PROXY.recovery.release(entry)
        task = asyncio.create_task(self.request())
        for _ in range(20):
            if entry.users:
                break
            await asyncio.sleep(0.01)
        self.assertEqual(entry.users, 1)
        self.assertEqual(self.calls, 0)
        self.assertTrue(all(not g.active for g in PROXY.enforcement.gates.values()))
        self.assertTrue(all(not ledger.committed for ledger in PROXY.enforcement.ledgers.values()))
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        for _ in range(20):
            if not entry.users:
                break
            await asyncio.sleep(0.02)
        self.assertEqual(entry.users, 0)
        self.assertEqual(entry.in_flight, 0)

    async def test_cancel_while_queued_refunds_undispatched_booking(self):
        gate = next(iter(PROXY.enforcement.gates.values()))
        with patch.object(gate, "subagent_limit", 0):
            task = asyncio.create_task(self.request())
            for _ in range(20):
                if any(ledger.committed for ledger in PROXY.enforcement.ledgers.values()):
                    break
                await asyncio.sleep(0.01)
            self.assertTrue(any(ledger.committed for ledger in PROXY.enforcement.ledgers.values()))
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            for _ in range(20):
                if not any(e.users for e in PROXY.recovery.entries.values()):
                    break
                await asyncio.sleep(0.02)
        self.assertEqual(self.calls, 0)
        self.assertEqual(gate.active, 0)
        self.assertTrue(all(not ledger.committed for ledger in PROXY.enforcement.ledgers.values()))
