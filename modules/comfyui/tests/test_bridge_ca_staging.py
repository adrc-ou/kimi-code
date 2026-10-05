"""The bridge certificate has to survive being staged a second time.

`comfy-bridge-ca-init` writes into a named volume, and named volumes outlive a launch: `down` keeps
them and the compose project name is the instance id, so the second launch of a workspace starts
with the first launch's certificate already in place — and left mode 0444, which is what makes the
second write interesting. This runs that service's own command against a directory in exactly that
state, with the container's absolute paths rewritten into a temporary tree and the same `sh -ec` the
service's entrypoint uses.
"""

import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "compose.mps.yaml"
SERVICE = "comfy-bridge-ca-init"


def staging_command() -> str:
    """The one command the CA one-shot runs, read out of the service that runs it."""
    after_service = COMPOSE.read_text(encoding="utf-8").split(f"{SERVICE}:", 1)[1]
    found = re.search(r'^\s+command: \["(.+)"\]', after_service, re.MULTILINE)
    assert found, f"{SERVICE} declares no single command in {COMPOSE.name}"
    return found.group(1)


class BridgeCaStagingTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.directory, True)
        self.source = self.directory / "bridge.crt"
        self.source.write_text("this launch's certificate\n", encoding="utf-8")
        self.staged = self.directory / "staged"
        self.staged.mkdir()
        self.destination = self.staged / "ca.crt"

    def leave_a_staged_certificate(self) -> None:
        """What the previous launch left behind, in the mode it left it in."""
        self.destination.write_text("last launch's certificate\n", encoding="utf-8")
        self.destination.chmod(0o444)

    def run_staging(self) -> subprocess.CompletedProcess:
        command = staging_command().replace("/in/bridge.crt", str(self.source)).replace(
            "/staged", str(self.staged)
        )
        return subprocess.run(
            ["/bin/sh", "-ec", command], capture_output=True, text=True, timeout=60
        )

    def test_an_empty_volume_gets_the_certificate(self):
        result = self.run_staging()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self.destination.read_text(encoding="utf-8"), "this launch's certificate\n"
        )

    def test_a_launch_after_another_replaces_the_certificate_the_first_left(self):
        # The regression: `cp` opens its destination with O_TRUNC, and a 0444 file cannot be opened
        # for writing by anyone without CAP_FOWNER — which the service drops with every other
        # capability — so the second launch of a workspace failed here and took the stack with it.
        self.leave_a_staged_certificate()
        result = self.run_staging()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self.destination.read_text(encoding="utf-8"), "this launch's certificate\n"
        )

    def test_the_staged_certificate_stays_unwritable(self):
        # Keeping 0444 is the point: the agent mounts this volume to decide whether to trust the
        # bridge, so it must not be able to trade the file for one it issued itself.
        self.leave_a_staged_certificate()
        self.assertEqual(self.run_staging().returncode, 0)
        self.assertEqual(stat.S_IMODE(self.destination.stat().st_mode), 0o444)

    def test_the_command_replaces_the_file_rather_than_writing_into_it(self):
        # Structural, and needed alongside the run above: a suite executing as root with its
        # capabilities intact could truncate anything, so only an unprivileged run makes the test
        # above bite. Nothing in this file may write the staged path in place.
        command = staging_command()
        self.assertNotIn("cp ", command)
        self.assertNotIn(">", command)


if __name__ == "__main__":
    unittest.main()
