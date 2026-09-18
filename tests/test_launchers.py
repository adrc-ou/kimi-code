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
        # Model selection runs before the version menu, so a launcher fixture needs the
        # definition trees and every module the resolver imports.
        for relative in (
            "tools/models.py",
            "tools/model_config.py",
            "tools/policy.py",
            "tools/definitions.py",
            "tools/compose_hygiene.py",
            "tools/env_values.py",
            "tools/git_query.py",
            "runtime/config.toml",
            "compose.bootstrap.yaml",
        ):
            destination = self.root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / relative, destination)
        for tree in ("models", "providers"):
            shutil.copytree(ROOT / tree, self.root / tree)
        self.workspace.mkdir(parents=True, exist_ok=True)
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
        self.require_executable_fixtures()

    def require_executable_fixtures(self):
        # These tests drive the entrypoints through stub commands in self.bin, which the
        # launcher runs by name through bash. On a noexec filesystem (a tmpfs /tmp is the
        # common case) every such call exits 126, indistinguishable from a real regression.
        probe = self.bin / "harness-exec-probe"
        probe.write_text("#!/bin/bash\nexit 0\n")
        probe.chmod(0o700)
        try:
            unreachable = subprocess.run(
                ["/bin/bash", "-c", "harness-exec-probe"],
                capture_output=True,
                env={**self.env, "PATH": str(self.bin)},
            ).returncode == 126
        except OSError:
            unreachable = True
        if unreachable:
            self.skipTest(
                f"{self.base} is on a noexec filesystem, so the launcher stubs cannot run; "
                "set TMPDIR to a writable executable directory"
            )

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


