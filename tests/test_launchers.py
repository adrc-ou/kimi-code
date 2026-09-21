"""Exercise host entrypoints without operator credentials or a running stack."""

import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for _directory in (ROOT, ROOT / "tools"):
    if str(_directory) not in sys.path:
        sys.path.insert(0, str(_directory))

import prompt_context  # noqa: E402
from tests.helpers import run_in_pty  # noqa: E402


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
            "prompts.sh",
            "tools/runtime.sh",
            "tools/modules.py",
            "tools/private_file.py",
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
            "runtime/config-policy.json",
            "compose.bootstrap.yaml",
        ):
            destination = self.root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / relative, destination)
        for tree in ("models", "providers"):
            shutil.copytree(ROOT / tree, self.root / tree)
        # The module picker renders through the shared modal engine, so a launcher fixture has to
        # carry it: tools/modules.py is run as a script, which puts tools/ alone on the path.
        shutil.copytree(
            ROOT / "tools" / "tui",
            self.root / "tools" / "tui",
            ignore=shutil.ignore_patterns("__pycache__"),
        )
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
if [[ "$*" == *"/check_services.py"* ]]; then echo check-services >>"$TEST_EVENTS"; exit 0; fi
if [[ "$*" == *"cp kimi-agent:"* ]]; then echo '{"mode":"fixture"}' >"${@: -1}"; exit 0; fi
if [[ "$*" == *"logs --no-color"* ]]; then echo browser >>"$TEST_EVENTS"; exit 0; fi
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
                "start", "register-workspace", "check-services", "browser",
            ],
        )
        data = self.workspace / "demo/user/data"
        data.write_text("keep")
        runtime = next((self.root / ".local/runtime").iterdir())
        # The check runs before the browser is opened, so a probe never competes with the first
        # session it displaces, and its report is carried out of the container and left private.
        report = runtime / "service-check.json"
        self.assertEqual(os.stat(report).st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(report.read_text()), {"mode": "fixture"})
        # A module's own guidance has exactly one route to the agent, and it is the all-lane
        # contract rather than the main agent's voice.
        contract = (runtime / "AGENTS.md").read_text()
        self.assertIn("Demo instructions", contract)
        self.assertIn("Model usage limits", contract)
        staged = (runtime / "SYSTEM.md").read_text()
        self.assertIn("Model runtime envelope", staged)
        self.assertNotIn("Demo instructions", staged)
        # The workspace's own guidance file belongs to the project being worked on, so the
        # harness must never write inside it - not for the envelope, and not for module text.
        self.assertFalse((self.workspace / "AGENTS.md").exists())
        self.env["HARNESS_MODULES"] = ""
        (self.base / "events").write_text("")
        # A launch that died mid-pass leaves its state machine and its secret answers behind,
        # and the next one must sweep both rather than mistake the first for progress and the
        # second for memory. Seeded here because the headless path creates neither file.
        (runtime / "flow-state.json").write_text(
            '{"live":true,"steps":["model"],"committed":{"model":"stub"}}'
        )
        (runtime / "module-values.json").write_text('{"DEMO_KEY":"stub-secret"}')
        result = self.run_script("start.sh", "--non-interactive")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            (self.base / "events").read_text(),
            "kimi-version\nregister-workspace\ncheck-services\nbrowser\n",
        )
        self.assertEqual(data.read_text(), "keep")
        self.assertNotIn("Demo instructions", (runtime / "AGENTS.md").read_text())
        self.assertIn("Model usage limits", (runtime / "AGENTS.md").read_text())
        self.assertIn("Model runtime envelope", (runtime / "SYSTEM.md").read_text())
        self.assertFalse((self.workspace / "AGENTS.md").exists())
        self.assertEqual((runtime / "last-modules.json").read_text().strip(), "[]")
        self.assertFalse((runtime / "module.env").exists())
        # Session material is deleted on exit, but the selection survives in the "last used"
        # copy so the next launch can label and pre-select it.
        self.assertFalse(list((runtime / "credentials").glob("*")))
        self.assertFalse((runtime / "model-policy.json").exists())
        self.assertFalse((runtime / "model.env").exists())
        self.assertFalse((runtime / "model-selection.json").exists())
        # The two files seeded above are gone, so the next launch starts from the answers it can
        # actually see rather than from a dead launch's notion of how far it got.
        self.assertFalse((runtime / "flow-state.json").exists())
        self.assertFalse((runtime / "module-values.json").exists())
        self.assertIn("primary", (runtime / "last-model-selection.json").read_text())
        # The panel's choices are memory: they survive the launch that made them and are honoured
        # by the next, which is the only way a headless session can express a selection at all.
        # An empty prompt file is the operator asking for no prompt, and Kimi Code throws a blank
        # one away, so what must reach the volume is the one-token sentinel rather than nothing.
        (self.root / "SYSTEM.md").write_text("")
        prompt_context.save_prefs(
            runtime / prompt_context.PREFS_FILE,
            dict.fromkeys(prompt_context.OPTION_IDS, False),
        )
        self.bootstrap.write_text(f"WORKSPACE_PATH={self.workspace}\nNRP_API_KEY=fixture-key\n")
        result = self.run_script("start.sh", "--non-interactive")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((runtime / "SYSTEM.md").read_text(), ".\n")
        self.assertFalse((self.workspace / "AGENTS.md").exists())
        # Remembered choices are not wiped by the launch that read them.
        self.assertTrue((runtime / prompt_context.PREFS_FILE).is_file())
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


