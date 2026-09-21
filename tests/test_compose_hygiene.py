"""Compose hygiene assertions confine the agent's mounts, ports and credentials."""

import importlib.util
import io
import json
import re
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("compose_hygiene", ROOT / "tools/compose_hygiene.py")
hygiene = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hygiene)


def bind(source, target, read_only=True, create_host_path=False):
    return {
        "type": "bind",
        "source": str(source),
        "target": str(target),
        "read_only": read_only,
        "bind": {"create_host_path": create_host_path},
    }


def volume(name, target):
    return {"type": "volume", "source": name, "target": str(target)}


def base(root):
    workspace = root / "workspace"
    runtime = root / "runtime"
    services = {
        "agent-state-init": {
            "read_only": True,
            "volumes": [
                {"type": "bind", "source": str(root / "container/init.py"), "target": "/init.py"}
            ],
        },
        "model-proxy": {
            "read_only": True,
            "volumes": [volume("proxy-cache", "/var/cache")],
            # Compose reports a secret's name here, not its file: the path lives in the
            # top-level secrets section, so a service can never learn one this way.
            "secrets": [{"source": "nrp__default"}],
        },
        "kimi-agent": {
            "read_only": True,
            "volumes": [
                bind(workspace, "/workspace", read_only=False),
                volume("kimi-state", "/home/agent/.kimi-code"),
                volume("serena-state", "/home/agent/.serena"),
                volume("kimi-assets", "/opt/kimi-runtime"),
            ],
            "ports": [{"host_ip": "127.0.0.1", "published": "5494", "target": 5494}],
        },
    }
    return services, str(workspace), str(runtime)


class ComposeHygieneTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.services, self.workspace, self.runtime = base(self.root)

    def agent_volumes(self):
        return self.services["kimi-agent"]["volumes"]

    def check_mounts(self):
        hygiene.check_agent_mounts(self.services, self.workspace, self.runtime)

    def test_conforming_configuration_passes(self):
        """The fixture is the launch shape, so every check must accept it.

        One subTest per check: a bare sequence would stop at the first refusal and report only
        that the fixture is wrong, not which of the four guards it trips.
        """
        for name, check in (
            ("mounts", self.check_mounts),
            ("credentials", lambda: hygiene.check_credentials(
                self.services, self.runtime, "nrp__default")),
            ("read-only roots", lambda: hygiene.check_read_only_roots(self.services)),
            ("loopback ports", lambda: hygiene.check_loopback_ports(self.services)),
        ):
            with self.subTest(check=name):
                check()

    def test_extension_snapshot_bind_is_allowed(self):
        snapshot = Path(self.runtime) / "extension-snapshot"
        for relative in (".kimi-code/skills", ".agents/agents", ".kimi-code/mcp.json"):
            self.agent_volumes().append(bind(snapshot / relative, f"/workspace/{relative}"))
        self.check_mounts()

    def test_snapshot_bind_outside_privileged_targets_is_refused(self):
        snapshot = Path(self.runtime) / "extension-snapshot"
        self.agent_volumes().append(bind(snapshot / "home/agent", "/home/agent/.ssh"))
        with self.assertRaisesRegex(SystemExit, "exposes host bind"):
            self.check_mounts()

    def test_arbitrary_host_bind_on_the_agent_is_refused(self):
        self.agent_volumes().append(bind(self.root / "elsewhere", "/elsewhere"))
        with self.assertRaisesRegex(SystemExit, "exposes host bind"):
            self.check_mounts()

    def test_runtime_directory_bind_on_the_agent_is_refused(self):
        self.agent_volumes().append(bind(Path(self.runtime) / "config.toml", "/run/config.toml"))
        with self.assertRaisesRegex(SystemExit, "exposes host bind"):
            self.check_mounts()

    def test_writable_agent_bind_is_refused(self):
        self.agent_volumes().append(bind(self.root / "other", "/other", read_only=False))
        with self.assertRaisesRegex(SystemExit, "not read-only"):
            self.check_mounts()

    def test_agent_bind_that_may_create_a_host_path_is_refused(self):
        self.agent_volumes().append(bind(self.root / "other", "/other", create_host_path=True))
        with self.assertRaisesRegex(SystemExit, "create a host path"):
            self.check_mounts()

    def test_agent_bind_omitting_create_host_path_is_refused_with_a_compose_hint(self):
        # Compose before v5.0.2 serialises an explicit false as a missing key. The gate still
        # fails closed, but the message must name that cause rather than accuse the mount.
        mount = bind(self.root / "other", "/other")
        del mount["bind"]["create_host_path"]
        self.agent_volumes().append(mount)
        with self.assertRaisesRegex(SystemExit, "does not state create_host_path"):
            self.check_mounts()

    def test_missing_state_volume_is_refused(self):
        self.agent_volumes()[:] = [
            mount
            for mount in self.agent_volumes()
            if mount.get("source") != "kimi-state"
        ]
        with self.assertRaisesRegex(SystemExit, "state volumes"):
            self.check_mounts()

    def test_unrecognised_mount_shape_fails_closed(self):
        self.agent_volumes()[:] = ["kimi-state:/home/agent/.kimi-code"]
        with self.assertRaisesRegex(SystemExit, "unrecognised or unexpected mount entry"):
            self.check_mounts()

    def test_public_port_is_refused(self):
        self.services["kimi-agent"]["ports"] = [{"published": "8188", "target": 8188}]
        with self.assertRaisesRegex(SystemExit, "all interfaces"):
            hygiene.check_loopback_ports(self.services)

    def test_wildcard_public_port_is_refused(self):
        # The fixture deliberately spells the shape the check exists to reject.
        self.services["kimi-agent"]["ports"] = [
            {"host_ip": "0.0.0.0", "published": "5494"}  # noqa: S104
        ]
        with self.assertRaisesRegex(SystemExit, r"publishes 5494 on 0\.0\.0\.0"):
            hygiene.check_loopback_ports(self.services)

    def test_service_losing_read_only_root_is_refused(self):
        self.services["model-proxy"]["read_only"] = False
        with self.assertRaisesRegex(SystemExit, "read-only root filesystem"):
            hygiene.check_read_only_roots(self.services)

    def test_secret_mounted_anywhere_but_the_proxy_is_refused(self):
        key = str(Path(self.runtime) / "credentials/nrp__default")
        self.services["kimi-agent"]["secrets"] = [{"source": key}]
        with self.assertRaisesRegex(SystemExit, "only model-proxy may hold secrets"):
            hygiene.check_credentials(self.services, self.runtime, None)

    def test_expected_credential_must_reach_the_proxy(self):
        self.services["model-proxy"]["secrets"] = []
        with self.assertRaisesRegex(SystemExit, "did not reach model-proxy alone"):
            hygiene.check_credentials(self.services, self.runtime, "nrp__default")

    def test_credential_path_bind_is_refused_for_every_service(self):
        key = Path(self.runtime) / "credentials/nrp__default"
        self.agent_volumes().append(bind(key, "/run/key"))
        with self.assertRaisesRegex(SystemExit, "bind-mounts a credential path"):
            hygiene.check_credentials(self.services, self.runtime, None)

    def test_main_reports_success_for_a_clean_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            services, workspace, runtime = base(Path(directory).resolve())
            argv = [
                "compose_hygiene.py",
                "--workspace",
                workspace,
                "--runtime-dir",
                runtime,
                "--expect-secret",
                "nrp__default",
                "--label",
                "fixture",
            ]
            out = io.StringIO()
            with (
                patch.object(sys, "stdin", io.StringIO(json.dumps({"services": services}))),
                patch.object(sys, "argv", argv),
                redirect_stdout(out),
            ):
                hygiene.main()
            self.assertIn("are confined", out.getvalue())

    def test_main_fails_closed_without_a_services_section(self):
        argv = ["compose_hygiene.py", "--workspace", "/w", "--runtime-dir", "/r"]
        with (
            patch.object(sys, "stdin", io.StringIO("{}")),
            patch.object(sys, "argv", argv),
            self.assertRaisesRegex(SystemExit, "no services section"),
        ):
            hygiene.main()


class ConfigScriptSuppliesRequiredNamesTests(unittest.TestCase):
    """A ``${NAME:?…}`` the fixture script never exports fails the check that uses it."""

    COMPOSE_FILES = ("compose.yaml", "compose.search.yaml")
    SCRIPT = Path("tests/compose-config.sh")

    def test_every_required_name_is_exported_by_the_fixture(self):
        required: set[str] = set()
        for name in self.COMPOSE_FILES:
            required |= set(
                re.findall(r"\$\{([A-Z0-9_]+):\?", (ROOT / name).read_text(encoding="utf-8"))
            )
        self.assertTrue(required, "no Compose interpolation guards found to check")
        script = (ROOT / self.SCRIPT).read_text(encoding="utf-8")
        exported = set(re.findall(r"^export ([A-Z0-9_]+)=", script, re.MULTILINE))
        missing = sorted(required - exported)
        self.assertEqual(
            missing,
            [],
            f"{self.SCRIPT} must export these, or the check aborts on the guard",
        )


if __name__ == "__main__":
    unittest.main()
