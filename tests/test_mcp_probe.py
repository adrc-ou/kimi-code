"""Local HTTP MCP fixtures exercise the real SDK, worker protocol and discovery cost."""

import asyncio
import contextlib
import io
import json
import os
import tempfile
import threading
import time
import unittest
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from tests.test_mcp_compatibility import contract, tool
from tools import check_services as checks

try:
    import mcp  # noqa: F401
except ImportError:
    MCP_AVAILABLE = False
else:
    MCP_AVAILABLE = True


@unittest.skipUnless(MCP_AVAILABLE, "MCP SDK integration requires the pinned container/test SDK")
class McpProbeTests(unittest.TestCase):
    def setUp(self):
        self.calls = Counter()
        self.tools = [tool("read_wiki_structure")]
        self.cursor = None
        self.mode = "normal"
        self.release = threading.Event()
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                self.send_error(405)

            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                method = payload["method"]
                fixture.calls[method] += 1
                if fixture.mode == "stall":
                    fixture.release.wait(10)
                    return
                if "id" not in payload:
                    self.send_response(202)
                    self.end_headers()
                    return
                if method == "initialize":
                    result = {"protocolVersion": "2025-11-25", "capabilities": {"tools": {}},
                              "serverInfo": {"name": "local-fixture", "version": "1"}}
                elif method == "tools/list":
                    if fixture.mode == "oversized":
                        self.send_response(200)
                        length = checks.compatibility.MAX_CATALOG_BYTES + 1
                        self.send_header("Content-Length", str(length))
                        self.end_headers()
                        return
                    result = {"tools": fixture.tools}
                    if fixture.cursor:
                        result["nextCursor"] = fixture.cursor
                elif method == "tools/call":
                    result = {"content": [{"type": "text", "text": "Public fixture result"}],
                              "isError": False}
                else:
                    raise AssertionError(method)
                body = json.dumps({"jsonrpc": "2.0", "id": payload["id"],
                                   "result": result}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)
        self.config = {"url": f"http://127.0.0.1:{self.server.server_port}/mcp",
                       "enabledTools": ["read_wiki_structure"]}
        self.contract = contract(self.config, self.tools)

    def stop(self):
        self.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def probe(self, full=False, **kwargs):
        return asyncio.run(checks.mcp_probe("deepwiki", self.config, full, self.contract, **kwargs))

    def test_comparison_reuses_one_discovery_and_does_not_call_tools_in_quick_mode(self):
        result = self.probe()
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["compatibility"]["verdict"], "matched")
        self.assertEqual(self.calls["tools/list"], 1)
        self.assertEqual(self.calls["tools/call"], 0)

    def test_full_pass_reuses_discovery_before_approved_smoke_call(self):
        result = self.probe(full=True)
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(self.calls["tools/list"], 1)
        self.assertEqual(self.calls["tools/call"], 1)

    def test_changed_schema_and_renamed_tool_never_trigger_smoke_calls(self):
        for tools in ([tool("read_wiki_structure", inputSchema={"type": "object"})],
                      [tool("new_read_wiki_structure")]):
            self.tools = tools
            result = self.probe(full=True)
            self.assertEqual(result["status"], "FAIL")
        self.assertEqual(self.calls["tools/call"], 0)

    def test_repeated_page_and_oversized_response_are_unverified_failures(self):
        for mode in ("repeated", "oversized"):
            self.mode = mode
            self.cursor = "repeat" if mode == "repeated" else None
            evidence = {"verdict": "unverified"}
            with self.assertRaises(Exception) as raised:
                self.probe(evidence=evidence)
            self.assertEqual(checks.classify_error(raised.exception)[0], "FAIL")
            self.assertEqual(evidence["verdict"], "unverified")

    def test_required_empty_bearer_is_rejected_before_network_access(self):
        self.config["bearerTokenEnvVar"] = "FIXTURE_MISSING_BEARER"
        with patch.dict(os.environ, {"FIXTURE_MISSING_BEARER": ""}), \
                self.assertRaises(checks.UnmountedBearerVariable):
            self.probe()
        self.assertEqual(self.calls, Counter())

    def test_stalled_server_respects_worker_deadlines_and_never_reports_cached_success(self):
        self.mode = "stall"
        with tempfile.TemporaryDirectory() as directory:
            config, baseline = (Path(directory) / name for name in ("mcp.json", "contracts.json"))
            config.write_text(json.dumps({"mcpServers": {"deepwiki": self.config}}))
            baseline.write_text(json.dumps({"schema_version": 1,
                                            "servers": {"deepwiki": self.contract}}))
            started = time.monotonic()
            result = checks.run_probe("mcp", "deepwiki", False, config, 1.5, baseline)
            elapsed = time.monotonic() - started
            self.assertEqual(result["status"], "FAIL")
            self.assertEqual(result["compatibility"]["verdict"], "unverified")
            self.assertIn("2 attempts", result["detail"])
            self.assertLess(elapsed, 6)

    def test_worker_and_focused_report_preserve_structured_comparison_without_calls(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config, baseline, report = (root / name for name in (
                "mcp.json", "contracts.json", "report.json"
            ))
            config.write_text(json.dumps({"mcpServers": {"deepwiki": self.config}}))
            baseline.write_text(json.dumps({"schema_version": 1,
                                            "servers": {"deepwiki": self.contract}}))
            argv = ["check", "--config", str(config), "--contracts", str(baseline),
                    "--compatibility-only", "--server", "deepwiki", "--report", str(report)]
            with patch("sys.argv", argv), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(checks.main(), 0)
            value = json.loads(report.read_text())
            self.assertEqual(value["mode"], "compatibility")
            self.assertEqual(value["state"], "complete")
            self.assertEqual(len(value["checks"]), 1)
            self.assertEqual(value["checks"][0]["compatibility"]["verdict"], "matched")
            self.assertEqual(self.calls["tools/list"], 1)
            self.assertEqual(self.calls["tools/call"], 0)

    def test_focused_mode_suppresses_even_the_usual_quick_browser_call(self):
        self.tools = [tool("list_pages")]
        self.config["enabledTools"] = ["list_pages"]
        baseline = contract(self.config, self.tools)
        result = asyncio.run(checks.mcp_probe("chrome-devtools", self.config, False, baseline,
                                             compatibility_only=True))
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(self.calls["tools/call"], 0)