class ModuleHookTerminalTests(unittest.TestCase):
    """``harness_modules`` calls a hook with the launcher's own stdin, not with its module list.

    Every phase runs in the launcher's shell, and one of them draws a screen: the version menu a
    module's ``select_version`` hook puts up takes its keystrokes from stdin. Reading the list into
    the loop that runs the hooks replaces that stdin with the rest of a text file, and the selector
    answers by refusing to draw a menu it cannot get keys from. It is a module-only failure and an
    interactive one, so nothing else in a suite that runs everything through pipes can see it.
    """

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        base = Path(self.temporary.name).resolve()
        self.root = base / "harness"
        self.runtime = base / "runtime"
        self.runtime.mkdir(parents=True)
        # Both halves of the contract a hook needs from its caller: that the terminal is still
        # there to read keys from, and that MODULE_DIR names the module being asked.
        hook = """
module_select_version() {
  python3 -c 'import sys; print("TTY" if sys.stdin.isatty() else "REDIRECTED", flush=True)'
  echo "HOOK:${MODULE_DIR##*/}"
}
"""
        for name in ("demo", "second"):
            module = self.root / "modules" / name
            module.mkdir(parents=True)
            (module / "module.sh").write_text(hook)

    def run_phase(self, listing: str, phase: str = "select_version"):
        """Run one hook phase over ``listing`` with the launcher's terminal attached to stdin."""
        (self.runtime / "modules.list").write_text(listing)
        script = f"""
set -euo pipefail
source {shlex.quote(str(ROOT / "tools" / "runtime.sh"))}
HARNESS_ROOT={shlex.quote(str(self.root))}
HARNESS_RUNTIME_DIR={shlex.quote(str(self.runtime))}
harness_modules {phase}
"""
        return run_in_pty(["bash", "-c", script], timeout=20)

    def test_every_module_is_asked_and_keeps_the_terminal(self):
        session = self.run_phase("demo\nsecond\n")
        self.assertEqual(session.status, 0, session.screen)
        self.assertEqual(
            session.screen.split(), ["TTY", "HOOK:demo", "TTY", "HOOK:second"]
        )

    def test_last_module_needs_no_trailing_newline(self):
        session = self.run_phase("demo")
        self.assertEqual(session.status, 0, session.screen)
        self.assertEqual(session.screen.split(), ["TTY", "HOOK:demo"])

    def test_no_module_selected_is_not_a_failure(self):
        # ``tools/modules.py`` writes an empty list when nothing is selected, and the launcher
        # still runs every phase against it.
        session = self.run_phase("")
        self.assertEqual(session.status, 0, session.screen)
        self.assertEqual(session.screen.split(), [])

    def test_invalid_identifier_stops_the_launch(self):
        session = self.run_phase("demo\nBad Module\n")
        self.assertNotEqual(session.status, 0)
        self.assertIn("Invalid module identifier", session.screen)
        self.assertEqual(session.screen.split()[:2], ["TTY", "HOOK:demo"])

    def test_a_module_that_implements_the_phase_is_not_a_failure(self):
        # ``configure`` runs on every launch and most modules have nothing to say in it; an empty
        # module.sh is the shape of that, and it must stay quiet rather than trip ``set -e``.
        (self.root / "modules" / "demo" / "module.sh").write_text("")
        session = self.run_phase("demo\n", phase="configure")
        self.assertEqual(session.status, 0, session.screen)
        self.assertEqual(session.screen.split(), [])


