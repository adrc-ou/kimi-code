#!/usr/bin/env python3
"""The suite's own terminal fixture, put under test.

``run_in_pty`` hands a real pty to a real process, so what it has to guarantee is not about the
program under test at all — it is about what is left behind. A child that survives its test keeps a
descriptor whose other end no longer exists, and a screen waiting on input from that descriptor used
to wait by repainting at full processor speed until the container was removed. Two of those, found
fifteen hours apart and burning a core each, are what this file exists for.

So these cases assert on processes rather than on bytes. What a terminal test is allowed to leave
running is part of what it means for the test to pass, because the alternative is a leak that shows
up as a warm machine days later and never as a red line here.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for _directory in (ROOT, ROOT / "tools"):
    if str(_directory) not in sys.path:
        sys.path.insert(0, str(_directory))

from tests.helpers import LEAKED_GROUPS, _group_gone, run_in_pty  # noqa: E402

#: How long a reaped child is given to leave the process table before it counts as a survivor. The
#: fixture already waits for its process group to empty, so this only absorbs the kernel's own
#: latency in the one case that matters: a process that will not be reaped at all.
REAP_GRACE = 5.0

#: A screen that has been left no terminal to read from, and the shape the launcher's own step has:
#: a process that hands the terminal to its child and exits, so the surviving screen is a grandchild
#: of whoever spawned the shell. It announces its pid on the terminal so the test can ask the
#: operating system what became of it afterwards.
NEVER_ENDS = """
import os
import sys
import time

if os.fork() > 0:
    # This is the process the fixture knows about. Leaving first is the point: the screen outlives
    # it, which is precisely what a teardown that only kills its own child cannot reach.
    sys.exit(0)
print("PID=%d" % os.getpid(), flush=True)
time.sleep(600)
"""


def alive(pid: int) -> bool:
    """Whether ``pid`` still names a process.

    ``ESRCH`` is the answer once the process is gone and waited on. A zombie still counts, which is
    the right reading here: a zombie is a child nobody reaped, not a screen still spinning.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class PtyReapTests(unittest.TestCase):
    """Nothing a terminal test starts may outlive it."""

    def setUp(self):
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.base = Path(holder.name).resolve()
        self.sleeper = self.base / "screen_without_an_end.py"
        self.sleeper.write_text(NEVER_ENDS, encoding="utf-8")
        # A leak recorded before this test ran belongs to whatever came first, and this suite is
        # only obliged to report leaks it did not already report. Snapshot rather than clear: an
        # entry another module earned is evidence, and wiping it would hide the very thing the
        # guard looks for.
        self.leaks_before = set(LEAKED_GROUPS)

    def wait_for_death(self, pid: int) -> bool:
        """Whether ``pid`` stops existing within :data:`REAP_GRACE`."""
        deadline = time.monotonic() + REAP_GRACE
        while alive(pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        return not alive(pid)

    def test_a_screen_running_under_the_direct_child_dies_with_it(self):
        # The exact shape that leaked: the process the fixture spawned is not the process holding
        # the screen. Killing that child alone left the screen running on a terminal with no master,
        # and a screen with no terminal has been repainting at full speed for hours.
        session = run_in_pty(
            [sys.executable, str(self.sleeper)],
            expect=b"PID=",
            timeout=20,
        )
        found = re.search(rb"PID=(\d+)", session.output)
        self.assertIsNotNone(found, f"no screen was ever started; the run said: {session.screen!r}")
        pid = int(found.group(1))
        # The fixture returns only after its cleanup, so this is the verdict and not a race.
        self.assertTrue(
            self.wait_for_death(pid),
            f"pid {pid} outlived run_in_pty: the fixture killed its own child and left the screen",
        )

    def test_an_ordinary_terminal_run_records_no_leak(self):
        # The common path, as a control: a child that exits by itself still has to leave the group
        # empty and nothing recorded, or the guard would flag every passing test.
        session = run_in_pty(
            ["/bin/bash", "-c", "printf 'READY\\n'; exit 0"],
            expect=b"READY",
            timeout=20,
        )
        self.assertEqual(session.status, 0, session.screen)
        self.assertEqual(LEAKED_GROUPS - self.leaks_before, set())

    def test_the_guard_notices_a_group_that_is_still_there(self):
        # Nothing survives ``SIGKILL``, so the branch that records a leak cannot be reached by
        # leaving a real process behind — and a guard nobody has seen fire is a guard nobody has.
        # This asks the predicate the guard is built on about a group that is definitely still
        # alive, then about the same group once it is gone. Were `_group_gone` to answer "empty"
        # early, :data:`LEAKED_GROUPS` would stay blank and every assertion above it would be
        # decorative.
        sleeper = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True
        )
        self.addCleanup(sleeper.kill)
        pgid = os.getpgid(sleeper.pid)
        try:
            self.assertFalse(_group_gone(pgid, grace=0.2))
        finally:
            sleeper.kill()
            sleeper.wait(timeout=5)
        self.assertTrue(_group_gone(pgid, grace=2.0))
        self.assertEqual(LEAKED_GROUPS - self.leaks_before, set())


