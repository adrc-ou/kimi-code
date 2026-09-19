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
import prompt_context  # noqa: E402
import render_runtime  # noqa: E402


class LaunchSteps:
    """The two launch steps that produce Kimi's configuration, driven against the shipped trees.

    A plain mixin rather than a ``TestCase`` so a second case class can reuse the steps without
    inheriting - and so re-running - the first one's tests.
    """

    #: What the launcher would have read out of ``.env`` for the case under test.
    PROVIDER_ONLY = "NRP_API_KEY=provider-secret\n"
    MODEL_SCOPED = "QWEN3_API_KEY=model-secret\nNRP_API_KEY=provider-secret\n"

    def fixture(self, directory: Path, values: str = PROVIDER_ONLY) -> Path:
        """A resolved plan, plus the resolved ``.env`` the renderer is asked to read."""
        state = directory / "state"
        workspace = directory / "workspace"
        workspace.mkdir()
        resolved = directory / "resolved.env"
        resolved.write_text(f"{values}NRP_BASE_URL=\nKIMI_BACKGROUND_TASK_SLOTS=\n")
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
            # Long enough to be unguessable and made only of characters a path, a URL and a
            # Compose secret file all carry unchanged: the length itself is the generator's affair.
            self.assertGreaterEqual(len(first_salt), 32)
            self.assertRegex(first_salt, r"^[A-Za-z0-9_-]+$")
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

    def test_the_staged_documents_are_recorded_beside_themselves(self):
        """The sidecar is what lets doctor.sh explain an edit that had no effect.

        Nothing the agent can read may carry a credential, but the mode and the rotation behaviour
        are still the renderer's to get right: this file names operator documents, and it is
        rewritten rather than appended so a deselected document cannot linger as a false record.
        """
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            state = self.fixture(base)
            resolved = base / "resolved.env"
            self.render(state, resolved)
            sidecar = state / prompt_context.SOURCES_FILE
            self.assertEqual(os.stat(sidecar).st_mode & 0o777, 0o600)
            recorded = json.loads(sidecar.read_text())
            self.assertEqual(set(recorded), {"agents", "system"})
            self.assertEqual(recorded["agents"]["source"], "runtime/AGENTS.md")
            self.assertIsNone(recorded["system"]["source"], "no SYSTEM.md exists in the checkout")
            self.assertFalse(prompt_context.stale_sources(ROOT, state))
            # A second launch restages the pair, so the record has to describe the new bytes.
            self.render(state, resolved)
            self.assertEqual(json.loads(sidecar.read_text()), recorded)

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
    """Which two documents reach Kimi, and what each source and each choice means.

    The all-lane contract resolves in two tiers - the project-root ``CONTEXT.md``, else this
    harness's own ``runtime/AGENTS.md`` - and is staged as ``AGENTS.md``, where Kimi injects it
    through ``${agents_md}`` into every agent. The main agent's voice comes from ``SYSTEM.md``
    alone, or from Kimi's built-in prompt when the operator has no file. Neither chain reads a
    ``.example``. On top of whichever text resolves, the panel's remembered choices decide which
    generated blocks are appended: the usage limits for every agent, and the lane table plus the
    parallel-work guidance for the main agent only.
    """

    CONTRACT = "CONTEXT.md"
    LOCAL = "SYSTEM.md"
    #: Documentation only. Named so the tests can keep asserting neither is ever loaded.
    EXAMPLE = "SYSTEM.md.example"
    CONTRACT_EXAMPLE = "CONTEXT.md.example"
    #: The generated blocks are recognised by their heading line, never by the numbers under it.
    #: The leading newline matters: the contract prose mentions "Model runtime envelope" in
    #: passing, and "### Parallel work" contains "## Parallel work" as a substring.
    LIMITS = "## Model usage limits (generated at launch)"
    ENVELOPE = "\n## Model runtime envelope (generated at launch)"
    PARALLELISM = "\n## Parallel work"

    def root_with(self, **files: str) -> Path:
        """A project root holding only the named prompt files, given by their root-level name."""
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        for name, content in files.items():
            (root / name).write_text(content)
        return root

    def example_text(self) -> str:
        """The shipped ``SYSTEM.md.example``: for a human to read, and loaded by nothing."""
        return (ROOT / self.EXAMPLE).read_text(encoding="utf-8")

    def root_with_runtime(self, **files: str) -> Path:
        """A root the renderer can actually run against: prompt files plus the runtime sources.

        ``render_runtime.py`` reads only those from ``--root``, so this is enough to stage a prompt
        under test without copying the repository. The contract source is copied alongside the
        config template because it is tier two of the all-lane document, which every render reads.
        """
        root = self.root_with(**files)
        (root / "runtime").mkdir()
        for name in ("config.toml", "AGENTS.md"):
            shutil.copy(ROOT / "runtime" / name, root / "runtime" / name)
        return root

    def stage(
        self,
        files: dict[str, str],
        *,
        off: tuple[str, ...] = (),
        guidance: str = "",
    ) -> Path:
        """Render a project root holding exactly ``files``, with every panel option on except those
        named in ``off``, and return the runtime directory the renderer wrote into."""
        root = self.root_with_runtime(**files)
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        base = Path(holder.name)
        state = self.fixture(base)
        prompt_context.save_prefs(
            state / prompt_context.PREFS_FILE,
            {option: option not in off for option in prompt_context.OPTION_IDS},
        )
        if guidance:
            (state / render_runtime.MODULE_GUIDANCE_FILE).write_text(guidance)
        self.render(state, base / "resolved.env", root=root)
        return state

    def staged_prompt(self, files: dict[str, str], **values) -> str:
        """The main agent's staged document."""
        state = self.stage(files, **values)
        self.assertEqual(os.stat(state / "SYSTEM.md").st_mode & 0o777, 0o600)
        return (state / "SYSTEM.md").read_text()

    def staged_agents(self, files: dict[str, str], **values) -> str:
        """The all-lane staged contract, which is a different document from the one above."""
        state = self.stage(files, **values)
        self.assertEqual(os.stat(state / "AGENTS.md").st_mode & 0o777, 0o600)
        return (state / "AGENTS.md").read_text()

    def test_the_operators_file_is_what_gets_read(self):
        root = self.root_with(**{self.LOCAL: "# mine\n", self.EXAMPLE: "# harness default\n"})
        self.assertEqual(prompt_context.system_source(root), root / self.LOCAL)

    def test_the_operators_contract_beats_the_harness_default(self):
        root = self.root_with_runtime(**{self.CONTRACT: "# mine\n"})
        self.assertEqual(prompt_context.context_source(root), root / self.CONTRACT)

    def test_the_harness_default_is_the_second_tier(self):
        """With no operator file the contract is this harness's own, which is a real file."""
        root = self.root_with_runtime()
        self.assertEqual(prompt_context.context_source(root), root / "runtime" / "AGENTS.md")

    def test_neither_example_file_is_ever_loaded(self):
        """A file named as an example is documentation, so its presence must change nothing."""
        root = self.root_with(**{self.EXAMPLE: "# harness default\n"})
        self.assertIsNone(prompt_context.system_source(root))
        root = self.root_with(**{self.CONTRACT_EXAMPLE: "# harness default\n"})
        self.assertIsNone(prompt_context.context_source(root))

    def test_an_empty_operators_file_is_a_prompt_of_its_own(self):
        """An empty prompt file is a decision, not a missing file: it is still what gets read."""
        root = self.root_with(**{self.LOCAL: "", self.EXAMPLE: "# harness default\n"})
        self.assertEqual(prompt_context.system_source(root), root / self.LOCAL)

    def test_no_prompt_file_at_all_leaves_kimi_on_its_own_prompt(self):
        """The last tier is the only one that means "we have no opinion about the prompt"."""
        self.assertIsNone(prompt_context.system_source(self.root_with()))

    def test_prompt_files_below_the_project_root_are_not_prompt_files(self):
        """The names resolve at the root only, so a module's own prompt file is never picked up."""
        root = self.root_with()
        (root / "modules" / "comfyui").mkdir(parents=True)
        (root / "runtime").mkdir()
        for name in (self.LOCAL, self.CONTRACT):
            (root / "modules" / "comfyui" / name).write_text("# module\n")
            (root / "runtime" / name).write_text("# runtime\n")
        self.assertIsNone(prompt_context.system_source(root))
        self.assertIsNone(prompt_context.context_source(root))

    def test_a_prompt_without_the_placeholder_replaces_kimis_own_prompt(self):
        """Total replacement is a supported mode, not a launch failure."""
        replacement = "You are ${product_name} in ${cwd} on ${os}.\n"
        staged = self.staged_prompt({self.LOCAL: replacement}, off=prompt_context.OPTION_IDS)
        self.assertEqual(staged, replacement)

    def test_the_generated_blocks_are_on_by_default(self):
        """Nothing was ever asked to be omitted, so both documents carry their blocks."""
        agents = self.staged_agents({self.LOCAL: "# mine\n"})
        self.assertIn(self.LIMITS, agents)
        prompt = self.staged_prompt({self.LOCAL: "# mine\n"})
        self.assertTrue(prompt.startswith("# mine\n"))
        self.assertIn(self.ENVELOPE, prompt)
        self.assertIn(self.PARALLELISM, prompt)

    def test_the_contract_holds_nothing_the_voice_owns_and_vice_versa(self):
        """No text reaches the main agent twice: it gets the contract through ``${agents_md}``."""
        agents = self.staged_agents({self.LOCAL: "# mine\n"})
        prompt = self.staged_prompt({self.LOCAL: "# mine\n"})
        self.assertNotIn(self.ENVELOPE, agents)
        self.assertNotIn(self.PARALLELISM, agents)
        self.assertNotIn(self.LIMITS, prompt)

    def test_a_single_block_can_be_switched_off_without_touching_the_others(self):
        prompt = self.staged_prompt({self.LOCAL: "# mine\n"}, off=(policy.OPTION_LANE_TABLE,))
        self.assertIn(self.PARALLELISM, prompt)
        self.assertNotIn(self.ENVELOPE, prompt)
        agents = self.staged_agents({self.LOCAL: "# mine\n"}, off=(policy.OPTION_LANE_TABLE,))
        self.assertIn(self.LIMITS, agents)

    def test_switching_off_every_block_stages_the_operators_text_and_nothing_else(self):
        """With nothing selected the operator's text is the whole document. Edge newlines are
        normalised by the composer; no add-on text is appended."""
        staged = self.staged_prompt({self.LOCAL: "# mine\n\n"}, off=prompt_context.OPTION_IDS)
        self.assertEqual(staged, "# mine\n")
        self.assertNotIn(self.ENVELOPE, staged)

    def test_no_blocks_and_no_prompt_file_at_all_stages_nothing(self):
        """No file is the tier that means no opinion, which Kimi answers with its own prompt."""
        self.assertEqual(self.staged_prompt({}, off=prompt_context.OPTION_IDS), "")

    def test_an_absent_prompt_file_still_wraps_kimis_prompt_when_a_block_is_on(self):
        """Appending to a staged file that Kimi reads as empty would replace its prompt, so the
        wrapper goes in - as the 14-character placeholder, never as expanded text."""
        staged = self.staged_prompt({}, off=(policy.OPTION_LANE_TABLE,))
        self.assertTrue(staged.startswith(prompt_context.BASE_PROMPT_WRAPPER + "\n"))
        self.assertEqual(staged.count(prompt_context.BASE_PROMPT_WRAPPER), 1)

    def test_an_empty_prompt_file_with_blocks_stages_the_blocks_only(self):
        """Neither the example file nor the built-in prompt sneaks in behind an empty file.

        Existence determines authority and emptiness determines payload, so the enabled blocks
        become the entire prompt. That is the intended reading of a decision the operator made.
        """
        state = self.stage({self.LOCAL: "", self.EXAMPLE: "# harness default\n"})
        plan = json.loads((state / "model-policy.json").read_text())
        staged = (state / "SYSTEM.md").read_text()
        expected = policy.render_guidance(plan, "main").strip("\n") + "\n"
        self.assertEqual(staged, expected)
        self.assertNotIn("harness default", staged)
        self.assertNotIn(prompt_context.BASE_PROMPT_WRAPPER, staged)

    def test_an_empty_prompt_file_with_no_blocks_stages_a_period(self):
        """Kimi Code swaps its built-in prompt in for anything blank, so an empty prompt is
        expressed as the shortest string that survives that check."""
        staged = self.staged_prompt({self.LOCAL: ""}, off=prompt_context.OPTION_IDS)
        self.assertEqual(staged, prompt_context.EMPTY_PROMPT_SENTINEL + "\n")
        self.assertTrue(staged.strip())

    def test_a_whitespace_only_prompt_file_with_no_blocks_stages_a_period(self):
        """Trimming happens upstream, so whitespace is as blank as nothing."""
        staged = self.staged_prompt({self.LOCAL: "\n\n"}, off=prompt_context.OPTION_IDS)
        self.assertEqual(staged, prompt_context.EMPTY_PROMPT_SENTINEL + "\n")

    def test_module_guidance_reaches_the_contract_not_the_voice(self):
        """A module's guidance is for every agent that touches it, which is the all-lane file."""
        guidance = "## Module: Demo\n\nuse the demo\n"
        agents = self.staged_agents({self.LOCAL: "# mine\n"}, guidance=guidance)
        self.assertLess(agents.index(self.LIMITS), agents.index("## Module: Demo"))
        prompt = self.staged_prompt({self.LOCAL: "# mine\n"}, guidance=guidance)
        self.assertNotIn("use the demo", prompt)

    def test_turning_module_guidance_off_silences_it(self):
        """It is an option like any other, so the operator can decline it - and the panel says
        which modules then lose the only route their own text has to the agent."""
        agents = self.staged_agents(
            {self.LOCAL: "# mine\n"},
            off=("module_guidance",),
            guidance="## Module: Demo\n\nuse the demo\n",
        )
        self.assertNotIn("use the demo", agents)
        self.assertIn(self.LIMITS, agents)

    def test_an_empty_contract_still_gets_its_blocks_and_its_modules(self):
        """The sentinel exists to keep a *voice* document from being discarded. The contract is a
        bind-mounted file whose emptiness is simply silence, so its add-ons follow it unchanged."""
        agents = self.staged_agents(
            {self.CONTRACT: ""},
            off=(policy.OPTION_LANE_TABLE,),
            guidance="## Module: Demo\n\nuse the demo\n",
        )
        self.assertIn(self.LIMITS, agents)
        self.assertIn("## Module: Demo", agents)

    def test_the_example_is_a_documentation_file(self):
        self.assertTrue((ROOT / self.EXAMPLE).is_file())

    def test_the_example_shows_a_wrapper_around_kimis_prompt(self):
        """Not a load-bearing claim about this session's prompt - a lint on the worked example, so
        the pattern the docs teach is a pattern that actually works.

        Checked on the text an operator would get after staging, because that is the only
        version that has ever shipped anywhere: help comments in the example are stripped by
        the same rule that strips them from the real file.
        """
        staged = prompt_context.strip_html_comments(self.example_text())
        self.assertTrue(staged.strip())
        # Substituted everywhere, so a second occurrence would duplicate the whole built-in prompt.
        self.assertEqual(staged.count("${base_prompt}"), 1)
        # Every placeholder the example advertises in its prose must be one Kimi actually binds,
        # or the example teaches a variable that reaches the model as a literal. ${HOME} is the
        # one deliberate exception: it is shown as an example of prose that survives untouched.
        documented = set(prompt_context.PLACEHOLDER_PATTERN.findall(self.example_text()))
        self.assertIn("${base_prompt}", self.example_text())
        documented -= {"HOME"}
        known = set(prompt_context.known_placeholders()) | {"base_prompt"}
        self.assertEqual(
            sorted(name for name in documented if name not in known), [],
            "the example documents a placeholder nothing binds",
        )
        # Nothing is loaded from this file, so an operator's own SYSTEM.md may carry the HTML
        # comments this example uses to explain itself without any of them reaching the prompt.
        self.assertIn("<!--", self.example_text())


if __name__ == "__main__":
    unittest.main()
