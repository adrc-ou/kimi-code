import importlib.util
import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "search_adapter", ROOT / "search-adapter" / "app.py"
)
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


class SearchAdapterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.upstream = ThreadingHTTPServer(("127.0.0.1", 0), SearchHandler)
        cls.upstream_thread = threading.Thread(
            target=cls.upstream.serve_forever, daemon=True
        )
        cls.upstream_thread.start()
        ADAPTER.SEARXNG_URL = f"http://127.0.0.1:{cls.upstream.server_port}"
        ADAPTER.INTERNAL_TOKEN = "adapter-test"
        ADAPTER.Handler.log_message = lambda *_args: None
        cls.adapter = ThreadingHTTPServer(("127.0.0.1", 0), ADAPTER.Handler)
        cls.adapter_thread = threading.Thread(
            target=cls.adapter.serve_forever, daemon=True
        )
        cls.adapter_thread.start()
        cls.url = f"http://127.0.0.1:{cls.adapter.server_port}/search"

    @classmethod
    def tearDownClass(cls):
        cls.adapter.shutdown()
        cls.upstream.shutdown()
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
        self.assertEqual(raised.exception.code, 401)


if __name__ == "__main__":
    unittest.main()
