import json
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(ROOT / "tools"))

import policy  # noqa: E402
import render_runtime  # noqa: E402


class LaunchSteps:
    """The two launch steps that produce Kimi's configuration, driven against the shipped trees.

    A plain mixin rather than a ``TestCase`` so a second case class can reuse the steps without
    inheriting - and so re-running - the first one's tests.
    """

    #: What the launcher would have read out of ``.env`` for the case under test.
    PROVIDER_ONLY = "NRP_API_KEY=provider-secret\n"
    MODEL_SCOPED = "QWEN3_API_KEY=model-secret\nNRP_API_KEY=provider-secret\n"

    def fixture(
        self, directory: Path, values: str = PROVIDER_ONLY, omit: str | None = None
    ) -> Path:
        """A resolved plan, plus the resolved ``.env`` the renderer is asked to read.

        ``omit`` is the literal text the operator put after the
        ``KIMI_SYSTEM_PROMPT_OMIT_ENVELOPE=`` line; ``None`` leaves the name out of the file
        entirely, which is how an operator who never set the flag looks to the launcher.
        """
        state = directory / "state"
        workspace = directory / "workspace"
        workspace.mkdir()
        resolved = directory / "resolved.env"
        text = f"{values}NRP_BASE_URL=\nKIMI_BACKGROUND_TASK_SLOTS=\n"
        if omit is not None:
            text += f"{render_runtime.OMIT_ENVELOPE_FLAG}={omit}\n"
        resolved.write_text(text)
        environment = {
            **os.environ,
            "HARNESS_ROOT": str(ROOT),
            "HARNESS_RUNTIME_DIR": str(state),
            "HARNESS_WORKSPACE": str(workspace),
            "HARNESS_RESOLVED_BOOTSTRAP": str(resolved),
        }
        for action in ("select", "resolve"):
            subprocess.run(
                ["python3", str(ROOT / "tools" / "models.py"), action, "--non-interactive"],
                check=True,
                capture_output=True,
                env=environment,
            )
        return state

    def render(self, state: Path, resolved: Path, root: Path = ROOT) -> None:
        subprocess.run(
            [
                "python3",
                str(ROOT / "tools" / "render_runtime.py"),
                "--root",
                str(root),
                "--runtime-dir",
                str(state),
                "--resolved-env",
                str(resolved),
            ],
            check=True,
            capture_output=True,
            # A credential with no value anywhere makes the renderer ask. Under test there is
            # nobody to answer, and inheriting a terminal would block the suite on a prompt.
            stdin=subprocess.DEVNULL,
        )