class PromptsScriptTests(unittest.TestCase):
    """``./prompts.sh`` is a dispatcher, so these tests check the dispatch and the guards.

    A full panel render needs the resolved policy plan, which only a real launch writes. Rather
    than reproduce that here - ``test_prompt_panel.py`` already covers the drawing - these tests
    drive every mode reachable without a plan and hold the plan-hungry mode to the panel's own
    complaint, which is the behaviour an operator actually meets when they run it too early.
    """

    command = LauncherTests.command
    run_script = LauncherTests.run_script
    require_executable_fixtures = LauncherTests.require_executable_fixtures

    def setUp(self) -> None:
        LauncherTests.setUp(self)
        # The panel is a real program in this repo, so the stubbed launcher runs the real one.
        shutil.copytree(ROOT / "tools", self.root / "tools", dirs_exist_ok=True)
        shutil.copytree(ROOT / "runtime", self.root / "runtime", dirs_exist_ok=True)

    def instance_runtime_dir(self) -> Path:
        """The instance directory the launcher would compute for this fixture.

        Mirrors ``harness_instance()``: a digest of root, workspace and platform, truncated. The
        platform is what the ``uname`` stub in ``setUp`` reports.
        """
        material = b"\0".join(
            (str(self.root).encode(), str(self.workspace).encode(), b"darwin-arm64")
        )
        return self.root / ".local/runtime" / hashlib.sha256(material).hexdigest()[:16]

    def with_plan(self, plan: object) -> Path:
        runtime = self.instance_runtime_dir()
        runtime.mkdir(parents=True, exist_ok=True)
        (runtime / "model-policy.json").write_text(json.dumps(plan), encoding="utf-8")
        return runtime

    def test_unknown_mode_is_usage_not_a_crash(self):
        result = self.run_script("prompts.sh", "--nonsense")
        self.assertEqual(result.returncode, 2)
        self.assertIn("usage: ./prompts.sh", result.stderr)

    def test_extra_arguments_are_refused_before_the_runtime_is_read(self):
        result = self.run_script("prompts.sh", "--vars", "spurious")
        self.assertEqual(result.returncode, 2)
        self.assertIn("usage:", result.stderr)

    def test_missing_runtime_says_so_instead_of_printing_nothing(self):
        result = self.run_script("prompts.sh")
        self.assertEqual(result.returncode, 1)
        self.assertIn("No prepared runtime", result.stderr)

    def test_configure_lists_every_shipped_option_without_reading_the_plan(self):
        self.with_plan({})
        result = self.run_script("prompts.sh", "--configure", "--show")
        self.assertEqual(result.returncode, 0, result.stderr)
        for option_id in prompt_context.OPTION_IDS:
            self.assertIn(option_id, result.stdout)

    def test_configure_writes_the_file_the_launcher_composes_from(self):
        runtime = self.with_plan({})
        result = self.run_script("prompts.sh", "--configure", "--disable", "lane_table")
        self.assertEqual(result.returncode, 0, result.stderr)
        prefs = json.loads((runtime / prompt_context.PREFS_FILE).read_text(encoding="utf-8"))
        self.assertFalse(prefs["lane_table"])
        self.assertTrue(prefs["module_guidance"])

    def test_configure_rejects_an_option_it_does_not_own(self):
        self.with_plan({})
        result = self.run_script("prompts.sh", "--configure", "--disable", "not_an_option")
        self.assertEqual(result.returncode, 2)
        self.assertIn("valid ids", result.stderr)

    def test_show_refuses_to_price_a_plan_it_cannot_resolve(self):
        self.with_plan({})
        result = self.run_script("prompts.sh", "--show")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("the panel needs the resolved policy plan", result.stderr)

    def test_vars_names_both_placeholder_families_and_the_read_only_tables(self):
        self.with_plan({})
        result = self.run_script("prompts.sh", "--vars")
        self.assertEqual(result.returncode, 0, result.stderr)
        for expected in ("base_prompt", "harness.date", "kimi.coder_role"):
            self.assertIn(expected, result.stdout)
        # The operator's own config tables are evidence here, never a checkbox: they are not the
        # harness's to rewrite, so the listing has to say so in the operator's words.
        self.assertIn("[experimental]", result.stdout)

    def test_the_container_backed_modes_name_the_stack_as_the_missing_piece(self):
        self.command(
            "docker",
            """
if [[ "$*" == "compose version" || "$*" == "info" ]]; then exit 0; fi
if [[ "$*" == *"config --environment" ]]; then cat "$TEST_BOOTSTRAP"; exit 0; fi
if [[ "$1" == ps ]]; then exit 0; fi
echo "Unexpected Docker call" >&2
exit 99
""",
        )
        self.with_plan({})
        for mode in ("--live", "--extract"):
            with self.subTest(mode=mode):
                result = self.run_script("prompts.sh", mode)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn("is not running", result.stderr)


class RetiredEntrypointTests(unittest.TestCase):
    """`./doctor.sh` was a habit, and habits are what the launch is supposed to replace."""

    def test_the_standalone_doctor_is_gone_and_unmentioned(self):
        # Its value - the deep pass, the stale-prompt notice, a check that runs without being asked
        # for - now happens on every launch. A reference that outlives the file would teach the
        # next reader to run a command that no longer exists, so none may remain.
        self.assertFalse((ROOT / "doctor.sh").exists(), "the retired launcher is back")
        for path in sorted(ROOT.rglob("*")):
            if (
                not path.is_file()
                or ".git" in path.parts
                or ".local" in path.parts
                or ".agent-state" in path.parts
                or "__pycache__" in path.parts
                or ".ruff_cache" in path.parts
                or path.suffix in {".so", ".pyc"}
            ):
                continue
            try:
                text = path.read_text()
            except (UnicodeDecodeError, OSError):
                continue
            where = path.relative_to(ROOT).as_posix()
            if where == "tests/test_launchers.py":
                continue  # this test, and only this one
            self.assertNotIn("doctor.sh", text, f"{where} still points at ./doctor.sh")


if __name__ == "__main__":
    unittest.main()
