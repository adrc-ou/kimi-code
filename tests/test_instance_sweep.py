#!/usr/bin/env python3
"""The launcher's residue sweep, exercised against a directory rather than a whole launch.

A killed launcher runs no shutdown, so the only thing standing between a provider key and an
indefinite life on disk is the next launch clearing the instance directory before it renders
anything. That is a shell function, so it is tested as one: source `tools/runtime.sh`, point it at
a temporary directory, and look at what survived. Two passes get exercised: the one that clears the
instance directory this launch is using, and the one that reaches the sibling directories left by a
checkout that has moved since.
"""

import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: What must never outlive the launch that wrote it, in the three shapes the sweep recognises.
SECRET_RESIDUE = (
    "credentials/nrp__api_key",
    "credentials/deselected__key",
    "nrp-api-key",
    "bridge-token",
    "nrp__api_key",
    "bootstrap.Abc123",
)
#: What the sweep has no business touching: deliberate cross-launch memory, and generated state
#: that the rest of the startup already overwrites.
KEEP = (
    "cache-salt",
    "state.env",
    "last-modules.json",
    "last-model-selection.json",
    "prompt-context.json",
    "prompt-measurements.jsonl",
    "service-check.json",
    "kimi-prompts/literals.json",
    "compose/approved-extensions.yaml",
    "prompt-log/prompt-20260921T030721-964055-bede5997.txt",
)


def sweep(runtime: Path) -> subprocess.CompletedProcess[str]:
    script = f"""
set -euo pipefail
source {shlex.quote(str(ROOT / "tools" / "runtime.sh"))}
harness_sweep_secrets {shlex.quote(str(runtime))}
"""
    return subprocess.run(
        ["bash", "-c", script], check=False, capture_output=True, text=True
    )


def sweep_siblings(root: Path, current: Path) -> subprocess.CompletedProcess[str]:
    """The stale pass only ever runs from a launcher, which has already set these two."""
    script = f"""
set -euo pipefail
source {shlex.quote(str(ROOT / "tools" / "runtime.sh"))}
HARNESS_ROOT={shlex.quote(str(ROOT))}
HARNESS_RUNTIME_DIR={shlex.quote(str(current))}
harness_sweep_stale_instances {shlex.quote(str(root))}
"""
    return subprocess.run(
        ["bash", "-c", script], check=False, capture_output=True, text=True
    )


#: A launcher holds its lock for the whole launch, so the only way to hand a test a genuinely held
#: lock is to let a process keep it. The pid must stay out of the assertion: the sweep is meant to
#: ignore it.
HOLD_LOCK = """
import fcntl, os, signal, sys
fd = os.open(sys.argv[1], os.O_WRONLY | os.O_CREAT, 0o600)
fcntl.flock(fd, fcntl.LOCK_EX)
os.write(1, b"held\\n")
signal.signal(signal.SIGTERM, lambda *_: os._exit(0))
signal.pause()
"""


def populate(directory: Path) -> None:
    """An instance directory as a launch would leave it: residue and memory side by side."""
    for relative in (*SECRET_RESIDUE, *KEEP):
        path = directory / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("fixture\n")


