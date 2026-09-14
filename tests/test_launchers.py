"""Exercise host entrypoints without operator credentials or a running stack."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.root = self.base / "harness with spaces"
        self.workspace = self.base / "workspace with spaces"
        for relative in (
            "start.sh", "init-workspace.sh", "shell.sh", "extensions.sh",
            "tools/runtime.sh", "tools/safe_workspace_init.py", "tools/verify_bind_paths.py",
            "scripts/read_env.py", "scripts/install_comfy_macos.sh", "comfy/backend.env",
        ):
            destination = self.root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / relative, destination)
        (self.root / ".env").touch()
        (self.root / "scripts/select_versions.py").write_text(
            'print("VERSION_SELECTION_REACHED", flush=True)\nraise SystemExit(42)\n'
        )
        self.bin = self.base / "bin"
        self.bin.mkdir()
        (self.bin / "python3").symlink_to(sys.executable)
        self.command("uname", 'case "$1" in -s) echo Darwin;; -m) echo arm64;; esac\n')
        self.command("docker", '''
if [[ "$*" == "compose version" || "$*" == "info" ]]; then exit 0; fi
if [[ "$*" == *"config --environment" ]]; then
  cat "$TEST_BOOTSTRAP"
  exit 0
fi
echo "Unexpected Docker call" >&2
exit 99
''')
        self.bootstrap = self.base / "bootstrap-fixture"
        self.bootstrap.write_text(f"WORKSPACE_PATH={self.workspace}\n")
        # Keep operator configuration out of test subprocesses.
        self.env = {
            "PATH": f"{self.bin}:{os.defpath}",
            "HOME": str(self.base),
            "TEST_BOOTSTRAP": str(self.bootstrap),
        }

    def command(self, name, body):
        path = self.bin / name
        path.write_text("#!/bin/bash\nset -euo pipefail\n" + body)
        path.chmod(0o700)

    def run_script(self, name, *args):
        return subprocess.run(
            ["/bin/bash", str(self.root / name), *args],
            cwd=self.base,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )

    def test_init_without_optional_paths_creates_workspace_and_releases_lock(self):
        for _ in range(2):
            result = self.run_script("init-workspace.sh")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Initialized agent state", result.stdout)
        self.assertTrue((self.workspace / "comfyui/user/default/workflows").is_dir())
        manifests = list((self.root / ".local/runtime").glob("*/binds.json"))
        self.assertEqual(len(manifests), 1)
        self.assertEqual(
            json.loads(manifests[0].read_text())["user"]["path"],
            str(self.workspace / "comfyui/user"),
        )
        self.assertFalse(list((self.root / ".local/runtime").glob("*/bootstrap.*")))

    def test_init_preserves_optional_external_paths(self):
        models = self.base / "external models"
        models.mkdir()
        with self.bootstrap.open("a") as output:
            output.write(f"COMFYUI_MODELS_PATH={models}\nCOMFYUI_USER_PATH=\n")
        result = self.run_script("init-workspace.sh")
        self.assertEqual(result.returncode, 0, result.stderr)
        manifest = next((self.root / ".local/runtime").glob("*/binds.json"))
        self.assertEqual(json.loads(manifest.read_text())["models"]["path"], str(models))

    def test_start_reaches_selector_and_reports_failure(self):
        result = self.run_script("start.sh", "--non-interactive")
        self.assertEqual(result.returncode, 42, result.stderr)
        self.assertIn("VERSION_SELECTION_REACHED", result.stdout)
        self.assertIn("Harness failed at", result.stderr)
        self.assertFalse(list((self.root / ".local/runtime").glob("*/bootstrap.*")))
        # A failed start must leave the same instance available to init.
        self.assertEqual(self.run_script("init-workspace.sh").returncode, 0)

    def test_start_reports_unavailable_docker_before_version_selection(self):
        self.command("docker", '''
if [[ "$*" == "compose version" ]]; then exit 0; fi
if [[ "$*" == *"config --environment" ]]; then cat "$TEST_BOOTSTRAP"; exit 0; fi
exit 1
''')
        result = self.run_script("start.sh")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Docker is not ready", result.stderr)
        self.assertNotIn("VERSION_SELECTION_REACHED", result.stdout)

    def test_start_rejects_extra_arguments(self):
        result = self.run_script("start.sh", "--non-interactive", "unexpected")
        self.assertEqual(result.returncode, 2)
        self.assertIn("usage:", result.stderr)

    def test_shell_reports_missing_runtime_instead_of_silent_exit(self):
        result = self.run_script("shell.sh")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("No running runtime", result.stderr)

    def test_failed_command_diagnostic_does_not_echo_its_arguments(self):
        self.command("docker", "exit 17\n")
        result = self.run_script("init-workspace.sh")
        self.assertEqual(result.returncode, 17)
        self.assertIn("Harness failed at", result.stderr)
        self.assertNotIn("config --environment", result.stderr)

    def test_installer_preserves_existing_release_when_mps_check_fails(self):
        self.command("shasum", 'cat >/dev/null\nprintf "fixture\\n"\n')
        base = self.root / ".local/comfy-macos/0123456789abcdef"
        release = base / "releases/fixture"
        (release / "venv/bin").mkdir(parents=True)
        python = release / "venv/bin/python"
        python.write_text("#!/bin/bash\nexit 1\n")
        python.chmod(0o700)
        (release / "FINGERPRINT").write_text("fixture\n")
        (base / "current").symlink_to("releases/fixture")
        result = self.run_script(
            "scripts/install_comfy_macos.sh", str(self.root), str(self.workspace),
            "test-version", "test-commit", "python3", "0123456789abcdef",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(python.is_file())
        self.assertTrue((base / "current").is_dir())


if __name__ == "__main__":
    unittest.main()
