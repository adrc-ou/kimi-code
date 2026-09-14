import json
import re
import subprocess
import tempfile
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class ConfigurationTests(unittest.TestCase):
    def test_checked_in_json_and_toml_parse(self):
        for path in ROOT.rglob("*.json"):
            if "node_modules" not in path.parts and ".local" not in path.parts:
                json.loads(path.read_text())
        for path in ROOT.rglob("*.toml"):
            if ".local" in path.parts:
                continue
            with path.open("rb") as source:
                tomllib.load(source)

    def test_browser_sandbox_is_not_disabled(self):
        document = json.loads((ROOT / "runtime" / "mcp.json").read_text())
        args = document["mcpServers"]["chrome-devtools"]["args"]
        self.assertFalse(any("no-sandbox" in arg for arg in args))
        profile = json.loads((ROOT / "container" / "chromium-seccomp.json").read_text())
        allowed = [
            call
            for call in profile["syscalls"]
            if call.get("action") == "SCMP_ACT_ALLOW"
            and {"clone", "chroot", "setns", "unshare"}.issubset(call.get("names", []))
            and not call.get("includes")
        ]
        self.assertTrue(allowed)

    def test_all_base_images_are_digest_pinned(self):
        for relative in (
            "container/Dockerfile",
            "comfy/Dockerfile.cuda",
            "proxy/Dockerfile",
            "search-adapter/Dockerfile",
        ):
            for line in (ROOT / relative).read_text().splitlines():
                if line.startswith("FROM ") and "${" not in line:
                    image = line.split()[1]
                    self.assertRegex(image, r"@sha256:[0-9a-f]{64}$")

    def test_platform_key_is_not_persisted(self):
        selector = (ROOT / "scripts" / "select_versions.py").read_text()
        self.assertNotIn('"PLATFORM_KEY":', selector)

    def test_read_env_uses_last_resolved_value(self):
        with tempfile.NamedTemporaryFile("w", delete=False) as output:
            output.write("VALUE=first\nVALUE=second\n")
            path = Path(output.name)
        try:
            result = subprocess.run(
                ["python3", str(ROOT / "scripts" / "read_env.py"), str(path), "VALUE"],
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.stdout.strip(), "second")
        finally:
            path.unlink()

    def test_dependency_lock_has_only_digests_and_commits(self):
        lock = json.loads((ROOT / "dependencies.lock.json").read_text())
        self.assertTrue(lock["images"])
        self.assertTrue(
            all(re.fullmatch(r"sha256:[0-9a-f]{64}", value) for value in lock["images"].values())
        )
        self.assertTrue(
            all(re.fullmatch(r"[0-9a-f]{40}", value) for value in lock["sources"].values())
        )


if __name__ == "__main__":
    unittest.main()