class SessionFlowTests(unittest.TestCase):
    """Use fake Docker/services to verify startup ordering without operator state."""

    setUp = LauncherTests.setUp
    command = LauncherTests.command
    run_script = LauncherTests.run_script
    require_executable_fixtures = LauncherTests.require_executable_fixtures

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
        self.bootstrap.write_text(f"WORKSPACE_PATH={self.workspace}\nNRP_API_KEY=fixture-key\n")
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
        # The launcher asserts confinement against the resolved configuration before it starts
        # anything, so the stub has to describe a real one: the workspace bind, the staged
        # volumes, and the five snapshot binds approve_extensions.py always emits.
        self.command(
            "resolved-config-clean",
            """
exec "$TEST_REAL_PYTHON" - <<'PY'
import json, os


def bind(source, target, read_only=True):
    return {
        "type": "bind",
        "source": source,
        "target": target,
        "read_only": read_only,
        "bind": {"create_host_path": False},
    }


snapshot = os.path.join(os.environ["HARNESS_RUNTIME_DIR"], "extension-snapshot")
volumes = [
    bind(os.environ["WORKSPACE_PATH"], "/workspace", False),
    {"type": "volume", "source": "kimi-state", "target": "/home/agent/.kimi-code"},
    {"type": "volume", "source": "serena-state", "target": "/home/agent/.serena"},
    {"type": "volume", "source": "kimi-assets", "target": "/opt/kimi-runtime"},
]
volumes += [
    bind(f"{snapshot}/{relative}", f"/workspace/{relative}")
    for relative in (
        ".kimi-code/agents",
        ".agents/agents",
        ".kimi-code/skills",
        ".agents/skills",
        ".kimi-code/mcp.json",
    )
]
print(
    json.dumps(
        {
            "services": {
                "model-proxy": {"read_only": True, "secrets": [{"source": "proxy_internal_token"}]},
                "kimi-agent": {
                    "read_only": True,
                    "volumes": volumes,
                    "ports": [{"host_ip": "127.0.0.1", "published": "5494", "target": 5494}],
                },
            }
        }
    )
)
PY
""",
        )
        self.command(
            "docker",
            """
if [[ "$*" == *"config --environment" ]]; then cat "$TEST_BOOTSTRAP"; exit 0; fi
if [[ "$*" == *"config --format json" ]]; then "${RESOLVED_STUB:-resolved-config-clean}"; exit 0; fi
if [[ "$*" == *"kimi --version" ]]; then echo 0.42.0; exit 0; fi
if [[ "$*" == *"/register_workspace.py" ]]; then
  echo register-workspace >>"$TEST_EVENTS"; exit 0
fi
if [[ "$*" == *"/check_services.py" ]]; then echo check-services >>"$TEST_EVENTS"; exit 0; fi
if [[ "$*" == *"up --remove-orphans"* ]]; then sleep 1; exit 0; fi
exit 0
""",
        )
        result = self.run_script("start.sh", "--non-interactive")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            (self.base / "events").read_text().splitlines(),
            [
                "configure", "kimi-version", "module-version", "prepare", "install",
                "start", "register-workspace", "check-services",
            ],
        )
        data = self.workspace / "demo/user/data"
        data.write_text("keep")
        runtime = next((self.root / ".local/runtime").iterdir())
        staged = (runtime / "SYSTEM.md").read_text()
        self.assertIn("Demo instructions", staged)
        self.assertIn("Model runtime envelope", staged)
        # The workspace's own guidance file belongs to the project being worked on, so the
        # harness must never write inside it - not for the envelope, and not for module text.
        self.assertFalse((self.workspace / "AGENTS.md").exists())
        self.env["HARNESS_MODULES"] = ""
        (self.base / "events").write_text("")
        result = self.run_script("start.sh", "--non-interactive")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            (self.base / "events").read_text(),
            "kimi-version\nregister-workspace\ncheck-services\n",
        )
        self.assertEqual(data.read_text(), "keep")
        staged = (runtime / "SYSTEM.md").read_text()
        self.assertNotIn("Demo instructions", staged)
        self.assertIn("Model runtime envelope", staged)
        self.assertFalse((self.workspace / "AGENTS.md").exists())
        self.assertEqual((runtime / "last-modules.json").read_text().strip(), "[]")
        self.assertFalse((runtime / "module.env").exists())
        # Session material is deleted on exit, but the selection survives in the "last used"
        # copy so the next launch can label and pre-select it.
        self.assertFalse(list((runtime / "credentials").glob("*")))
        self.assertFalse((runtime / "model-policy.json").exists())
        self.assertFalse((runtime / "model.env").exists())
        self.assertFalse((runtime / "model-selection.json").exists())
        self.assertIn("primary", (runtime / "last-model-selection.json").read_text())
        # The omission has to survive the whole path: `.env`, the bootstrap declaration, the
        # resolved environment, and the staged prompt. An empty prompt file is the operator asking
        # for no prompt, and Kimi Code throws a blank one away, so what must reach the volume is the
        # one-token sentinel rather than nothing.
        (self.root / "SYSTEM.md").write_text("")
        self.bootstrap.write_text(
            f"WORKSPACE_PATH={self.workspace}\nNRP_API_KEY=fixture-key\n"
            "KIMI_SYSTEM_PROMPT_OMIT_ENVELOPE=1\n"
        )
        result = self.run_script("start.sh", "--non-interactive")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((runtime / "SYSTEM.md").read_text(), ".\n")
        self.assertFalse((self.workspace / "AGENTS.md").exists())
        # A resolved configuration that hands the agent an arbitrary host path has to stop
        # the launch before anything is started.
        self.command(
            "resolved-config-rogue",
            """
resolved-config-clean | "$TEST_REAL_PYTHON" -c '
import json, sys
configuration = json.load(sys.stdin)
configuration["services"]["kimi-agent"]["volumes"].append(
    {"type": "bind", "source": "/etc", "target": "/mnt/etc", "read_only": True,
     "bind": {"create_host_path": False}}
)
print(json.dumps(configuration))
'
""",
        )
        (self.base / "events").write_text("")
        self.env["RESOLVED_STUB"] = "resolved-config-rogue"
        result = self.run_script("start.sh", "--non-interactive")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("exposes host bind", result.stderr)
        self.assertNotIn("register-workspace", (self.base / "events").read_text())


if __name__ == "__main__":
    unittest.main()