class InstanceSweepTests(unittest.TestCase):
    def setUp(self):
        # The guard on the argument is part of the contract, so the fixture has to look like the
        # real thing: an instance directory under .local/runtime.
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name) / "checkout" / ".local" / "runtime"
        self.runtime = self.base / "eda3f42afd9d9d41"

    def populate(self) -> None:
        self.runtime.mkdir(parents=True)
        populate(self.runtime)

    def test_a_key_never_survives_into_the_next_launch(self):
        self.populate()
        result = sweep(self.runtime)
        self.assertEqual(result.returncode, 0, result.stderr)
        for relative in SECRET_RESIDUE:
            self.assertFalse((self.runtime / relative).exists(), relative)
        # The directory itself is materialise_credentials() to own and refill.
        self.assertTrue((self.runtime / "credentials").is_dir())

    def test_memory_outlives_the_sweep_that_clears_the_keys(self):
        self.populate()
        sweep(self.runtime)
        for relative in KEEP:
            self.assertTrue((self.runtime / relative).exists(), relative)
        self.assertEqual((self.runtime / "cache-salt").read_text(), "fixture\n")

    def test_an_unrelated_name_shaped_like_a_secret_is_left_alone(self):
        self.populate()
        # Only the instance root is swept, and only for the shapes the sweep documents.
        (self.runtime / "compose" / "model-policy.json").write_text("{}\n")
        (self.runtime / "modules.list").write_text("comfyui\n")
        sweep(self.runtime)
        self.assertTrue((self.runtime / "compose/model-policy.json").exists())
        self.assertTrue((self.runtime / "modules.list").exists())

    def test_a_path_that_is_not_an_instance_directory_is_refused(self):
        self.populate()
        elsewhere = Path(self.temporary.name) / "checkout" / "somewhere"
        elsewhere.mkdir()
        (elsewhere / "nrp-api-key").write_text("fixture\n")
        result = sweep(elsewhere)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Refusing to sweep", result.stderr)
        self.assertTrue((elsewhere / "nrp-api-key").exists())

    def test_nothing_to_clean_is_not_a_failure(self):
        with self.subTest("a directory that does not exist"):
            self.assertEqual(sweep(self.base / "never-created").returncode, 0)
        with self.subTest("no instance directory named at all"):
            result = subprocess.run(
                [
                    "bash",
                    "-c",
                    f"set -euo pipefail; source {shlex.quote(str(ROOT / 'tools/runtime.sh'))};"
                    " HARNESS_RUNTIME_DIR= harness_sweep_secrets",
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)


class StaleInstanceTests(unittest.TestCase):
    """The pass that reaches the instance directories this launch will never name.

    The instance id digests the checkout path and the workspace path, so a moved repository leaves
    its whole previous directory behind, keys and all, and nothing in a later launch refers to it
    by name.
    """

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name) / "checkout" / ".local" / "runtime"
        self.live = self.base / "1111111111111111"
        self.orphan = self.base / "eda3f42afd9d9d41"
        self.live.mkdir(parents=True)
        self.orphan.mkdir(parents=True)
        populate(self.orphan)
        populate(self.live)

    def run_sweep(self) -> subprocess.CompletedProcess[str]:
        return sweep_siblings(self.base, self.live)

    def test_an_orphaned_checkout_loses_its_key(self):
        result = self.run_sweep()
        self.assertEqual(result.returncode, 0, result.stderr)
        for relative in SECRET_RESIDUE:
            self.assertFalse((self.orphan / relative).exists(), relative)
        for relative in KEEP:
            self.assertTrue((self.orphan / relative).exists(), relative)

    def test_the_launching_instance_is_left_to_its_own_sweep(self):
        self.run_sweep()
        for relative in SECRET_RESIDUE:
            self.assertTrue((self.live / relative).exists(), relative)

    def test_a_stale_pid_in_the_lock_file_claims_nothing(self):
        # What a killed launcher actually leaves behind: the line, but no holder. The pid is a
        # number a reboot hands to somebody else, so trusting it would protect the key forever.
        (self.orphan / "launcher.lock").write_text(f"pid={os.getpid() + 1_000_000}\n")
        self.run_sweep()
        self.assertFalse((self.orphan / "nrp-api-key").exists())

    def test_a_directory_another_launcher_holds_is_untouched(self):
        lock = self.orphan / "launcher.lock"
        holder = subprocess.Popen(
            [sys.executable, "-c", HOLD_LOCK, str(lock)],
            stdout=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(holder.terminate)
        try:
            assert holder.stdout is not None
            self.assertEqual(holder.stdout.readline().strip(), "held")
            self.run_sweep()
            for relative in SECRET_RESIDUE:
                self.assertTrue((self.orphan / relative).exists(), relative)
        finally:
            holder.terminate()
            holder.wait(timeout=10)
            assert holder.stdout is not None
            holder.stdout.close()

    def test_a_lock_that_cannot_be_read_is_treated_as_held(self):
        if os.geteuid() == 0:
            self.skipTest("root reads anything, so this proves nothing here")
        lock = self.orphan / "launcher.lock"
        lock.write_text("pid=1\n")
        lock.chmod(0o000)
        try:
            self.run_sweep()
            self.assertTrue((self.orphan / "nrp-api-key").exists())
        finally:
            lock.chmod(0o600)

    def test_a_sibling_that_is_not_a_directory_is_not_a_failure(self):
        (self.base / "stray-file").write_text("x\n")
        self.assertEqual(self.run_sweep().returncode, 0)

    def test_no_siblings_and_no_root_are_both_quiet(self):
        with self.subTest("a runtime root that does not exist"):
            self.assertEqual(
                sweep_siblings(
                    Path(self.temporary.name) / "never-created", self.live
                ).returncode,
                0,
            )
        with self.subTest("nothing but this launch"):
            for other in self.base.iterdir():
                if other != self.live:
                    shutil.rmtree(other)
            self.assertEqual(self.run_sweep().returncode, 0)


if __name__ == "__main__":
    unittest.main()
