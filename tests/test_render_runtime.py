import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class RenderRuntimeTests(unittest.TestCase):
    def test_tokens_rotate_but_cache_salt_persists(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state"
            env = Path(directory) / "resolved.env"
            env.write_text("LITELLM_API_KEY=provider-secret\n")
            command = [
                "python3",
                str(ROOT / "tools" / "render_runtime.py"),
                "--root",
                str(ROOT),
                "--runtime-dir",
                str(state),
                "--resolved-env",
                str(env),
            ]
            subprocess.run(command, check=True, capture_output=True)
            first_token = (state / "proxy-token").read_text()
            first_salt = (state / "cache-salt").read_text()
            subprocess.run(command, check=True, capture_output=True)
            self.assertNotEqual(first_token, (state / "proxy-token").read_text())
            self.assertEqual(first_salt, (state / "cache-salt").read_text())
            self.assertEqual(len(first_salt), 43)
            self.assertNotIn("__MODEL_PROXY_TOKEN__", (state / "kimi-config.toml").read_text())
            self.assertEqual(os.stat(state / "nrp-api-key").st_mode & 0o777, 0o600)

    def test_runtime_environment_exposes_only_the_staging_inputs(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state"
            env = Path(directory) / "resolved.env"
            env.write_text("LITELLM_API_KEY=provider-secret\n")
            subprocess.run(
                [
                    "python3",
                    str(ROOT / "tools" / "render_runtime.py"),
                    "--root",
                    str(ROOT),
                    "--runtime-dir",
                    str(state),
                    "--resolved-env",
                    str(env),
                ],
                check=True,
                capture_output=True,
            )
            published = (state / "runtime.env").read_text()
            self.assertIn("KIMI_RENDERED_CONFIG=", published)
            self.assertIn("KIMI_SYSTEM_MD=", published)
            self.assertNotIn("KIMI_EMPTY", published)
            self.assertFalse((state / "user-agents").exists())


if __name__ == "__main__":
    unittest.main()
