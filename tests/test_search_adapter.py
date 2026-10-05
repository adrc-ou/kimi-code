import contextlib
import importlib.util
import io
import json
import os
import socket
import subprocess
import sys
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("search_adapter", ROOT / "search-adapter" / "app.py")
assert SPEC and SPEC.loader
ADAPTER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ADAPTER)


class SearchHandler(BaseHTTPRequestHandler):
    def log_message(self, _format, *_args):
        return

    def do_GET(self):
        body = json.dumps(
            {
                "results": [
                    {
                        "title": "Example",
                        "url": "https://example.com/page",
                        "content": "A result",
                        "publishedDate": "2026-01-02",
                    }
                ]
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class SearchAdapterAccessLogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.lines = []
        cls.original_log_message = ADAPTER.Handler.log_message
        ADAPTER.Handler.log_message = lambda self, fmt, *args: cls.lines.append(fmt % args)
        cls.adapter = ADAPTER.BoundedHTTPServer(
            ("127.0.0.1", 0), ADAPTER.Handler, workers=2, queued=2
        )
        cls.adapter_thread = threading.Thread(target=cls.adapter.serve_forever, daemon=True)
        cls.adapter_thread.start()
        cls.base = f"http://127.0.0.1:{cls.adapter.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.adapter.shutdown()
        cls.adapter.server_close()
        cls.adapter_thread.join()
        ADAPTER.Handler.log_message = cls.original_log_message

    def get(self, path):
        try:
            with urllib.request.urlopen(f"{self.base}{path}") as response:
                return response.status
        except urllib.error.HTTPError as raised:
            try:
                return raised.code
            finally:
                raised.close()

    def test_healthy_probe_is_not_logged(self):
        self.lines.clear()
        self.assertEqual(self.get("/healthz"), 200)
        self.assertEqual(self.lines, [])

    def test_other_requests_are_logged(self):
        self.lines.clear()
        self.assertEqual(self.get("/missing"), 404)
        self.assertEqual(len(self.lines), 1)
        self.assertIn("/missing", self.lines[0])


class SearchAdapterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.upstream = ThreadingHTTPServer(("127.0.0.1", 0), SearchHandler)
        cls.upstream_thread = threading.Thread(target=cls.upstream.serve_forever, daemon=True)
        cls.upstream_thread.start()
        ADAPTER.SEARXNG_URL = f"http://127.0.0.1:{cls.upstream.server_port}"
        ADAPTER.INTERNAL_TOKEN = "adapter-test"
        ADAPTER.Handler.log_message = lambda *_args: None
        cls.adapter = ADAPTER.BoundedHTTPServer(
            ("127.0.0.1", 0), ADAPTER.Handler, workers=2, queued=2
        )
        cls.adapter_thread = threading.Thread(target=cls.adapter.serve_forever, daemon=True)
        cls.adapter_thread.start()
        cls.url = f"http://127.0.0.1:{cls.adapter.server_port}/search"

    @classmethod
    def tearDownClass(cls):
        cls.adapter.shutdown()
        cls.adapter.server_close()
        cls.upstream.shutdown()
        cls.upstream.server_close()
        cls.adapter_thread.join()
        cls.upstream_thread.join()

    def test_translates_search_schema(self):
        request = urllib.request.Request(
            self.url,
            data=json.dumps({"text_query": "test"}).encode(),
            headers={
                "Authorization": "Bearer adapter-test",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(request) as response:
            result = json.load(response)
        self.assertEqual(result["search_results"][0]["site_name"], "example.com")
        self.assertEqual(result["search_results"][0]["snippet"], "A result")

    def test_rejects_missing_token(self):
        request = urllib.request.Request(
            self.url,
            data=json.dumps({"text_query": "test"}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(request)
        try:
            self.assertEqual(raised.exception.code, 401)
        finally:
            raised.exception.close()


class UpstreamStub(BaseHTTPRequestHandler):
    """The SearXNG side, told per test which way to answer.

    One knob, because every refusal this adapter exists to make is chosen by what the upstream
    does to it. A stub that could only answer correctly would test the happy path forever.
    """

    mode = "ok"

    def log_message(self, _format, *_args):
        return

    def _results(self, count):
        return {
            "results": [
                {
                    "title": f"result {index}",
                    "url": f"https://example.com/{index}",
                    "content": f"snippet {index}",
                    "publishedDate": "2026-01-02",
                }
                for index in range(count)
            ]
        }

    def do_GET(self):
        if self.mode == "http_error":
            self.send_error(500, "upstream exploded")
            return
        if self.mode == "not_json":
            payload = b"<html>bad gateway</html>"
        elif self.mode == "wrong_shape":
            payload = b'{"results": "not a list"}'
        elif self.mode == "many_results":
            payload = json.dumps(self._results(40)).encode()
        else:
            payload = json.dumps(self._results(1)).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class SearchAdapterFailurePathsTests(unittest.TestCase):
    """What the adapter answers when the request, the upstream, or its own limits refuse."""

    @classmethod
    def setUpClass(cls):
        cls.upstream = ThreadingHTTPServer(("127.0.0.1", 0), UpstreamStub)
        cls.upstream_thread = threading.Thread(target=cls.upstream.serve_forever, daemon=True)
        cls.upstream_thread.start()
        cls.adapter = ADAPTER.BoundedHTTPServer(
            ("127.0.0.1", 0), ADAPTER.Handler, workers=2, queued=2
        )
        cls.adapter_thread = threading.Thread(target=cls.adapter.serve_forever, daemon=True)
        cls.adapter_thread.start()
        cls.url = f"http://127.0.0.1:{cls.adapter.server_port}/search"

    @classmethod
    def tearDownClass(cls):
        cls.adapter.shutdown()
        cls.adapter.server_close()
        cls.adapter_thread.join()
        cls.upstream.shutdown()
        cls.upstream.server_close()
        cls.upstream_thread.join()

    def setUp(self):
        self.addCleanup(setattr, ADAPTER, "SEARXNG_URL", ADAPTER.SEARXNG_URL)
        self.addCleanup(setattr, ADAPTER, "INTERNAL_TOKEN", ADAPTER.INTERNAL_TOKEN)
        self.addCleanup(setattr, UpstreamStub, "mode", "ok")
        ADAPTER.SEARXNG_URL = f"http://127.0.0.1:{self.upstream.server_port}"
        ADAPTER.INTERNAL_TOKEN = "adapter-failure-test"
        ADAPTER.Handler.log_message = lambda *_args: None

    def post(self, body, *, content_type="application/json", bearer="adapter-failure-test"):
        """One request, answered as (status, parsed-or-raw body) rather than as an exception."""
        headers = {"Content-Type": content_type}
        if bearer is not None:
            headers["Authorization"] = f"Bearer {bearer}"
        request = urllib.request.Request(self.url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as raised:
            try:
                return raised.code, raised.read()
            finally:
                raised.close()

    def search(self, payload):
        return self.post(json.dumps(payload).encode())

    def test_a_non_json_content_type_is_refused_before_the_upstream_is_read(self):
        # Nothing about the body has been parsed yet, so the cheapest honest answer is 415 and
        # no outbound request at all.
        status, body = self.post(
            b"text_query=hello", content_type="application/x-www-form-urlencoded"
        )
        self.assertEqual(status, 415)
        self.assertIn(b"application/json required", body)

    def test_an_empty_or_oversized_request_is_refused_without_being_buffered(self):
        # `length <= 0` is what stops the adapter reading a body that never arrives; the upper
        # bound is what stops one client allocating the whole of memory.
        self.assertEqual(self.post(b"")[0], 400)
        self.addCleanup(setattr, ADAPTER, "MAX_REQUEST_BYTES", ADAPTER.MAX_REQUEST_BYTES)
        ADAPTER.MAX_REQUEST_BYTES = 16
        status, body = self.post(b'{"text_query": "' + b"x" * 64 + b'"}')
        self.assertEqual(status, 400)
        self.assertIn(b"invalid request size", body)

    def test_a_body_that_is_not_an_object_is_refused_rather_than_read_for_a_query(self):
        for payload in (b"[]", b'"just a string"', b"not json at all"):
            with self.subTest(payload=payload[:20]):
                self.assertEqual(self.post(payload)[0], 400)

    def test_a_missing_or_non_string_or_blank_query_is_refused(self):
        for payload in ({}, {"text_query": 42}, {"text_query": "   "}, {"text_query": "y" * 8193}):
            with self.subTest(payload=str(payload)[:24]):
                self.assertEqual(self.search(payload)[0], 400)

    def test_an_upstream_http_error_is_reported_with_the_status_it_returned(self):
        # A bare 502 would hide whether SearXNG was down, rate-limiting, or 404 on its own route,
        # which is the difference an operator acts on.
        UpstreamStub.mode = "http_error"
        status, body = self.search({"text_query": "test"})
        self.assertEqual(status, 502)
        self.assertIn(b"HTTP 500", body)

    def test_an_unreachable_upstream_is_refused_rather_than_raising_through_the_handler(self):
        # The port is bound and released, so nothing listens: a URLError, not an HTTP status.
        # Left unhandled it would surface inside the thread pool and the client would see a
        # closed connection instead of a refusal it can retry.
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        dead_port = probe.getsockname()[1]
        probe.close()
        ADAPTER.SEARXNG_URL = f"http://127.0.0.1:{dead_port}"
        status, body = self.search({"text_query": "test"})
        self.assertEqual(status, 502)
        self.assertIn(b"invalid or unavailable SearXNG response", body)

    def test_an_unparsable_or_wrongly_shaped_upstream_body_is_refused(self):
        for mode in ("not_json", "wrong_shape"):
            with self.subTest(mode=mode):
                UpstreamStub.mode = mode
                status, body = self.search({"text_query": "test"})
                self.assertEqual(status, 502)
                self.assertIn(b"invalid or unavailable SearXNG response", body)

    def test_a_response_beyond_the_read_bound_is_refused_not_truncated(self):
        # Half a JSON document decoded into a result list is worse than no answer: it reads as
        # SearXNG having found little. The bound is checked against the bytes actually read, with
        # one byte of slack so an exactly-sized body still passes.
        self.addCleanup(setattr, ADAPTER, "MAX_RESPONSE_BYTES", ADAPTER.MAX_RESPONSE_BYTES)
        ADAPTER.MAX_RESPONSE_BYTES = 32
        response = io.BytesIO(b"x" * 33)
        response.headers = {}
        with self.assertRaisesRegex(ValueError, "too large"):
            ADAPTER.load_bounded_json(response)
        # And over the wire that refusal arrives as the one generic upstream fault, because the
        # client does not need to know which bound SearXNG crossed.
        UpstreamStub.mode = "many_results"
        status, body = self.search({"text_query": "test"})
        self.assertEqual(status, 502)
        self.assertIn(b"invalid or unavailable SearXNG response", body)

    def test_a_normalized_answer_bigger_than_the_output_bound_is_refused(self):
        # The bound exists because the caller is a model context, not a browser. Truncating the
        # list would silently change the answer, so the whole request is refused instead.
        self.addCleanup(setattr, ADAPTER, "MAX_OUTPUT_BYTES", ADAPTER.MAX_OUTPUT_BYTES)
        ADAPTER.MAX_OUTPUT_BYTES = 64
        UpstreamStub.mode = "many_results"
        status, body = self.search({"text_query": "test"})
        self.assertEqual(status, 502)
        self.assertIn(b"normalized response is too large", body)

    def test_a_result_that_is_not_an_object_or_not_a_web_url_is_dropped(self):
        # One malformed row must not answer for the whole search, and a `file://` or scheme-less
        # URL handed to an agent is a path the harness does not intend to be able to read.
        class Mixed(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                return

            def do_GET(self):
                payload = json.dumps(
                    {
                        "results": [
                            "not an object",
                            {"title": "no scheme", "url": "example.com/x", "content": "c"},
                            {"title": "on disk", "url": "file:///etc/passwd", "content": "c"},
                            {
                                "title": "kept",
                                "url": "https://example.com/kept",
                                "content": "c",
                            },
                        ]
                    }
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        mixed = ThreadingHTTPServer(("127.0.0.1", 0), Mixed)
        thread = threading.Thread(target=mixed.serve_forever, daemon=True)
        thread.start()
        try:
            ADAPTER.SEARXNG_URL = f"http://127.0.0.1:{mixed.server_port}"
            status, body = self.search({"text_query": "test"})
        finally:
            mixed.shutdown()
            mixed.server_close()
            thread.join()
        self.assertEqual(status, 200)
        results = json.loads(body)["search_results"]
        self.assertEqual([item["title"] for item in results], ["kept"])

    def test_oversized_upstream_fields_are_cut_to_their_own_bounds(self):
        # Each field has a different ceiling because each costs the model something different; a
        # snippet the length of the page would defeat the cap on the whole answer.
        long = "z" * (ADAPTER.MAX_FIELD + 5000)

        class Verbose(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                return

            def do_GET(self):
                payload = json.dumps(
                    {
                        "results": [
                            {
                                "title": long,
                                "url": "https://example.com/a",
                                "content": long,
                                "publishedDate": "2026-01-02",
                            }
                        ]
                    }
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        verbose = ThreadingHTTPServer(("127.0.0.1", 0), Verbose)
        thread = threading.Thread(target=verbose.serve_forever, daemon=True)
        thread.start()
        try:
            ADAPTER.SEARXNG_URL = f"http://127.0.0.1:{verbose.server_port}"
            results = json.loads(self.search({"text_query": "test"})[1])["search_results"]
        finally:
            verbose.shutdown()
            verbose.server_close()
            thread.join()
        self.assertEqual(len(results[0]["title"]), ADAPTER.MAX_FIELD)
        self.assertEqual(len(results[0]["snippet"]), ADAPTER.MAX_FIELD)

    def test_a_saturated_server_answers_503_rather_than_queueing_without_limit(self):
        # The semaphore is the whole back-pressure story: overflow must be refused now with a
        # retry hint, because an unbounded accept queue shows up as the client hanging.
        sent = []

        class Socket:
            def sendall(self, payload):
                sent.append(bytes(payload))

            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return False

        server = ADAPTER.BoundedHTTPServer(("127.0.0.1", 0), ADAPTER.Handler, workers=1, queued=0)
        try:
            self.assertTrue(server.capacity.acquire(blocking=False))
            with contextlib.redirect_stdout(io.StringIO()):
                server.process_request(Socket(), ("127.0.0.1", 55555))
        finally:
            server.server_close()
        answer = b"".join(sent)
        self.assertIn(b"503 Service Unavailable", answer)
        self.assertIn(b"Retry-After: 1", answer)

    def test_a_declared_response_size_over_the_bound_is_refused_before_it_is_read(self):
        # Trusting Content-Length is what lets a lying upstream allocate the read; the declared
        # size is refused on its own so the body is never pulled.
        response = io.BytesIO(b"x" * 10)
        response.headers = {"Content-Length": str(ADAPTER.MAX_RESPONSE_BYTES + 1)}
        with self.assertRaisesRegex(ValueError, "too large"):
            ADAPTER.load_bounded_json(response)

    def test_a_non_string_field_becomes_empty_rather_than_reaching_the_answer(self):
        # SearXNG fields are whatever upstream put there. A list would serialize into the model's
        # text as a Python repr, so anything not a string is simply absent.
        self.assertEqual(ADAPTER.bounded_text(["a", "b"]), "")
        self.assertEqual(ADAPTER.bounded_text(None), "")
        self.assertEqual(ADAPTER.bounded_text(7), "")
        self.assertEqual(ADAPTER.bounded_text("kept", 2), "ke")

    def test_the_server_refuses_to_start_on_a_short_token(self):
        # The adapter is reachable from inside the network namespace only, but it forwards an
        # authenticated search on the agent's behalf, so a stub token must not boot.
        env = {**os.environ, "SEARCH_ADAPTER_TOKEN": "too-short"}
        result = subprocess.run(
            [sys.executable, str(ROOT / "search-adapter" / "app.py")],
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("at least 32 characters", result.stderr)


if __name__ == "__main__":
    unittest.main()
