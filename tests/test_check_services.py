import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from tools import check_services as checks


class ServiceCheckTests(unittest.TestCase):
    def test_setup_survives_async_transport_exception_groups(self):
        error = ExceptionGroup("transport", [
            ExceptionGroup("session", [checks.NeedsSetup("private project name")])
        ])
        status, detail = checks.classify_error(error)
        self.assertEqual(status, "SETUP")
        self.assertNotIn("private project name", detail)

    def test_allowlist_and_blocklist_match_exposed_tools(self):
        tools = [SimpleNamespace(name=name) for name in ("read", "write", "list")]
        self.assertEqual(checks.selected_tools(
            {"enabledTools": ["read", "list"], "disabledTools": ["list"]}, tools
        ), {"read"})

    def test_missing_expected_tool_fails(self):
        with self.assertRaises(ValueError):
            checks.selected_tools({"enabledTools": ["missing"]}, [])

    def test_empty_allowlist_does_not_enable_everything(self):
        self.assertEqual(checks.selected_tools(
            {"enabledTools": []}, [SimpleNamespace(name="write")]
        ), set())

    def test_language_server_readiness_is_what_serena_probe_reports(self):
        self.assertEqual(
            checks.serena_language_server("...\nLanguage server status: ready\n..."), "ready"
        )

    def test_unstarted_language_server_fails_rather_than_passing(self):
        # get_current_config succeeds while every symbol tool is dead, which is the state a service
        # check must never report as healthy.
        status, detail = checks.classify_error(
            checks.LanguageServerUnavailable("error (Failed to start 1 language server(s): python)")
        )
        self.assertEqual(status, "FAIL")
        self.assertIn("symbol tools", detail)

    def test_language_server_report_omits_serena_text(self):
        # The status string embeds Serena's own exception, including workspace paths and LSP
        # parameters; none of it belongs in a probe report.
        _, detail = checks.classify_error(
            checks.LanguageServerUnavailable("error (/home/agent/private-root)")
        )
        self.assertNotIn("private-root", detail)

    def test_absent_language_server_status_is_missing_setup(self):
        for text in ("Active modes (2): editing", "Language server status: not initialized"):
            with self.subTest(text=text), self.assertRaises(checks.NeedsSetup):
                checks.serena_language_server(text)

    def test_worker_does_not_print_exception_secrets(self):
        output = io.StringIO()
        with patch("sys.argv", ["check", "--worker", "service", "--name", "search"]), \
                patch.object(checks, "service_probe", side_effect=RuntimeError("secret-fixture")), \
                contextlib.redirect_stdout(output):
            self.assertEqual(checks.main(), 0)
        self.assertEqual(json.loads(output.getvalue())[0], "FAIL")
        self.assertNotIn("secret-fixture", output.getvalue())

    def test_timeout_kills_probe_process_group(self):
        process = Mock(pid=12345)
        process.communicate.side_effect = subprocess.TimeoutExpired("probe", 1)
        with patch.object(checks.subprocess, "Popen", return_value=process), \
                patch.object(checks.os, "killpg") as kill:
            status, detail = checks.run_probe("mcp", "demo", False, Path("fixture"), 1)
        self.assertEqual(status, "FAIL")
        # One retry, and every attempt reaps its own process group: a timed-out probe leaves
        # Chromium or a language server behind otherwise, and both would be counted twice.
        self.assertEqual(kill.call_count, checks.PROBE_ATTEMPTS)
        self.assertEqual(process.wait.call_count, checks.PROBE_ATTEMPTS)
        self.assertIn(f"{checks.PROBE_ATTEMPTS} attempts", detail)

    def test_a_probe_that_answers_after_a_timeout_passes(self):
        # This is the cold start the retry exists for: the first window is spent competing with a
        # stack that is still coming up, and the service itself was never the problem.
        process = Mock(pid=12345, returncode=0)
        process.communicate.side_effect = [
            subprocess.TimeoutExpired("probe", 1),
            ('["PASS", "MCP initialized; 3 configured tools discovered"]', ""),
        ]
        with patch.object(checks.subprocess, "Popen", return_value=process), \
                patch.object(checks.os, "killpg"):
            status, detail = checks.run_probe("mcp", "demo", False, Path("fixture"), 1)
        self.assertEqual(status, "PASS")
        self.assertIn("retried", detail, "a borrowed second window has to be visible in the result")

    def test_nonzero_probe_is_not_a_pass(self):
        process = Mock(pid=12345, returncode=1)
        process.communicate.return_value = ("", "")
        spawn = Mock(return_value=process)
        with patch.object(checks.subprocess, "Popen", spawn), \
                patch.object(checks.os, "killpg"):
            status, _ = checks.run_probe("mcp", "demo", False, Path("fixture"), 1)
        self.assertEqual(status, "FAIL")
        # A probe that answered by failing is evidence, not a cold start: no second window.
        self.assertEqual(spawn.call_count, 1)

    def test_report_records_the_verdict_privately(self):
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / "service-check.json"
            checks.write_report(report, "full", [
                {"kind": "service", "name": "search", "status": "FAIL", "detail": "no results"},
                {"kind": "mcp", "name": "serena", "status": "PASS", "detail": "ready"},
            ], 1)
            self.assertEqual(os.stat(report).st_mode & 0o777, 0o600)
            document = json.loads(report.read_text())
            self.assertEqual(document["mode"], "full")
            self.assertEqual(document["exit_code"], 1)
            self.assertEqual(document["counts"], {"FAIL": 1, "PASS": 1})
            # Sorted so two passes of the same stack diff cleanly instead of shuffling.
            self.assertEqual([check["kind"] for check in document["checks"]], ["mcp", "service"])
            self.assertRegex(document["generated_at"], r"^\d{4}-\d\d-\d\dT")

    def test_report_flag_is_what_the_parent_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "mcp.json"
            config.write_text(json.dumps({"mcpServers": {}}))
            report = Path(directory) / "service-check.json"
            with patch.object(sys, "argv", ["check", "--config", str(config),
                                            "--report", str(report)]), \
                    patch.object(checks, "run_probe", return_value=("PASS", "ready")), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(checks.main(), 0)
            self.assertEqual(json.loads(report.read_text())["mode"], "quick")

    def test_a_pass_without_the_flag_writes_no_report(self):
        # The report is opt-in so the worker protocol stays a single JSON line on stdout.
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "mcp.json"
            config.write_text(json.dumps({"mcpServers": {}}))
            with patch.object(sys, "argv", ["check", "--config", str(config)]), \
                    patch.object(checks, "run_probe", return_value=("PASS", "ready")), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(checks.main(), 0)
            self.assertEqual(list(Path(directory).iterdir()), [config])


if __name__ == "__main__":
    unittest.main()
