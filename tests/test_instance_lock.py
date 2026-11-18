"""The launcher-lock probe, which is what decides whether a directory gets deleted.

`tools/runtime.sh` sweeps stale instance directories and asks this module whether each
``launcher.lock`` is held. Getting that answer wrong in the optimistic direction destroys a running
launch's policy files, credential mounts and resolved plan. So the four answers
below are the safety property, not trivia: free, held, absent, and unanswerable.

The probe deliberately ignores the ``pid=`` line inside the file. A process killed outright
leaves its number behind, and after a reboot a stranger owns it, so only the kernel's own advisory
lock can say whether anybody holds it now.
"""

from __future__ import annotations

import fcntl
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
for _directory in (ROOT, ROOT / "tests", ROOT / "tools", ROOT / "proxy"):
    if str(_directory) not in sys.path:
        sys.path.insert(0, str(_directory))

import instance_lock  # noqa: E402

PROC_FDS = Path("/proc/self/fd")


class IsFreeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.lock = Path(self._dir.name) / "launcher.lock"

    def hold(self):
        """Take the lock the way the launcher does, and return the descriptor to release it with."""
        fd = os.open(self.lock, os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.addCleanup(lambda: (fcntl.flock(fd, fcntl.LOCK_UN), os.close(fd)))
        return fd

    def test_a_missing_lock_is_free(self) -> None:
        # A launch creates the file before acquiring it, so absent must not read as "in use" or no
        # directory would ever be cleaned.
        self.assertTrue(instance_lock.is_free(self.lock))

    def test_an_unheld_lock_file_is_free(self) -> None:
        self.lock.write_text(f"pid={os.getpid() + 1_000_000}\n", encoding="utf-8")
        self.assertTrue(instance_lock.is_free(self.lock))

    def test_a_held_lock_is_not_free_however_its_pid_reads(self) -> None:
        # The whole point: this file names a live pid AND is locked, and the lock is what answers.
        self.hold()
        self.lock.write_text(f"pid={os.getpid()}\n", encoding="utf-8")
        self.assertFalse(instance_lock.is_free(self.lock))

    def test_a_name_that_is_not_a_file_is_not_free(self) -> None:
        # os.open() accepts a directory and flock() then refuses it. Reporting "free" here would let
        # a sweep delete a directory somebody named launcher.lock.
        self.lock.mkdir()
        self.assertFalse(instance_lock.is_free(self.lock))

    def test_the_probe_gives_back_the_descriptor_it_borrowed(self) -> None:
        if not PROC_FDS.is_dir():  # pragma: no cover - /proc is Linux's, the lock is not
            self.skipTest("descriptor counting needs /proc")
        self.hold()
        before = len(os.listdir(PROC_FDS))
        for _ in range(20):
            self.assertFalse(instance_lock.is_free(self.lock))
        self.assertEqual(len(os.listdir(PROC_FDS)), before, "one descriptor per refusal")


class CommandLineTests(unittest.TestCase):
    """The exit codes are the interface: `tools/runtime.sh` branches on them."""

    def run_main(self, *argv: str) -> int:
        with mock.patch.object(sys, "argv", ["instance_lock.py", *argv]):
            return instance_lock.main()

    def test_no_flag_answers_nothing(self) -> None:
        # Forgetting --free must not read as "free" through a bare successful exit.
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(SystemExit):
            self.run_main(tmp)

    def test_free_lock_exits_zero_and_a_held_one_exits_one(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "launcher.lock"
            self.assertEqual(self.run_main("--free", str(missing)), 0)
            fd = os.open(missing, os.O_RDWR | os.O_CREAT, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                self.assertEqual(self.run_main("--free", str(missing)), 1)
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)

    def test_a_directory_exits_one_rather_than_crashing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(self.run_main("--free", tmp), 1)


if __name__ == "__main__":
    unittest.main()
