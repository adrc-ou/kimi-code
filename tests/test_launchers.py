"""Exercise host entrypoints without operator credentials or a running stack."""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
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
            "scripts/select_macos_python.sh",
            "dependencies.lock.json",
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

    def test_macos_python_prefers_versioned_interpreter(self):
        self.command("python3.12", "exit 0\n")
        result = self.run_script("scripts/select_macos_python.sh")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), str(self.bin / "python3.12"))

    def managed_python_fixture(self, valid=True):
        (self.bin / "python3").unlink()
        self.env["TEST_HOST_PYTHON"] = sys.executable
        self.command("python3", '''
if [[ "$1" == -c ]]; then exit 1; fi
exec "$TEST_HOST_PYTHON" "$@"
''')
        # Make tests independent of any system Python 3.12 or package manager.
        self.command("python3.12", "exit 1\n")
        self.command("brew", "exit 99\n")
        payload = self.base / "payload"
        (payload / "python/bin").mkdir(parents=True)
        python = payload / "python/bin/python3.12"
        python.write_text(f"#!/bin/bash\nexit {0 if valid else 1}\n")
        python.chmod(0o700)
        archive = self.base / "python.tar.gz"
        with tarfile.open(archive, "w:gz") as output:
            output.add(payload / "python", arcname="python")
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        lock_path = self.root / "dependencies.lock.json"
        lock = json.loads(lock_path.read_text())
        lock["downloads"]["macos-python"]["digest"] = f"sha256:{digest}"
        lock_path.write_text(json.dumps(lock))
        self.env["TEST_ARCHIVE"] = str(archive)
        self.env["HARNESS_RUNTIME_DIR"] = str(self.root / ".local/runtime/test")
        self.command("curl", '''
while [[ "$1" != --output ]]; do shift; done
cp "$TEST_ARCHIVE" "$2"
''')
        return Path(self.env["HARNESS_RUNTIME_DIR"]) / "python" / digest

    def test_macos_python_installs_verified_archive_and_reuses_it(self):
        destination = self.managed_python_fixture()
        result = self.run_script("scripts/select_macos_python.sh")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), str(destination / "python/bin/python3.12"))
        self.assertTrue((destination / "python/bin/python3.12").is_file())
        self.assertFalse(list(destination.parent.glob("install.*")))
        self.command("curl", "exit 99\n")
        reused = self.run_script("scripts/select_macos_python.sh")
        self.assertEqual(reused.returncode, 0, reused.stderr)
        self.assertEqual(reused.stdout, result.stdout)

    def test_macos_python_rejects_bad_checksum_and_cleans_staging(self):
        destination = self.managed_python_fixture()
        Path(self.env["TEST_ARCHIVE"]).write_bytes(b"corrupted archive")
        result = self.run_script("scripts/select_macos_python.sh")
        self.assertEqual(result.returncode, 1)
        self.assertIn("checksum mismatch", result.stderr)
        self.assertFalse(destination.exists())
        self.assertFalse(list(destination.parent.glob("install.*")))

    def test_macos_python_rejects_incompatible_download(self):
        destination = self.managed_python_fixture(valid=False)
        result = self.run_script("scripts/select_macos_python.sh")
        self.assertEqual(result.returncode, 1)
        self.assertIn("does not run as arm64 Python 3.12", result.stderr)
        self.assertFalse(destination.exists())
        self.assertFalse(list(destination.parent.glob("install.*")))

    def test_macos_python_download_failure_cleans_staging(self):
        destination = self.managed_python_fixture()
        self.command("curl", "exit 22\n")
        result = self.run_script("scripts/select_macos_python.sh")
        self.assertEqual(result.returncode, 22)
        self.assertFalse(destination.exists())
        self.assertFalse(list(destination.parent.glob("install.*")))

    def test_macos_python_rejects_invalid_override_without_fallback(self):
        self.command("python3.12", "exit 0\n")
        self.env["COMFYUI_MACOS_PYTHON"] = str(self.base / "missing python")
        result = self.run_script("scripts/select_macos_python.sh")
        self.assertEqual(result.returncode, 1)
        self.assertIn("COMFYUI_MACOS_PYTHON must point", result.stderr)
        self.assertEqual(result.stdout, "")

    def test_macos_python_valid_override_preserves_path_with_spaces(self):
        python = self.base / "custom python"
        python.write_text("#!/bin/bash\nexit 0\n")
        python.chmod(0o700)
        self.env["COMFYUI_MACOS_PYTHON"] = str(python)
        result = self.run_script("scripts/select_macos_python.sh")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), str(python))

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
