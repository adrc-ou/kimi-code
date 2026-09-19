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

ROOT = Path(__file__).resolve().parents[3]


class MacInstallerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.root = self.base / "harness with spaces"
        self.workspace = self.base / "workspace with spaces"
        shutil.copytree(ROOT / "modules/comfyui", self.root / "modules/comfyui")
        self.bin = self.base / "bin"
        self.bin.mkdir()
        (self.bin / "python3").symlink_to(sys.executable)
        self.env = {"PATH": f"{self.bin}:{os.defpath}", "HOME": str(self.base)}

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
        )

    def test_macos_python_prefers_versioned_interpreter(self):
        self.command("python3.12", "exit 0\n")
        result = self.run_script("modules/comfyui/scripts/select_macos_python.sh")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), str(self.bin / "python3.12"))

    def managed_python_fixture(self, valid=True):
        (self.bin / "python3").unlink()
        self.env["TEST_HOST_PYTHON"] = sys.executable
        self.command(
            "python3",
            """
if [[ "$1" == -c ]]; then exit 1; fi
exec "$TEST_HOST_PYTHON" "$@"
""",
        )
        # Make tests independent of any system Python 3.12.
        self.command("python3.12", "exit 1\n")
        payload = self.base / "payload"
        (payload / "python/bin").mkdir(parents=True)
        python = payload / "python/bin/python3.12"
        python.write_text(f"#!/bin/bash\nexit {0 if valid else 1}\n")
        python.chmod(0o700)
        archive = self.base / "python.tar.gz"
        with tarfile.open(archive, "w:gz") as output:
            output.add(payload / "python", arcname="python")
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        lock_path = self.root / "modules/comfyui/dependencies.lock.json"
        lock = json.loads(lock_path.read_text())
        lock["downloads"]["macos-python"]["digest"] = f"sha256:{digest}"
        lock_path.write_text(json.dumps(lock))
        self.env["TEST_ARCHIVE"] = str(archive)
        self.env["HARNESS_RUNTIME_DIR"] = str(self.root / ".local/runtime/test")
        self.command(
            "curl",
            """
while [[ "$1" != --output ]]; do shift; done
cp "$TEST_ARCHIVE" "$2"
""",
        )
        return Path(self.env["HARNESS_RUNTIME_DIR"]) / "module-data/comfyui/python" / digest

    def test_macos_python_installs_verified_archive_and_reuses_it(self):
        destination = self.managed_python_fixture()
        result = self.run_script("modules/comfyui/scripts/select_macos_python.sh")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), str(destination / "python/bin/python3.12"))
        self.assertTrue((destination / "python/bin/python3.12").is_file())
        self.assertFalse(list(destination.parent.glob("install.*")))
        self.command("curl", "exit 99\n")
        reused = self.run_script("modules/comfyui/scripts/select_macos_python.sh")
        self.assertEqual(reused.returncode, 0, reused.stderr)
        self.assertEqual(reused.stdout, result.stdout)

    def test_macos_python_rejects_bad_checksum_and_cleans_staging(self):
        destination = self.managed_python_fixture()
        Path(self.env["TEST_ARCHIVE"]).write_bytes(b"corrupted archive")
        result = self.run_script("modules/comfyui/scripts/select_macos_python.sh")
        self.assertEqual(result.returncode, 1)
        self.assertIn("checksum mismatch", result.stderr)
        self.assertFalse(destination.exists())
        self.assertFalse(list(destination.parent.glob("install.*")))

    def test_macos_python_rejects_incompatible_download(self):
        destination = self.managed_python_fixture(valid=False)
        result = self.run_script("modules/comfyui/scripts/select_macos_python.sh")
        self.assertEqual(result.returncode, 1)
        self.assertIn("does not run as arm64 Python 3.12", result.stderr)
        self.assertFalse(destination.exists())
        self.assertFalse(list(destination.parent.glob("install.*")))

    def test_macos_python_download_failure_cleans_staging(self):
        destination = self.managed_python_fixture()
        self.command("curl", "exit 22\n")
        result = self.run_script("modules/comfyui/scripts/select_macos_python.sh")
        self.assertEqual(result.returncode, 22)
        self.assertFalse(destination.exists())
        self.assertFalse(list(destination.parent.glob("install.*")))

    def test_macos_python_rejects_invalid_override_without_fallback(self):
        self.command("python3.12", "exit 0\n")
        self.env["COMFYUI_MACOS_PYTHON"] = str(self.base / "missing python")
        result = self.run_script("modules/comfyui/scripts/select_macos_python.sh")
        self.assertEqual(result.returncode, 1)
        self.assertIn("COMFYUI_MACOS_PYTHON must point", result.stderr)
        self.assertEqual(result.stdout, "")

    def test_macos_python_valid_override_preserves_path_with_spaces(self):
        python = self.base / "custom python"
        python.write_text("#!/bin/bash\nexit 0\n")
        python.chmod(0o700)
        self.env["COMFYUI_MACOS_PYTHON"] = str(python)
        result = self.run_script("modules/comfyui/scripts/select_macos_python.sh")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), str(python))

    def test_installer_preserves_existing_release_when_mps_check_fails(self):
        self.command("shasum", 'cat >/dev/null\nprintf "fixture\\n"\n')
        base = self.root / ".local/runtime/0123456789abcdef/module-data/comfyui/app"
        release = base / "releases/fixture"
        (release / "venv/bin").mkdir(parents=True)
        python = release / "venv/bin/python"
        python.write_text("#!/bin/bash\nexit 1\n")
        python.chmod(0o700)
        (release / "FINGERPRINT").write_text("fixture\n")
        (base / "current").symlink_to("releases/fixture")
        result = self.run_script(
            "modules/comfyui/scripts/install_comfy_macos.sh",
            str(self.root),
            str(self.workspace),
            "test-version",
            "test-commit",
            "python3",
            "0123456789abcdef",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(python.is_file())
        self.assertTrue((base / "current").is_dir())
