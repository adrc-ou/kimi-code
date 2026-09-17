"""Exercise discovery, selection, session isolation, and non-destructive setup."""

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.modules import assemble, choose, discover, merge_guidance, ordered
from tools.safe_workspace_init import UnsafeWorkspace, initialize

ROOT = Path(__file__).resolve().parents[1]


class ModuleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.runtime = self.root / ".local/runtime/test"
        self.runtime.mkdir(parents=True)
        (self.runtime / "compose").mkdir()
        self.workspace = self.root / "workspace"
        initialize(self.workspace)
        shutil.copytree(ROOT / "runtime", self.root / "runtime")
        (self.root / "tools").mkdir()

    def module(self, name="demo", **fields):
        path = self.root / "modules" / name
        path.mkdir(parents=True)
        doc = {"schema_version": 1, "label": name.title(), **fields}
        (path / "module.json").write_text(json.dumps(doc))
        (path / "module.sh").write_text("exit 0\n")
        return path

    def test_discovery_is_drop_in_and_removal_is_automatic(self):
        self.assertEqual(discover(self.root), [])
        path = self.module()
        self.assertEqual([m["id"] for m in discover(self.root)], ["demo"])
        shutil.rmtree(path)
        self.assertEqual(discover(self.root), [])

    def test_previous_enabled_first_then_alphabetical(self):
        modules = [{"id": n, "label": n.title()} for n in ["z", "a", "b"]]
        self.assertEqual([m["id"] for m in ordered(modules, ["z"])], ["z", "a", "b"])
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(choose(modules, ["z", "deleted"], True), ["z"])
            self.assertEqual(choose([], ["z"], False), [])

    def test_explicit_selection_rejects_unavailable_modules(self):
        with patch.dict(os.environ, {"HARNESS_MODULES": "missing"}):
            with self.assertRaises(ValueError):
                choose([], [], True)

    def test_disabled_module_preserves_data_and_removes_only_managed_guidance(self):
        path = self.module(workspace_directories=["demo/user"])
        (path / "AGENTS.md").write_text("Use demo tools.\n")
        guidance = self.workspace / "AGENTS.md"
        guidance.write_text("# User guidance\nKeep this.\n")
        assemble(self.root, self.runtime, discover(self.root), self.workspace)
        data = self.workspace / "demo/user/data"
        data.write_text("keep")
        first = guidance.read_text()
        assemble(self.root, self.runtime, discover(self.root), self.workspace)
        self.assertEqual(guidance.read_text(), first)
        assemble(self.root, self.runtime, [], self.workspace)
        self.assertEqual(guidance.read_text(), "# User guidance\nKeep this.\n")
        self.assertEqual(data.read_text(), "keep")

    def test_core_setup_does_not_create_comfyui(self):
        assemble(self.root, self.runtime, [], self.workspace)
        self.assertFalse((self.workspace / "comfyui").exists())
        self.assertFalse((self.runtime / "assets/tools/comfyctl.py").exists())

    def test_guidance_rejects_symlink_and_hardlink(self):
        target = self.root / "outside"
        target.write_text("keep")
        guidance = self.workspace / "AGENTS.md"
        guidance.symlink_to(target)
        with self.assertRaises(UnsafeWorkspace):
            merge_guidance(self.workspace, "bad")
        guidance.unlink()
        os.link(target, guidance)
        with self.assertRaises(UnsafeWorkspace):
            merge_guidance(self.workspace, "bad")
        self.assertEqual(target.read_text(), "keep")

    def test_module_directories_cannot_escape_or_follow_links(self):
        self.module(workspace_directories=["../escape"])
        with self.assertRaises(UnsafeWorkspace):
            discover(self.root)

    def test_assets_and_mcp_are_selected_and_collisions_fail(self):
        path = self.module()
        (path / "runtime/skills/demo").mkdir(parents=True)
        (path / "runtime/skills/demo/SKILL.md").write_text("Demo")
        (path / "runtime/mcp.json").write_text(
            json.dumps({"mcpServers": {"demo": {"url": "https://example.invalid"}}})
        )
        assemble(self.root, self.runtime, discover(self.root), self.workspace)
        self.assertTrue((self.runtime / "assets/skills/demo/SKILL.md").exists())
        self.assertIn(
            "demo", json.loads((self.runtime / "assets/mcp.json").read_text())["mcpServers"]
        )
        (path / "runtime/mcp.json").write_text(json.dumps({"mcpServers": {"serena": {}}}))
        with self.assertRaises(ValueError):
            assemble(self.root, self.runtime, discover(self.root), self.workspace)

    def test_missing_environment_fails_closed_and_session_value_is_not_persisted(self):
        self.module(environment=[{"name": "DEMO_TOKEN", "prompt": "Demo token", "agent": True}])
        (self.runtime / "modules.json").write_text('["demo"]')
        (self.root / ".env").write_text("")
        bootstrap = self.runtime / "bootstrap"
        bootstrap.write_text("DEMO_TOKEN=host-inherited-value\n")
        env = {
            **os.environ,
            "HARNESS_ROOT": str(self.root),
            "HARNESS_RUNTIME_DIR": str(self.runtime),
            "HARNESS_RESOLVED_BOOTSTRAP": str(bootstrap),
        }
        command = ["python3", str(ROOT / "tools/modules.py"), "environment", "--non-interactive"]
        result = subprocess.run(command, env=env, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("host-inherited-value", result.stderr)
        (self.root / ".env").write_text("DEMO_TOKEN=example\n")
        bootstrap.write_text("DEMO_TOKEN=example\n")
        result = subprocess.run(command, env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.runtime / "module.env").stat().st_mode & 0o777, 0o600)
        self.assertFalse((self.runtime / "state.env").exists())

    def test_checkbox_menu_arrow_space_enter_and_terminal_restoration(self):
        import pty
        import select
        import termios
        import time

        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        before = termios.tcgetattr(slave)
        script = (
            "import json; from tools.modules import choose; "
            'print("RESULT=" + json.dumps(choose('
            '[{"id":"alpha","label":"Alpha"},{"id":"beta","label":"Beta"}], [], False)))'
        )
        env = {k: v for k, v in os.environ.items() if k != "HARNESS_MODULES"}
        process = subprocess.Popen(
            ["python3", "-c", script], cwd=ROOT, env=env, stdin=slave, stdout=slave, stderr=slave
        )

        def reap():
            if process.poll() is None:
                process.kill()
            process.wait()

        self.addCleanup(reap)
        output = b""
        deadline = time.monotonic() + 5
        while b"[ ] Beta" not in output and time.monotonic() < deadline:
            if select.select([master], [], [], 0.1)[0]:
                output += os.read(master, 4096)
        self.assertIn(b"[ ] Beta", output)
        os.write(master, b"\x1b[B \r")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if select.select([master], [], [], 0.1)[0]:
                output += os.read(master, 4096)
            if process.poll() is not None:
                break
        process.wait(timeout=1)
        self.assertIn(b'RESULT=["beta"]', output)
        after = termios.tcgetattr(slave)
        # macOS may set PENDIN while applying attributes; it is transient driver state.
        transient = getattr(termios, "PENDIN", 0)
        after[3] &= ~transient
        before[3] &= ~transient
        self.assertEqual(after, before)

    def test_uninstall_reclaims_only_private_installation_without_following_data_links(self):
        from tools.modules import reconcile_installed

        data = self.runtime / "module-data/demo"
        data.mkdir(parents=True)
        persistent = self.workspace / "user-data"
        persistent.mkdir()
        (persistent / "keep").write_text("keep")
        (data / "user").symlink_to(persistent, target_is_directory=True)
        (self.runtime / "installed-modules.json").write_text('{"demo": []}')
        reconcile_installed(self.runtime, [])
        self.assertFalse(data.exists())
        self.assertEqual((persistent / "keep").read_text(), "keep")

    def test_interactive_env_is_quoted_and_compose_dollars_are_literal(self):
        from tools.modules import main

        self.module(environment=[{"name": "DEMO_TOKEN", "prompt": "Token", "agent": True}])
        (self.runtime / "modules.json").write_text('["demo"]')
        (self.root / ".env").write_text("")
        bootstrap = self.runtime / "bootstrap"
        bootstrap.write_text("")
        token = "quote'${NRP_API_KEY}`literal`"
        with (
            patch.dict(
                os.environ,
                {
                    "HARNESS_ROOT": str(self.root),
                    "HARNESS_RUNTIME_DIR": str(self.runtime),
                    "HARNESS_RESOLVED_BOOTSTRAP": str(bootstrap),
                },
            ),
            patch("sys.argv", ["modules.py", "environment"]),
            patch("sys.stdin.isatty", return_value=True),
            patch("getpass.getpass", return_value=token),
        ):
            main()
        result = subprocess.run(
            [
                "bash",
                "-c",
                'source "$1"; printf "%s" "$DEMO_TOKEN"',
                "bash",
                str(self.runtime / "module.env"),
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(result.stdout, token)
        overlay = json.loads((self.runtime / "compose/module-environment.json").read_text())
        self.assertEqual(
            overlay["services"]["kimi-agent"]["environment"]["DEMO_TOKEN"], token.replace("$", "$$")
        )
        self.assertEqual((self.root / ".env").read_text(), "")
