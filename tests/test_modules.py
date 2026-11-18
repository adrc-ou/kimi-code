"""Exercise discovery, selection, session isolation, and non-destructive setup."""

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.test_mcp_compatibility import contract
from tools.modules import assemble, choose, discover, module_agents_text, ordered
from tools.safe_workspace_init import UnsafeWorkspace, initialize
from tools.tui.app import Result

ROOT = Path(__file__).resolve().parents[1]


class ModuleTests(unittest.TestCase):
    def build_harness(self, root):
        """The harness-side directories a launch reads, under whatever root is given.

        Used by :meth:`setUp` and by cases that need a second complete root: ``assemble()`` stages
        from the harness's own ``runtime/`` as well as from the modules, so a root holding only
        ``modules/`` is enough to probe ``discover()`` but not enough to reach an assembly refusal.
        """
        shutil.copytree(ROOT / "runtime", root / "runtime")
        (root / "tools").mkdir()
        workspace = root / "workspace"
        initialize(workspace)
        runtime = root / ".local/runtime/test"
        runtime.mkdir(parents=True)
        (runtime / "compose").mkdir()
        return runtime, workspace

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.runtime, self.workspace = self.build_harness(self.root)

    def module_in(self, root, name="demo", **fields):
        """One module directory under ``root``, valid unless the caller says otherwise."""
        path = Path(root) / "modules" / name
        path.mkdir(parents=True)
        doc = {"schema_version": 1, "label": name.title(), **fields}
        (path / "module.json").write_text(json.dumps(doc))
        (path / "module.sh").write_text("exit 0\n")
        return path

    def module(self, name="demo", **fields):
        return self.module_in(self.root, name, **fields)

    def isolated(self):
        """A root holding nothing but the modules one case puts in it.

        discover() walks every module and raises on the first refusal it meets, so cases that each
        expect a *different* sentence cannot share ``self.root``: the first case's bad module would
        answer for every later one, and the assertions would pass without their own guard firing.
        """
        root = Path(tempfile.mkdtemp(dir=self.temp.name))
        self.addCleanup(shutil.rmtree, root, True)
        return root

    def test_a_module_environment_declaration_without_a_name_is_a_named_refusal(self):
        # discover() read the prompt field with .get() and the name field with a subscript, so a
        # manifest missing "name" stopped the launch with a bare KeyError traceback instead of the
        # sentence that tells the operator which declaration is wrong.
        self.module(environment=[{"prompt": "Which port should the proxy use?"}])
        with self.assertRaisesRegex(ValueError, "Invalid module environment declaration"):
            discover(self.root)

    def test_a_malformed_manifest_is_refused_by_name_whatever_shape_it_has(self):
        # Each shape used to fail a different way: an unparseable file reached the operator as a
        # bare JSONDecodeError naming a byte offset, a non-string image as a TypeError out of
        # re.fullmatch, and a bare string in a list field iterated into its characters so the module
        # silently "declared" several entries. Each now answers the way the rest of discover() does,
        # naming the module.
        shapes = (
            ("unparseable", "{oops"),
            ("list instead of object", "[1, 2]"),
            ("label not a string", '{"schema_version": 1, "label": 7}'),
            ("images is a bare string", '{"schema_version": 1, "label": "D", "images": "one"}'),
            ("image not a string", '{"schema_version": 1, "label": "D", "images": [5]}'),
            (
                "environment is a mapping",
                '{"schema_version": 1, "label": "D", "environment": {"NAME": "p"}}',
            ),
        )
        for number, (label, text) in enumerate(shapes):
            with self.subTest(manifest=label):
                root = self.isolated()
                self.module_in(root, f"bad{number}").joinpath("module.json").write_text(
                    text, encoding="utf-8"
                )
                with self.assertRaisesRegex(
                    ValueError, "Invalid module manifest|Invalid image name"
                ):
                    discover(root)

    def test_every_discovery_refusal_says_which_rule_it_applied(self):
        # discover() raises ValueError for all of these, so an assertion on the type alone cannot
        # tell a working guard from an unrelated one three lines later — which is how six of them
        # went unwitnessed.
        def linked_module(root):
            self.module_in(root, "linkd")
            (root / "modules" / "linkd-lnk").symlink_to("linkd", target_is_directory=True)

        def symlink_inside(root):
            path = self.module_in(root, "insyn")
            (path / "runtime").mkdir()
            (path / "runtime" / "escape").symlink_to("/etc/passwd")

        cases = (
            (lambda r: self.module_in(r, "BAD"), "lowercase identifiers", "uppercase module id"),
            (linked_module, "lowercase identifiers", "symlinked module dir"),
            (symlink_inside, "Unsafe module entry", "symlink inside module"),
            (lambda r: os.mkfifo(self.module_in(r, "fifomod") / "pipe"),
             "Unsafe module entry", "fifo inside module"),
            (lambda r: self.module_in(r, "blank", label="   "),
             "Invalid module label", "blank label"),
            (lambda r: self.module_in(r, "ctrl", label="Bad\x01label"),
             "Invalid module label", "control character in label"),
            (lambda r: self.module_in(r, "img", images=["Bad Image!"]),
             "Invalid image name", "image with a space"),
            (lambda r: self.module_in(r, "noscript").joinpath("module.sh").unlink(),
             "Module requires module.sh", "module without its script"),
        )
        for build, refused, label in cases:
            with self.subTest(case=label):
                root = self.isolated()
                build(root)
                with self.assertRaisesRegex(ValueError, refused):
                    discover(root)

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

    def test_disabled_module_preserves_data_and_clears_its_staged_guidance(self):
        path = self.module(workspace_directories=["demo/user"])
        (path / "AGENTS.md").write_text("Use demo tools.\n")
        guidance = self.workspace / "AGENTS.md"
        guidance.write_text("# User guidance\nKeep this.\n")
        assemble(self.root, self.runtime, discover(self.root), self.workspace)
        data = self.workspace / "demo/user/data"
        data.write_text("keep")
        # Module text is staged for the system prompt instead: the workspace's own guidance
        # file belongs to the project being worked on and the harness never writes it, whether
        # a module is selected or not.
        staged = self.runtime / "module-guidance.md"
        self.assertIn("## Module: Demo\n\nUse demo tools.", staged.read_text())
        self.assertEqual(guidance.read_text(), "# User guidance\nKeep this.\n")
        assemble(self.root, self.runtime, [], self.workspace)
        self.assertEqual(staged.read_text(), "")
        self.assertEqual(guidance.read_text(), "# User guidance\nKeep this.\n")
        self.assertEqual(data.read_text(), "keep")

    def test_each_module_owns_a_heading_naming_it(self):
        for name, text in (("alpha", "Alpha way.\n"), ("beta", "Beta way.\n")):
            (self.module(name) / "AGENTS.md").write_text(text)
        guidance = module_agents_text(discover(self.root))
        self.assertIn("## Module: Alpha\n\nAlpha way.", guidance)
        self.assertLess(guidance.index("## Module: Alpha"), guidance.index("## Module: Beta"))

    def test_a_module_without_guidance_contributes_no_section(self):
        self.module()
        self.assertEqual(module_agents_text(discover(self.root)), "")
        self.assertEqual(module_agents_text([]), "")

    def test_an_unselected_module_contributes_nothing(self):
        path = self.module(workspace_directories=["demo/user"])
        (path / "AGENTS.md").write_text("Demo way.\n")
        (path / "runtime/tools").mkdir(parents=True)
        (path / "runtime/tools/democtl.py").write_text("exit 0\n")
        assemble(self.root, self.runtime, [], self.workspace)
        self.assertFalse((self.workspace / "demo").exists())
        self.assertFalse((self.runtime / "assets/tools/democtl.py").exists())
        self.assertEqual((self.runtime / "module-guidance.md").read_text(), "")
        assemble(self.root, self.runtime, discover(self.root), self.workspace)
        self.assertTrue((self.workspace / "demo/user").exists())
        self.assertTrue((self.runtime / "assets/tools/democtl.py").exists())
        self.assertIn("## Module: Demo", (self.runtime / "module-guidance.md").read_text())

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
        with self.assertRaisesRegex(ValueError, "Duplicate MCP server names"):
            assemble(self.root, self.runtime, discover(self.root), self.workspace)

    def test_each_merge_collision_is_refused_by_its_own_name(self):
        # assemble() raises ValueError for several different collisions, and a bare type assertion
        # cannot tell them apart -- the guard that matters can be deleted while a neighbour three
        # lines on keeps the test green. Each row builds the one input that must trip one guard.
        def two_modules_sharing_a_skill(root):
            for name in ("alpha", "beta"):
                self.module_in(root, name)
                directory = root / f"modules/{name}/runtime/skills/shared"
                directory.mkdir(parents=True)
                (directory / "SKILL.md").write_text("x", encoding="utf-8")

        def module_shadowing_a_harness_server(root):
            self.module_in(root, "alpha")
            (root / "modules/alpha/runtime").mkdir()
            (root / "modules/alpha/runtime/mcp.json").write_text(
                json.dumps({"mcpServers": {"serena": {"url": "https://example.invalid"}}}),
                encoding="utf-8",
            )

        def modules_declaring_the_same_server(root):
            for name, url in (("alpha", "https://a.invalid"), ("beta", "https://b.invalid")):
                self.module_in(root, name)
                (root / f"modules/{name}/runtime").mkdir()
                (root / f"modules/{name}/runtime/mcp.json").write_text(
                    json.dumps({"mcpServers": {"shared": {"url": url}}}), encoding="utf-8"
                )

        def contracts_without_a_configuration(root):
            path = self.module_in(root, "alpha")
            (path / "runtime").mkdir()
            (path / "runtime/mcp-contracts.json").write_text(
                json.dumps({"schema_version": 1, "servers": {}}), encoding="utf-8"
            )

        for build, refused, label in (
            (two_modules_sharing_a_skill, "Duplicate runtime asset", "same skill twice"),
            (
                module_shadowing_a_harness_server,
                "Duplicate MCP server names",
                "module shadows harness",
            ),
            (modules_declaring_the_same_server, "Duplicate MCP server names", "same server twice"),
            (contracts_without_a_configuration, "owning mcp.json", "contracts without config"),
        ):
            with self.subTest(case=label):
                root = self.isolated()
                runtime, workspace = self.build_harness(root)
                build(root)
                with self.assertRaisesRegex(ValueError, refused):
                    assemble(root, runtime, discover(root), workspace)

    def test_a_module_mcp_manifest_is_read_as_data_not_as_a_promise(self):
        # Each shape below stopped the launcher with a KeyError, TypeError or AttributeError,
        # none of which the CLI's refusal handler catches, so a module author's typo printed a
        # traceback where the contract reader one call later already prints a sentence.
        shapes = (
            ("empty object", "{}"),
            ("a list", "[]"),
            ("wrong key", '{"servers": {}}'),
            ("servers is a list", '{"mcpServers": ["a"]}'),
            ("unparseable", "{oops"),
        )
        for label, text in shapes:
            with self.subTest(manifest=label):
                root = self.isolated()
                runtime, workspace = self.build_harness(root)
                module = self.module_in(root, "demo")
                (module / "runtime").mkdir()
                (module / "runtime" / "mcp.json").write_text(text, encoding="utf-8")
                with self.assertRaisesRegex(
                    ValueError, "invalid MCP configuration|has no mcpServers object"
                ):
                    assemble(root, runtime, discover(root), workspace)

    def test_module_contract_ownership_staging_and_deselection(self):
        path = self.module()
        runtime = path / "runtime"
        runtime.mkdir()
        config = {"url": "https://example.invalid", "enabledTools": ["read"]}
        (runtime / "mcp.json").write_text(json.dumps({"mcpServers": {"demo": config}}))
        manifest = {"schema_version": 1, "servers": {"demo": contract(config)}}
        (runtime / "mcp-contracts.json").write_text(json.dumps(manifest))
        assemble(self.root, self.runtime, discover(self.root), self.workspace)
        staged = self.runtime / "assets/mcp-contracts.json"
        self.assertIn("demo", json.loads(staged.read_text())["servers"])
        assemble(self.root, self.runtime, [], self.workspace)
        self.assertNotIn("demo", json.loads(staged.read_text())["servers"])
        manifest["servers"]["foreign"] = manifest["servers"].pop("demo")
        (runtime / "mcp-contracts.json").write_text(json.dumps(manifest))
        with self.assertRaisesRegex(ValueError, "contract"):
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
        # A value with leading and trailing spaces in it on purpose: this environment never
        # stripped what the user typed, and a secret whose spaces were trimmed would not match
        # the file it was quoted into.
        token = " quote'${NRP_API_KEY}`literal` "
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
            patch("tools.modules.run", return_value=Result(value=token)) as asked,
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
        # What was asked, not just what was stored: a declared secret is hidden while it is typed,
        # and the spaces above survive only because this interface does not treat them as nothing.
        step = asked.call_args.args[0]
        self.assertTrue(step.masked)
        self.assertTrue(step.complete(token))
        self.assertEqual(asked.call_args.args[1].total, 1)
