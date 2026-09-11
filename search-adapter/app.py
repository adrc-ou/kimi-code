#!/usr/bin/env python3

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


SEARXNG_URL = os.environ.get(
    "SEARXNG_URL",
    "http://searxng:8080",
).rstrip("/")

INTERNAL_TOKEN = os.environ.get(
    "SEARCH_ADAPTER_TOKEN",
    "search-internal-only",
)


class Handler(BaseHTTPRequestHandler):
    server_version = "KimiSearchAdapter/1"

    def log_message(self, format_string, *args):
        print(format_string % args, flush=True)

    def send_json(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/healthz":
            self.send_json(200, {"status": "ok"})
            return
        self.send_json(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/search":
            self.send_json(404, {"error": "not found"})
            return

        expected = f"Bearer {INTERNAL_TOKEN}"
        if self.headers.get("Authorization") != expected:
            self.send_json(401, {"error": "unauthorized"})
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 64 * 1024:
                raise ValueError("invalid request size")
            payload = json.loads(self.rfile.read(length))
            query_value = payload["text_query"]
            if not isinstance(query_value, str):
                raise TypeError("text_query must be a string")
            query = query_value.strip()
            if not query:
                raise ValueError("text_query must not be empty")
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            self.send_json(400, {"error": str(exc)})
            return

        params = urllib.parse.urlencode(
            {"q": query, "format": "json", "language": "auto"}
        )
        request = urllib.request.Request(
            f"{SEARXNG_URL}/search?{params}",
            headers={"Accept": "application/json"},
        )

        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                upstream = json.load(response)
        except urllib.error.HTTPError as exc:
            self.send_json(502, {"error": f"SearXNG returned HTTP {exc.code}"})
            return
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            self.send_json(502, {"error": str(exc)})
            return

        results = []
        for item in upstream.get("results", [])[:20]:
            url = item.get("url", "")
            results.append(
                {
                    "site_name": urllib.parse.urlsplit(url).netloc,
                    "title": item.get("title", ""),
                    "url": url,
                    "snippet": item.get("content", ""),
                    "date": item.get("publishedDate", ""),
                }
            )
        self.send_json(200, {"search_results": results})


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
