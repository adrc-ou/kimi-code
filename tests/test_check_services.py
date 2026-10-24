import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from tools import check_services as checks


class ServiceCheckTests(unittest.TestCase):
    def test_discovery_runs_module_probes_without_treating_core_helpers_as_services(self):
        # Stage core helpers alongside a real module probe, as modules.assemble does. Only the
        # built-in network probes are stubbed; every discovered file goes through a real worker.
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory)
            for source in Path(checks.__file__).parent.glob("*.py"):
                shutil.copy2(source, runtime / source.name)
            (runtime / "service_fixture.py").write_text(
                'def probe(full):\n    return "fixture full" if full else "fixture quick"\n'
            )
            config, report = runtime / "mcp.json", runtime / "report.json"
            config.write_text(json.dumps({"mcpServers": {}}))
            builtins = {"kimi-web", "model-proxy", "search", "local-tools"}
            run_probe = checks.run_probe

            def probe(kind, name, *args):
                if kind == "service" and name in builtins:
                    return {"status": "PASS", "detail": "fixture HTTP ready"}
                return run_probe(kind, name, *args)

            for full in (False, True):
                argv = ["check", "--config", str(config), "--report", str(report)]
                if full:
                    argv.append("--full")
                output = io.StringIO()
                with patch.object(checks, "__file__", str(runtime / "check_services.py")), \
                        patch.object(checks, "run_probe", side_effect=probe), \
                        patch.object(sys, "argv", argv), contextlib.redirect_stdout(output):
                    self.assertEqual(checks.main(), 0, output.getvalue())
                document = json.loads(report.read_text())
                self.assertEqual({item["name"] for item in document["checks"]},
                                 builtins | {"fixture"})
                finding = next(item for item in document["checks"] if item["name"] == "fixture")
                self.assertEqual(finding["detail"], "fixture full" if full else "fixture quick")

    def test_setup_survives_async_transport_exception_groups(self):
        error = ExceptionGroup("transport", [
            ExceptionGroup("session", [checks.NeedsSetup("private project name")])
        ])
        status, detail = checks.classify_error(error)
        self.assertEqual(status, "SETUP")
        self.assertNotIn("private project name", detail)

    def test_bearer_verdict_names_the_variable_and_never_a_value(self):
        status, detail = checks.classify_error(checks.UnmountedBearerVariable("CTX_KEY"))
        self.assertEqual(status, "SETUP")
        self.assertIn("CTX_KEY", detail)
        self.assertIn("bearerTokenEnvVar", detail)

    def test_declared_but_empty_bearer_variable_is_not_a_pass(self):
        # An anonymous probe connects happily to a server Kimi will never mount, so the empty
        # variable has to become the finding instead of a green check the operator trusts.
        for value in ("", "   "):
            with self.subTest(value=value):
                self.assertEqual(
                    checks.unmounted_bearer_variable({"bearerTokenEnvVar": "CTX_KEY"},
                                                     {"CTX_KEY": value}),
                    "CTX_KEY",
                )
        self.assertIsNone(
            checks.unmounted_bearer_variable({"bearerTokenEnvVar": "CTX_KEY"}, {"CTX_KEY": "k"})
        )

    def test_keyless_server_has_nothing_to_fail_closed_on(self):
        self.assertIsNone(
            checks.unmounted_bearer_variable({"url": "https://example.invalid"}, {})
        )

    def test_allowlist_and_blocklist_match_exposed_tools(self):
        tools = [SimpleNamespace(name=name) for name in ("read", "write", "list")]
        self.assertEqual(checks.selected_tools(
            {"enabledTools": ["read", "list"], "disabledTools": ["list"]}, tools
        ), {"read"})

    def test_missing_expected_tool_fails(self):
        with self.assertRaises(checks.MissingConfiguredTools):
            checks.selected_tools({"enabledTools": ["missing"]}, [])

    def test_catalog_drift_names_missing_tool_inside_transport_groups(self):
        tools = [SimpleNamespace(name=name) for name in (
            "read_wiki_structure", "read_wiki_contents", "ask_wiki_question"
        )]
        with self.assertRaises(checks.MissingConfiguredTools) as raised:
            checks.selected_tools({"enabledTools": ["ask_question"]}, tools)
        status, detail = checks.classify_error(ExceptionGroup("transport", [raised.exception]))
        self.assertEqual(status, "FAIL")
        self.assertIn("ask_question", detail)
        self.assertIn("catalog", detail)

    def test_catalog_diagnostic_bounds_and_sanitizes_untrusted_names(self):
        _, detail = checks.classify_error(
            checks.MissingConfiguredTools(["tool\nforged PASS", "x" * 10000])
        )
        self.assertNotIn("\n", detail)
        self.assertNotIn("forged PASS", detail)
        self.assertLess(len(detail), 300)

    def test_search_readiness_does_not_claim_a_successful_query(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = b'{"status":"ok"}'
        with patch("urllib.request.urlopen", return_value=response):
            detail = checks.service_probe("search", False)
        self.assertIn("adapter HTTP ready", detail)
        self.assertIn("full mode", detail)

    def test_empty_authenticated_search_has_an_actionable_failure(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = b'{"search_results":[]}'
        with patch.dict(os.environ, {"KIMI_WEB_SEARCH_BASE_URL": "http://search/search",
                                    "KIMI_WEB_SEARCH_API_KEY": "test-token"}), \
                patch("urllib.request.urlopen", return_value=response), \
                self.assertRaises(checks.SearchUnavailable) as raised:
            checks.service_probe("search", True)
        status, detail = checks.classify_error(raised.exception)
        self.assertEqual(status, "FAIL")
        self.assertIn("no results", detail)
        self.assertIn("CAPTCHAs", detail)

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
        self.assertEqual(json.loads(output.getvalue())["status"], "FAIL")
        self.assertNotIn("secret-fixture", output.getvalue())

    def test_timeout_kills_probe_process_group(self):
        process = Mock(pid=12345)
        process.communicate.side_effect = subprocess.TimeoutExpired("probe", 1)
        with patch.object(checks.subprocess, "Popen", return_value=process), \
                patch.object(checks.os, "killpg") as kill:
            result = checks.run_probe("mcp", "demo", False, Path("fixture"), 1)
        self.assertEqual(result["status"], "FAIL")
        # One retry, and every attempt reaps its own process group: a timed-out probe leaves
        # Chromium or a language server behind otherwise, and both would be counted twice.
        self.assertEqual(kill.call_count, checks.PROBE_ATTEMPTS)
        self.assertEqual(process.wait.call_count, checks.PROBE_ATTEMPTS)
        self.assertIn(f"{checks.PROBE_ATTEMPTS} attempts", result["detail"])
        self.assertEqual(result["compatibility"]["verdict"], "unverified")

    def test_a_probe_that_answers_after_a_timeout_passes(self):
        # This is the cold start the retry exists for: the first window is spent competing with a
        # stack that is still coming up, and the service itself was never the problem.
        process = Mock(pid=12345, returncode=0)
        process.communicate.side_effect = [
            subprocess.TimeoutExpired("probe", 1),
            (json.dumps({"status": "PASS", "detail": "MCP initialized; 3 tools discovered",
                         "compatibility": {"verdict": "matched"}}), ""),
        ]
        with patch.object(checks.subprocess, "Popen", return_value=process), \
                patch.object(checks.os, "killpg"):
            result = checks.run_probe("mcp", "demo", False, Path("fixture"), 1)
        self.assertEqual(result["status"], "PASS")
        self.assertIn("retried", result["detail"], "a borrowed second window must be visible")

    def test_nonzero_probe_is_not_a_pass(self):
        process = Mock(pid=12345, returncode=1)
        process.communicate.return_value = ("", "")
        spawn = Mock(return_value=process)
        with patch.object(checks.subprocess, "Popen", spawn), \
                patch.object(checks.os, "killpg"):
            result = checks.run_probe("mcp", "demo", False, Path("fixture"), 1)
        self.assertEqual(result["status"], "FAIL")
        # A probe that answered by failing is evidence, not a cold start: no second window.
        self.assertEqual(spawn.call_count, 1)

    def test_mcp_pass_without_a_verified_compatibility_result_is_rejected(self):
        for finding in (None, {"verdict": "unverified"}):
            payload = {"status": "PASS", "detail": "discovered", "compatibility": finding}
            process = Mock(pid=12345, returncode=0)
            process.communicate.return_value = (json.dumps(payload), "")
            with patch.object(checks.subprocess, "Popen", return_value=process), \
                    patch.object(checks.os, "killpg"):
                result = checks.run_probe("mcp", "demo", False, Path("fixture"), 1)
            self.assertEqual(result["status"], "FAIL")
            self.assertEqual(result["compatibility"]["verdict"], "unverified")

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
                    patch.object(checks, "run_probe", return_value={
                        "status": "PASS", "detail": "ready"}), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(checks.main(), 0)
            self.assertEqual(json.loads(report.read_text())["mode"], "quick")

    def test_a_pass_without_the_flag_writes_no_report(self):
        # The report is opt-in so the worker protocol stays a single JSON line on stdout.
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "mcp.json"
            config.write_text(json.dumps({"mcpServers": {}}))
            with patch.object(sys, "argv", ["check", "--config", str(config)]), \
                    patch.object(checks, "run_probe", return_value={
                        "status": "PASS", "detail": "ready"}), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(checks.main(), 0)
            self.assertEqual(list(Path(directory).iterdir()), [config])

    def test_disabled_and_oauth_servers_are_explicitly_not_verified_without_discovery(self):
        with tempfile.TemporaryDirectory() as directory:
            config, report = (Path(directory) / name for name in ("mcp.json", "report.json"))
            config.write_text(json.dumps({"mcpServers": {
                "disabled": {"url": "https://example.invalid", "enabled": False},
                "oauth": {"url": "https://example.invalid", "auth": "oauth"},
            }}))
            with patch.object(sys, "argv", ["check", "--config", str(config),
                                            "--compatibility-only", "--report", str(report)]), \
                    patch.object(checks, "run_probe") as probe, \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(checks.main(), 2)
            probe.assert_not_called()
            findings = json.loads(report.read_text())["checks"]
            self.assertEqual({item["compatibility"]["verdict"] for item in findings},
                             {"skipped", "manual"})


if __name__ == "__main__":
    unittest.main()
