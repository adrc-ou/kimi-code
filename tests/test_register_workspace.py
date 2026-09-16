"""Verify the authenticated registration contract without operator state."""

import contextlib
import importlib.util
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("register_workspace", ROOT / "tools/register_workspace.py")
registration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(registration)


class RegistrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        home = Path(temporary.name)
        (home / "server.token").write_text("fixture-private-token\n")
        self.env = patch.dict(os.environ, {"KIMI_CODE_HOME": str(home)})
        self.env.start()
        self.addCleanup(self.env.stop)

    def run_registration(self, payload):
        with patch.object(registration.urllib.request, "build_opener") as build:
            build.return_value.open.return_value = io.BytesIO(json.dumps(payload).encode())
            with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()) as err:
                status = registration.main()
            request = build.return_value.open.call_args.args[0]
            return status, out.getvalue() + err.getvalue(), request, build.call_args.args

    def test_authenticated_registration_uses_fixed_workspace_and_no_proxy(self):
        status, output, request, handlers = self.run_registration(
            {"code": 0, "data": {"root": "/workspace", "id": "workspace-id"}}
        )
        self.assertEqual(status, 0)
        self.assertEqual(request.full_url, "http://127.0.0.1:5494/api/v1/workspaces")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(json.loads(request.data), {"root": "/workspace"})
        self.assertEqual(request.get_header("Authorization"), "Bearer fixture-private-token")
        self.assertEqual(handlers[0].proxies, {})
        self.assertIsNone(handlers[1].redirect_request(request, None, 302, "", {}, "https://example.invalid"))
        self.assertNotIn("fixture-private-token", output)

    def test_error_envelopes_and_wrong_workspace_fail_without_response_disclosure(self):
        for payload in (
            {"code": 40101, "msg": "fixture-private-token"},
            {"code": 0, "data": {"root": "/home/agent", "id": "other"}},
            {"code": 0, "data": {}},
            [],
        ):
            with self.subTest(payload=payload):
                status, output, _, _ = self.run_registration(payload)
                self.assertEqual(status, 1)
                self.assertNotIn("fixture-private-token", output)

    def test_missing_credential_fails_without_request(self):
        Path(os.environ["KIMI_CODE_HOME"], "server.token").unlink()
        with patch.object(registration.urllib.request, "build_opener") as build:
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(registration.main(), 1)
            build.assert_not_called()


if __name__ == "__main__":
    unittest.main()
