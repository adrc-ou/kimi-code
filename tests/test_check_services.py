import contextlib
import io
import json
import subprocess
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
            status, _ = checks.run_probe("mcp", "demo", False, Path("fixture"), 1)
        self.assertEqual(status, "FAIL")
        kill.assert_called_once_with(12345, checks.signal.SIGKILL)
        process.wait.assert_called_once()

    def test_nonzero_probe_is_not_a_pass(self):
        process = Mock(pid=12345, returncode=1)
        process.communicate.return_value = ("", "")
        with patch.object(checks.subprocess, "Popen", return_value=process), \
                patch.object(checks.os, "killpg"):
            status, _ = checks.run_probe("mcp", "demo", False, Path("fixture"), 1)
        self.assertEqual(status, "FAIL")


if __name__ == "__main__":
    unittest.main()
