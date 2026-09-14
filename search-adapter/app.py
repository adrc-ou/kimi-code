#!/usr/bin/env python3
"""Bounded schema adapter for the local SearXNG service."""

from __future__ import annotations

import hmac
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import BoundedSemaphore

SEARXNG_URL = os.environ.get("SEARXNG_URL", "http://searxng:8080").rstrip("/")
INTERNAL_TOKEN = os.environ.get("SEARCH_ADAPTER_TOKEN", "")
MAX_REQUEST_BYTES = int(os.environ.get("SEARCH_MAX_REQUEST_BYTES", str(64 * 1024)))
MAX_RESPONSE_BYTES = int(os.environ.get("SEARCH_MAX_RESPONSE_BYTES", str(4 * 1024 * 1024)))
MAX_RESULTS = int(os.environ.get("SEARCH_MAX_RESULTS", "20"))
MAX_FIELD = int(os.environ.get("SEARCH_MAX_FIELD_CHARS", "16384"))
MAX_OUTPUT_BYTES = int(os.environ.get("SEARCH_MAX_OUTPUT_BYTES", str(1024 * 1024)))
CONNECT_TIMEOUT = float(os.environ.get("SEARCH_CONNECT_TIMEOUT", "10"))
READ_TIMEOUT = float(os.environ.get("SEARCH_READ_TIMEOUT", "20"))


def bounded_text(value, maximum: int = MAX_FIELD) -> str:
    return value[:maximum] if isinstance(value, str) else ""


def load_bounded_json(response):
    declared = response.headers.get("Content-Length")
    if declared and int(declared) > MAX_RESPONSE_BYTES:
        raise ValueError("SearXNG response is too large")
    body = response.read(MAX_RESPONSE_BYTES + 1)
    if len(body) > MAX_RESPONSE_BYTES:
        raise ValueError("SearXNG response is too large")
    value = json.loads(body)
    if not isinstance(value, dict) or not isinstance(value.get("results", []), list):
        raise ValueError("unexpected SearXNG response schema")
    return value


class Handler(BaseHTTPRequestHandler):
    server_version = "KimiSearchAdapter/2"

    def log_message(self, format_string, *args):
        print(format_string % args, flush=True)

    def send_json(self, status, payload):
        body = json.dumps(payload, separators=(",", ":")).encode()
        if len(body) > MAX_OUTPUT_BYTES:
            status, body = 502, b'{"error":"normalized response is too large"}'
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/healthz":
            self.send_json(200, {"status": "ok"})
        else:
            self.send_json(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/search":
            self.send_json(404, {"error": "not found"})
            return
        supplied = self.headers.get("Authorization", "")
        if not hmac.compare_digest(supplied, f"Bearer {INTERNAL_TOKEN}"):
            self.send_json(401, {"error": "unauthorized"})
            return
        if (
            self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            != "application/json"
        ):
            self.send_json(415, {"error": "application/json required"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > MAX_REQUEST_BYTES:
                raise ValueError("invalid request size")
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise TypeError("request must be an object")
            query_value = payload["text_query"]
            if not isinstance(query_value, str):
                raise TypeError("text_query must be a string")
            query = query_value.strip()
            if not query or len(query) > 8192:
                raise ValueError("text_query length is invalid")
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            self.send_json(400, {"error": str(exc)})
            return

        params = urllib.parse.urlencode({"q": query, "format": "json", "language": "auto"})
        request = urllib.request.Request(
            f"{SEARXNG_URL}/search?{params}", headers={"Accept": "application/json"}
        )
        try:
            with urllib.request.urlopen(request, timeout=CONNECT_TIMEOUT) as response:
                sock = getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None)
                if sock is not None:
                    sock.settimeout(READ_TIMEOUT)
                upstream = load_bounded_json(response)
        except urllib.error.HTTPError as exc:
            self.send_json(502, {"error": f"SearXNG returned HTTP {exc.code}"})
            return
        except (urllib.error.URLError, TimeoutError, ValueError, json.JSONDecodeError):
            self.send_json(502, {"error": "invalid or unavailable SearXNG response"})
            return

        results = []
        for item in upstream["results"][:MAX_RESULTS]:
            if not isinstance(item, dict):
                continue
            url = bounded_text(item.get("url"), 8192)
            parsed = urllib.parse.urlsplit(url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                continue
            results.append(
                {
                    "site_name": bounded_text(parsed.netloc, 1024),
                    "title": bounded_text(item.get("title")),
                    "url": url,
                    "snippet": bounded_text(item.get("content")),
                    "date": bounded_text(item.get("publishedDate"), 256),
                }
            )
        self.send_json(200, {"search_results": results})


class BoundedHTTPServer(HTTPServer):
    def __init__(self, address, handler, workers=8, queued=32):
        super().__init__(address, handler)
        self.pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="search")
        self.capacity = BoundedSemaphore(workers + queued)

    def process_request(self, request, client_address):
        if not self.capacity.acquire(blocking=False):
            with request:
                request.sendall(
                    b"HTTP/1.1 503 Service Unavailable\r\n"
                    b"Connection: close\r\nRetry-After: 1\r\n"
                    b"Content-Length: 0\r\n\r\n"
                )
            return
        self.pool.submit(self._process, request, client_address)

    def _process(self, request, client_address):
        try:
            self.finish_request(request, client_address)
            self.shutdown_request(request)
        except BaseException:
            self.handle_error(request, client_address)
            self.shutdown_request(request)
        finally:
            self.capacity.release()

    def server_close(self):
        super().server_close()
        self.pool.shutdown(wait=True, cancel_futures=True)


if __name__ == "__main__":
    if len(INTERNAL_TOKEN) < 32:
        raise SystemExit("SEARCH_ADAPTER_TOKEN must contain at least 32 characters")
    BoundedHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()  # noqa: S104
