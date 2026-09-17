import os
import subprocess
import tempfile
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class RenderRuntimeTests(unittest.TestCase):
    """End to end over the two launch steps that produce Kimi's configuration.

    Each case runs the real resolver against the shipped ``./models`` and ``./providers``
    and then the real renderer, because the contract under test is that a plan written by
    one is consumable by the other, and that no credential value leaks into the files the
    agent can read.
    """

    def fixture(self, directory: Path) -> Path:
        state = directory / "state"
        workspace = directory / "workspace"
        workspace.mkdir()
        resolved = directory / "resolved.env"
        resolved.write_text("NRP_API_KEY=provider-secret\nNRP_BASE_URL=\nKIMI_BACKGROUND_TASK_SLOTS=\n")
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

    def render(self, state: Path, resolved: Path) -> None:
        subprocess.run(
            [
                "python3",
                str(ROOT / "tools" / "render_runtime.py"),
                "--root",
                str(ROOT),
                "--runtime-dir",
                str(state),
                "--resolved-env",
                str(resolved),
            ],
            check=True,
            capture_output=True,
        )

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
            for name in ("kimi-config.toml", "runtime.env", "model-policy.json"):
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


if __name__ == "__main__":
    unittest.main()
