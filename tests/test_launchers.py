"""Exercise host entrypoints without operator credentials or a running stack."""

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
            "start.sh",
            "shell.sh",
            "extensions.sh",
            "doctor.sh",
            "tools/runtime.sh",
            "tools/modules.py",
            "tools/safe_workspace_init.py",
            "scripts/read_env.py",
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
        self.command(
            "docker",
            """
if [[ "$*" == "compose version" || "$*" == "info" ]]; then exit 0; fi
if [[ "$*" == *"config --environment" ]]; then
  cat "$TEST_BOOTSTRAP"
  exit 0
fi
echo "Unexpected Docker call" >&2
exit 99
""",
        )
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

    def test_start_reaches_selector_and_reports_failure(self):
        result = self.run_script("start.sh", "--non-interactive")
        self.assertEqual(result.returncode, 42, result.stderr)
        self.assertIn("VERSION_SELECTION_REACHED", result.stdout)
        self.assertIn("Harness failed at", result.stderr)
        self.assertFalse(list((self.root / ".local/runtime").glob("*/bootstrap.*")))
        # A failed start must leave the same instance available to init.
        self.assertEqual(self.run_script("start.sh", "--non-interactive").returncode, 42)

    def test_start_reports_unavailable_docker_before_version_selection(self):
        self.command(
            "docker",
            """
if [[ "$*" == "compose version" ]]; then exit 0; fi
if [[ "$*" == *"config --environment" ]]; then cat "$TEST_BOOTSTRAP"; exit 0; fi
exit 1
""",
        )
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

    def test_doctor_reports_missing_runtime(self):
        result = self.run_script("doctor.sh")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("No prepared runtime", result.stderr)

    def test_doctor_rejects_unknown_arguments(self):
        result = self.run_script("doctor.sh", "--unknown")
        self.assertEqual(result.returncode, 2)
        self.assertIn("usage:", result.stderr)

    def test_failed_command_diagnostic_does_not_echo_its_arguments(self):
        self.command("docker", "exit 17\n")
        result = self.run_script("start.sh", "--non-interactive")
        self.assertEqual(result.returncode, 17)
        self.assertIn("Harness failed at", result.stderr)
        self.assertNotIn("config --environment", result.stderr)


if __name__ == "__main__":
    unittest.main()


class SessionFlowTests(unittest.TestCase):
    """Use fake Docker/services to verify startup ordering without operator state."""

    setUp = LauncherTests.setUp
    command = LauncherTests.command
    run_script = LauncherTests.run_script

    def test_module_session_then_core_session_preserves_workspace(self):
        for directory in ("tools", "runtime"):
            shutil.copytree(ROOT / directory, self.root / directory, dirs_exist_ok=True)
        (self.root / "scripts/select_versions.py").write_text("""
import os
from pathlib import Path
path = Path(os.environ['HARNESS_SESSION_FILE'])
path.write_text('KIMI_CODE_VERSION=0.42.0\\n'
                'KIMI_CODE_ASSET_URL=https://example.invalid/asset\\n'
                'KIMI_CODE_ASSET_SHA256=' + 'a'*64 + '\\n')
with open(os.environ['TEST_EVENTS'], 'a') as output: output.write('kimi-version\\n')
""")
        module = self.root / "modules/demo"
        module.mkdir(parents=True)
        (module / "module.json").write_text(
            '{"schema_version":1,"label":"Demo","workspace_directories":["demo/user"]}'
        )
        (module / "AGENTS.md").write_text("Demo instructions")
        (module / "module.sh").write_text("""
if [[ "${1:-}" == compatible ]]; then exit 0; fi
module_configure() { echo configure >>"${TEST_EVENTS}"; }
module_select_version() { echo module-version >>"${TEST_EVENTS}"; }
module_prepare() { echo prepare >>"${TEST_EVENTS}"; }
module_install() { echo install >>"${TEST_EVENTS}"; }
module_start() { echo start >>"${TEST_EVENTS}"; }
""")
        self.bootstrap.write_text(f"WORKSPACE_PATH={self.workspace}\nLITELLM_API_KEY=fixture-key\n")
        self.env["TEST_EVENTS"] = str(self.base / "events")
        self.env["HARNESS_MODULES"] = "demo"
        (self.bin / "python3").unlink()
        self.env["TEST_REAL_PYTHON"] = sys.executable
        self.command(
            "python3",
            """
if [[ -n "${URL_TO_CHECK:-}" ]]; then cat >/dev/null; exit 0; fi
exec "$TEST_REAL_PYTHON" "$@"
""",
        )
        self.command(
            "docker",
            """
if [[ "$*" == *"config --environment" ]]; then cat "$TEST_BOOTSTRAP"; exit 0; fi
if [[ "$*" == *"config --format json" ]]; then echo '{}'; exit 0; fi
if [[ "$*" == *"kimi --version" ]]; then echo 0.42.0; exit 0; fi
if [[ "$*" == *"up --remove-orphans"* ]]; then sleep 1; exit 0; fi
exit 0
""",
        )
        result = self.run_script("start.sh", "--non-interactive")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            (self.base / "events").read_text().splitlines(),
            ["configure", "kimi-version", "module-version", "prepare", "install", "start"],
        )
        data = self.workspace / "demo/user/data"
        data.write_text("keep")
        self.assertIn("Demo instructions", (self.workspace / "AGENTS.md").read_text())
        self.env["HARNESS_MODULES"] = ""
        (self.base / "events").write_text("")
        result = self.run_script("start.sh", "--non-interactive")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.base / "events").read_text(), "kimi-version\n")
        self.assertEqual(data.read_text(), "keep")
        self.assertNotIn("Demo instructions", (self.workspace / "AGENTS.md").read_text())
        runtime = next((self.root / ".local/runtime").iterdir())
        self.assertEqual((runtime / "last-modules.json").read_text().strip(), "[]")
        self.assertFalse((runtime / "module.env").exists())
        self.assertFalse((runtime / "nrp-api-key").exists())