class OrphanScanTests(unittest.TestCase):
    """The backstop for a leak the fixture did not make.

    :data:`LEAKED_GROUPS` can only report what :func:`run_in_pty` handed over, so a test that spawns
    a terminal some other way — or production code that spawns one and dies before its cleanup — is
    invisible to it. Both orphans this suite exists for were findable from outside: a live process
    whose command line named a temporary directory that had already been deleted, holding a
    pseudo-terminal whose master was gone.

    The scan is bounded to processes started after this one, because ``/proc`` is the whole machine
    and a checkout being driven by two agents at once is a normal thing. A survivor from before this
    run is somebody else's problem to find; one from during it is on this machine, whatever started
    it, and is worth naming.
    """

    #: The launcher's scratch shape: ``tempfile.mkdtemp`` under the system temp directory, with a
    #: harness script inside it. Deliberately narrow — a process that outlives its tempdir for some
    #: other reason is a different bug and should not be reported as this one. The literal is a
    #: shape, not a path this test touches, so the band saying so is the honest annotation.
    SCRATCH = re.compile(r"/tmp/tmp[A-Za-z0-9_]{8}/\S*\.(?:py|sh)\b")  # noqa: S108

    def boot_ticks(self):
        """Ticks since boot at which this process was created, from ``/proc/self/stat`` field 22."""
        stat = Path("/proc/self/stat").read_text()
        return int(stat.rpartition(") ")[2].split()[19])

    def survivors(self):
        """Pids started during this run whose command line names a tempdir that no longer exists."""
        floor = self.boot_ticks()
        found = []
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                cmdline = entry.joinpath("cmdline").read_bytes().replace(b"\0", b" ").decode(
                    "utf-8", "replace"
                ).strip()
                started = int(
                    entry.joinpath("stat").read_text().rpartition(") ")[2].split()[19]
                )
            except (FileNotFoundError, ProcessLookupError, PermissionError, ValueError, IndexError):
                continue  # it exited, or it belongs to somebody else
            if started < floor or not cmdline:
                continue
            match = self.SCRATCH.search(cmdline)
            if match and not Path(match.group(0)).exists():
                found.append((entry.name, cmdline))
        return found

    def test_nothing_started_during_the_run_outlived_its_scratch_directory(self):
        # Asserted against the present, not a delta: an orphan is still an orphan when a second
        # opinion asks about it, and this costs one directory walk.
        survivors = self.survivors()
        self.assertEqual(
            survivors,
            [],
            "processes still running on scratch that has been deleted — each one is a screen "
            "waiting on a terminal nobody owns",
        )

    def test_the_signature_is_specific_enough_to_be_trusted(self):
        # A guard that cannot see the bug it describes is worse than none: it reads as coverage.
        # These are the two command lines actually found on the host, verbatim.
        self.assertEqual(
            [
                m.group(0)
                for m in map(
                    self.SCRATCH.search,
                    [
                        # Synthetic command lines, one per shape worth matching; nothing here is a
                        # file this test opens.
                        "/usr/bin/python3 /tmp/tmpslk157eu/screen.py",  # noqa: S108
                        "python3 /tmp/tmp3fkn6gk6/harness/tools/workspace_choice.py "  # noqa: S108
                        "--root /tmp/tmp3fkn6gk6/harness",
                        "/bin/bash -c cd '/workspace' && python3 -m unittest tests.test_tui",
                    ],
                )
                if m
            ],
            [
                "/tmp/tmpslk157eu/screen.py",  # noqa: S108
                "/tmp/tmp3fkn6gk6/harness/tools/workspace_choice.py",  # noqa: S108
            ],
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