class RenderRuntimeTests(LaunchSteps, unittest.TestCase):
    """The contract that a plan written by the resolver is consumable by the renderer, and
    that no credential value leaks into a file the agent can read."""

    def test_tokens_rotate_but_cache_salt_persists(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            state = self.fixture(base)
            resolved = base / "resolved.env"
            self.render(state, resolved)
            first_token = (state / "proxy-token").read_text()
            first_salt = (state / "cache-salt").read_text()
            self.render(state, resolved)
            self.assertNotEqual(first_token, (state / "proxy-token").read_text())
            self.assertEqual(first_salt, (state / "cache-salt").read_text())
            self.assertEqual(len(first_salt), 43)
            config = (state / "kimi-config.toml").read_text()
            self.assertNotIn("__MODEL_PROXY_TOKEN__", config)
            # The default model is whatever the primary selection's alias is, so this asserts
            # the shape of the generated name rather than a model this test hard-codes.
            self.assertRegex(config, r'default_model = "[a-z0-9]+-primary"')

    def test_credential_is_a_mode_0600_file_named_by_the_definition(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            state = self.fixture(base)
            self.render(state, base / "resolved.env")
            secret = state / "credentials" / "nrp__default"
            self.assertEqual(secret.read_text(), "provider-secret")
            self.assertEqual(os.stat(secret).st_mode & 0o777, 0o600)
            self.assertEqual(os.stat(state / "credentials").st_mode & 0o777, 0o700)
            # The value must not appear in anything the agent's container can read.
            for name in ("kimi-config.toml", "runtime.env", "model-policy.json", "SYSTEM.md"):
                self.assertNotIn("provider-secret", (state / name).read_text())
            fragment = (state / "compose" / "models.json").read_text()
            self.assertIn("nrp__default", fragment)
            self.assertNotIn("provider-secret", fragment)

    def test_generated_tables_come_from_the_plan_not_the_baseline(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            state = self.fixture(base)
            self.render(state, base / "resolved.env")
            config = tomllib.loads((state / "kimi-config.toml").read_text())
            plan = __import__("json").loads((state / "model-policy.json").read_text())
            secondary = config["secondary_model"]
            self.assertTrue(secondary["force"])
            self.assertEqual(secondary["default_model"], plan["lanes"]["subagent"]["alias"])
            for lane, entry in plan["lanes"].items():
                table = config["models"][entry["alias"]]
                self.assertEqual(table["max_context_size"], entry["context_tokens"])
                self.assertEqual(table["max_input_size"], entry["input_tokens"])
                provider = config["providers"][entry["provider_name"]]
                self.assertEqual(provider["base_url"], f"http://model-proxy:8080/{lane}/v1")

    def test_runtime_environment_exposes_only_the_staging_inputs(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            state = self.fixture(base)
            self.render(state, base / "resolved.env")
            published = (state / "runtime.env").read_text()
            self.assertIn("KIMI_RENDERED_CONFIG=", published)
            self.assertIn("KIMI_SYSTEM_MD=", published)
            self.assertIn("MODEL_PROXY_POLICY_FILE=", published)
            self.assertNotIn("KIMI_EMPTY", published)
            self.assertFalse((state / "user-agents").exists())

    def test_renderer_refuses_a_missing_plan(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state"
            resolved = Path(directory) / "resolved.env"
            resolved.write_text("")
            with self.assertRaises(subprocess.CalledProcessError) as caught:
                self.render(state, resolved)
            self.assertIn("model-policy.json", caught.exception.stderr.decode())


class ModelScopedKeyTests(LaunchSteps, unittest.TestCase):
    """Which key a model authenticates with, and what that does to the plan.

    A model that names a variable and has a value for it becomes its own upstream identity:
    its own secret file, and - because credential-scoped provider rules are counted per key -
    its own ledger. A model whose own variable is blank or absent keeps sharing the
    provider-scoped credential, which is what makes the two scopes an operator's choice.
    """

    def test_a_model_with_its_own_key_gets_its_own_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            state = self.fixture(base, self.MODEL_SCOPED)
            self.render(state, base / "resolved.env")
            plan = json.loads((state / "model-policy.json").read_text())
            scoped = plan["lanes"]["primary"]["credential"]
            self.assertNotEqual(scoped, "default", "the model-scoped key did not change identity")
            secret = plan["providers"]["nrp"]["credentials"][scoped]["secret_name"]
            self.assertEqual((state / "credentials" / secret).read_text(), "model-secret")
            # The provider-scoped value is set but unread: it must not be mounted at all, or
            # one credential's key would be reachable through another model's identity.
            self.assertEqual([path.name for path in (state / "credentials").iterdir()], [secret])
            self.assertNotIn("provider-secret", json.dumps(plan))
            # What the launcher tells Compose to mount follows the same name.
            self.assertIn(secret, (state / "compose" / "models.json").read_text())
            # And the ledger is booked against the identity actually used.
            rate = next(c for c in plan["counters"].values() if c["family"] == "rate")
            self.assertEqual(
                rate["subject"], f"nrp/{scoped}/{plan['lanes']['primary']['model']}"
            )
            # Every lane of this one model shares its key, including the long lane.
            self.assertEqual(
                {lane["credential"] for lane in plan["lanes"].values()}, {scoped}
            )

    def test_a_model_without_its_own_key_shares_the_providers_credential(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            state = self.fixture(base, self.PROVIDER_ONLY)
            self.render(state, base / "resolved.env")
            plan = json.loads((state / "model-policy.json").read_text())
            self.assertEqual(
                {lane["credential"] for lane in plan["lanes"].values()}, {"default"}
            )
            self.assertEqual([p.name for p in (state / "credentials").iterdir()], ["nrp__default"])
            # Declaring a scope is not claiming one: the model's own name is still published to
            # the launcher, so Compose must keep carrying it or the choice could never win.
            self.assertEqual(plan["lanes"]["primary"]["key_env"], "QWEN3_API_KEY")

    def test_a_blank_model_key_falls_back_rather_than_failing(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            state = self.fixture(base, "QWEN3_API_KEY=\n" + self.PROVIDER_ONLY)
            self.render(state, base / "resolved.env")
            self.assertEqual([p.name for p in (state / "credentials").iterdir()], ["nrp__default"])

    def test_a_key_in_neither_scope_is_asked_for_by_naming_both_scopes(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            state = self.fixture(base, "")
            with self.assertRaises(subprocess.CalledProcessError) as caught:
                self.render(state, base / "resolved.env")
            refusal = caught.exception.stderr.decode()
            plan = json.loads((state / "model-policy.json").read_text())
            lane = plan["lanes"]["primary"]
            provider_scoped = plan["providers"]["nrp"]["credentials"]["default"]["env"]
            for name in (lane["key_env"], provider_scoped, lane["label"]):
                self.assertIn(name, refusal, "the hint must name both variables and the model")


class SystemPromptStagingTests(LaunchSteps, unittest.TestCase):
    """Which prompt reaches Kimi, and what each source means.

    The project-root ``SYSTEM.md`` is the operator's own untracked file and ``SYSTEM.md.example``
    is the harness default, so the pair behaves like ``.env`` and ``.env.example``. Either is a
    complete prompt: it may wrap Kimi Code's built-in prompt through ``${base_prompt}`` or replace
    it outright, and an empty file means an empty prompt rather than a fallback to the default.
    Appended on top of that text are the selected modules' guidance and the generated runtime
    envelope, the latter unless ``KIMI_SYSTEM_PROMPT_OMIT_ENVELOPE`` is truthy.
    """

    LOCAL = "SYSTEM.md"
    DEFAULT = "SYSTEM.md.example"
    #: The envelope is recognised by its heading rather than its numbers, which are derived.
    HEADING = "Model runtime envelope"

    def root_with(self, **files: str) -> Path:
        """A project root holding only the named prompt files, given by their root-level name."""
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        for name, content in files.items():
            (root / name).write_text(content)
        return root

    def prompt_source(self) -> Path:
        """The file this checkout actually resolves, which an operator's local file overrides."""
        local = ROOT / self.LOCAL
        return local if local.is_file() else ROOT / self.DEFAULT

    def root_with_runtime(self, **files: str) -> Path:
        """A root the renderer can actually run against: prompt files plus the config template.

        ``render_runtime.py`` reads only those two things from ``--root``, so this is enough to
        stage a prompt under test without copying the repository.
        """
        root = self.root_with(**files)
        (root / "runtime").mkdir()
        shutil.copy(ROOT / "runtime" / "config.toml", root / "runtime" / "config.toml")
        return root

    def staged_prompt(
        self, files: dict[str, str], *, omit: str | None = None, guidance: str = ""
    ) -> str:
        """Render a project root holding exactly ``files``, and return the staged prompt."""
        root = self.root_with_runtime(**files)
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            state = self.fixture(base, omit=omit)
            if guidance:
                (state / render_runtime.MODULE_GUIDANCE_FILE).write_text(guidance)
            self.render(state, base / "resolved.env", root=root)
            self.assertEqual(os.stat(state / "SYSTEM.md").st_mode & 0o777, 0o600)
            return (state / "SYSTEM.md").read_text()

    def test_the_operators_file_wins_over_the_harness_default(self):
        root = self.root_with(**{self.LOCAL: "# mine\n", self.DEFAULT: "# harness default\n"})
        self.assertEqual(render_runtime.system_prompt(root), "# mine\n")

    def test_the_harness_default_is_used_without_an_operators_file(self):
        root = self.root_with(**{self.DEFAULT: "# harness default\n"})
        self.assertEqual(render_runtime.system_prompt(root), "# harness default\n")

    def test_an_empty_operators_file_does_not_fall_back_to_the_default(self):
        """An empty prompt file is a decision, not a missing file: it stays empty."""
        root = self.root_with(**{self.LOCAL: "", self.DEFAULT: "# harness default\n"})
        self.assertEqual(render_runtime.system_prompt(root), "")

    def test_no_prompt_file_at_all_leaves_kimi_on_its_own_prompt(self):
        """The last tier is the only one that means "we have no opinion about the prompt"."""
        self.assertIsNone(render_runtime.system_prompt(self.root_with()))

    def test_prompt_files_below_the_project_root_are_not_prompt_files(self):
        """The names resolve at the root only, so a module's own prompt file is never picked up."""
        root = self.root_with()
        (root / "modules" / "comfyui").mkdir(parents=True)
        (root / "runtime").mkdir()
        (root / "modules" / "comfyui" / self.DEFAULT).write_text("# module\n")
        (root / "runtime" / self.LOCAL).write_text("# runtime\n")
        self.assertIsNone(render_runtime.system_prompt(root))

    def test_a_prompt_without_the_placeholder_replaces_kimis_own_prompt(self):
        """Total replacement is a supported mode, not a launch failure."""
        replacement = "You are ${product_name} in ${cwd} on ${os}.\n"
        root = self.root_with(**{self.LOCAL: replacement})
        self.assertEqual(render_runtime.system_prompt(root), replacement)

    def test_the_envelope_is_appended_to_the_prompt_by_default(self):
        staged = self.staged_prompt({self.LOCAL: "# mine\n"})
        self.assertTrue(staged.startswith("# mine\n"))
        self.assertIn(self.HEADING, staged)

    def test_an_absent_flag_and_a_blank_one_both_keep_the_envelope(self):
        """The envelope ships by default, so "unset" and "set to nothing" must not omit it."""
        for omit in (None, ""):
            with self.subTest(omit=repr(omit)):
                staged = self.staged_prompt({self.LOCAL: "# mine\n"}, omit=omit)
                self.assertIn(self.HEADING, staged)

    def test_a_value_that_is_not_truthy_keeps_the_envelope(self):
        """The flag is named after the omission, so only a truthy value may perform it."""
        for value in ("0", "false", "no", "off", "flase", "maybe", "2"):
            with self.subTest(value=value):
                staged = self.staged_prompt({self.LOCAL: "# mine\n"}, omit=value)
                self.assertIn(self.HEADING, staged)

    def test_omitting_the_envelope_stages_the_prompt_file_byte_for_byte(self):
        """The omission has to reach Kimi's own prompt too, so nothing at all may be appended."""
        for value in ("1", "true", "yes", "on", "ON", "Yes", " 1 "):
            with self.subTest(value=value):
                staged = self.staged_prompt({self.LOCAL: "# mine\n\n"}, omit=value)
                self.assertEqual(staged, "# mine\n\n")
                self.assertNotIn(self.HEADING, staged)

    def test_omitting_the_envelope_with_no_prompt_file_at_all_stages_nothing(self):
        """No file is tier three, which Kimi answers with its own prompt - so stay blank."""
        self.assertEqual(self.staged_prompt({}, omit="1"), "")

    def test_an_empty_prompt_file_with_the_envelope_stages_the_envelope_only(self):
        """Neither the harness default nor the built-in prompt sneaks in behind an empty file."""
        root = self.root_with_runtime(**{self.LOCAL: "", self.DEFAULT: "# harness default\n"})
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            state = self.fixture(base)
            self.render(state, base / "resolved.env", root=root)
            staged = (state / "SYSTEM.md").read_text()
            plan = json.loads((state / "model-policy.json").read_text())
        self.assertEqual(staged, policy.render_guidance(plan).strip("\n") + "\n")
        self.assertNotIn("harness default", staged)

    def test_an_empty_prompt_file_that_omitted_the_envelope_stages_a_period(self):
        """Kimi Code swaps its built-in prompt in for anything blank, so an empty prompt is
        expressed as the shortest string that survives that check."""
        staged = self.staged_prompt({self.LOCAL: ""}, omit="1")
        self.assertEqual(staged, render_runtime.EMPTY_PROMPT_SENTINEL + "\n")
        self.assertTrue(staged.strip())

    def test_a_whitespace_only_prompt_file_that_omitted_the_envelope_stages_a_period(self):
        """Trimming happens upstream, so whitespace is as blank as nothing."""
        staged = self.staged_prompt({self.LOCAL: "\n\n"}, omit="on")
        self.assertEqual(staged, render_runtime.EMPTY_PROMPT_SENTINEL + "\n")

    def test_module_guidance_is_appended_before_the_envelope(self):
        staged = self.staged_prompt(
            {self.LOCAL: "# mine\n"},
            guidance="## Module: Demo\n\nuse the demo\n",
        )
        self.assertLess(staged.index("# mine"), staged.index("## Module: Demo"))
        self.assertLess(staged.index("## Module: Demo"), staged.index(self.HEADING))

    def test_a_blank_prompt_file_with_module_guidance_needs_no_sentinel(self):
        """The period only exists to keep a blank prompt from being discarded, and appended text
        is not blank."""
        staged = self.staged_prompt(
            {self.LOCAL: ""}, omit="1", guidance="## Module: Demo\n\nuse the demo\n"
        )
        self.assertEqual(staged, "## Module: Demo\n\nuse the demo\n")

    def test_omitting_the_envelope_does_not_silence_module_guidance(self):
        """A module's guidance is the only path its own ``AGENTS.md`` has to reach the agent, so
        it is functionally required rather than policy decoration."""
        staged = self.staged_prompt(
            {self.LOCAL: "# mine\n"},
            omit="1",
            guidance="## Module: Demo\n\nuse the demo\n",
        )
        self.assertIn("use the demo", staged)
        self.assertNotIn(self.HEADING, staged)

    def test_the_harness_default_ships_at_the_project_root(self):
        self.assertTrue((ROOT / self.DEFAULT).is_file())

    def test_the_harness_default_wraps_kimis_prompt_exactly_once(self):
        default = self.prompt_source().read_text()
        self.assertTrue(default.strip())
        # Substituted everywhere, so a second occurrence would duplicate the whole built-in prompt.
        self.assertEqual(default.count("${base_prompt}"), 1)
        # Comments survive into the prompt, so the shipped default carries none.
        self.assertNotIn("<!--", default)


if __name__ == "__main__":
    unittest.main()
