"""Verify the authenticated registration contract without operator state."""

import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
# The repository root, so that `tests.helpers` resolves under every way of running the suite.
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.helpers import tracked_files  # noqa: E402

spec = importlib.util.spec_from_file_location(
    "register_workspace", ROOT / "tools/register_workspace.py"
)
registration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(registration)


class RegistrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        home = Path(temporary.name)
        (home / "server.token").write_text("fixture-private-token\n")
        # The scratch directory is an absolute path in the container; a test that ran main()
        # unpointed would rebuild the real one and take any in-flight session's notes with it.
        self.env = patch.dict(
            os.environ,
            {
                "KIMI_CODE_HOME": str(home),
                "KIMI_AGENT_STATE_DIR": str(home / "agent-state"),
            },
        )
        self.env.start()
        self.addCleanup(self.env.stop)

    def run_registration(self, payload):
        with patch.object(registration.urllib.request, "build_opener") as build:
            build.return_value.open.return_value = io.BytesIO(json.dumps(payload).encode())
            with (
                contextlib.redirect_stdout(io.StringIO()) as out,
                contextlib.redirect_stderr(io.StringIO()) as err,
            ):
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
        self.assertIsNone(
            handlers[1].redirect_request(request, None, 302, "", {}, "https://example.invalid")
        )
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


class ScratchTests(unittest.TestCase):
    """The agent's working memory is container scratch, staged empty on every launch."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name) / "agent-state"

    def test_stages_the_layout_the_operating_contract_names(self):
        prepared = registration.prepare_agent_state(self.directory)
        self.assertEqual(prepared, str(self.directory))
        self.assertIn("## Objective", (self.directory / "STATE.md").read_text())
        for name in registration.LEDGER_FILES:
            self.assertTrue((self.directory / name).is_file(), name)
        self.assertTrue((self.directory / "logs").is_dir())
        self.assertEqual(os.stat(self.directory).st_mode & 0o777, 0o700)

    def test_a_second_launch_drops_what_the_first_left(self):
        registration.prepare_agent_state(self.directory)
        (self.directory / "STATE.md").write_text("last session's notion of progress\n")
        (self.directory / "stale-run.log").write_text("evidence nobody asked for\n")
        registration.prepare_agent_state(self.directory)
        self.assertIn("## Objective", (self.directory / "STATE.md").read_text())
        self.assertFalse((self.directory / "stale-run.log").exists())

    def test_replaces_a_link_rather_than_following_it(self):
        outside = self.directory.parent / "outside"
        outside.mkdir()
        (outside / "keep.txt").write_text("not scratch\n")
        self.directory.symlink_to(outside, target_is_directory=True)
        registration.prepare_agent_state(self.directory)
        self.assertTrue(self.directory.is_dir())
        # The point of unlinking the link instead of descending through it: whatever is on the
        # other side is not scratch and is not this function's to remove.
        self.assertTrue((outside / "keep.txt").exists())
        self.assertTrue((self.directory / "STATE.md").is_file())

    def test_the_container_path_is_the_one_the_contract_tells_agents_to_use(self):
        # The prose and the code have exactly one chance to agree, which is here. Every expected
        # string is built from the constant the seeder actually uses, so the assertion compares
        # the documents against the code rather than against a second copy of the same literal.
        scratch = str(registration.AGENT_STATE_DIR)
        contract = (ROOT / "runtime" / "AGENTS.md").read_text()
        self.assertIn(f"{scratch}/STATE.md", contract)
        self.assertIn(f"{scratch}/{registration.LOGS_DIR}", contract)
        for name in registration.LEDGER_FILES:
            self.assertIn(f"{scratch}/{name}", contract)
        self.assertNotIn(".agent-state", contract)
        for document in tracked_files(".md"):
            if document.name == "SKILL.md":
                self.assertNotIn(".agent-state", document.read_text(), str(document))


if __name__ == "__main__":
    unittest.main()
