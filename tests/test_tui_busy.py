"""The spinner handshake between the launcher and the foreground painter.

Three properties here are invisible when they hold and bad when they break. The indicator may only
be published by whoever holds ``screen-claim``, because a spinner drawn onto another screen's cells
corrupts that screen; the render lock must be given up rather than waited on, because feedback must
never stall a picker; and none of it may write when the runtime directory is unset, since an
unattended launch has no terminal to notify.
"""

from __future__ import annotations

import fcntl
import json
import os
import stat
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
for _directory in (ROOT, ROOT / "tools"):
    if str(_directory) not in sys.path:
        sys.path.insert(0, str(_directory))

from tools.tui import busy, screen  # noqa: E402


class BusyTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        patcher = mock.patch.dict(os.environ, {"HARNESS_RUNTIME_DIR": str(self.root)})
        patcher.start()
        self.addCleanup(patcher.stop)
        (self.root / busy.CLAIM).write_text("token-1", encoding="ascii")

    def state(self) -> dict:
        return json.loads((self.root / busy.STATE).read_text(encoding="utf-8"))

    def test_the_indicator_is_published_only_to_a_screen_this_process_claims(self) -> None:
        busy.activate(4, 9, unicode=True, styled=False)
        self.assertEqual(
            self.state(),
            {
                "active": True,
                "claim": "token-1",
                "row": 4,
                "column": 9,
                "unicode": True,
                "styled": False,
            },
        )

    def test_a_process_that_does_not_hold_the_claim_writes_nothing(self) -> None:
        # No claim file, so no state at all: not an empty one, not a stale one.
        (self.root / busy.CLAIM).unlink()
        busy.activate(1, 1, unicode=False, styled=False)
        self.assertFalse((self.root / busy.STATE).exists())

    def test_hiding_the_indicator_needs_no_claim(self) -> None:
        # The asymmetry is deliberate, and pinned so nobody "corrects" it into a bug: activate
        # draws onto cells another screen may own, so it demands the claim, while cancel only ever
        # hides. Refusing the latter would leave an indicator spinning with nothing to dismiss it.
        (self.root / busy.CLAIM).unlink()
        busy.cancel()
        self.assertEqual(self.state(), {"active": False})

    def test_cancel_marks_the_indicator_inactive_under_the_same_name(self) -> None:
        busy.activate(2, 2, unicode=False, styled=True)
        busy.cancel()
        self.assertEqual(self.state(), {"active": False})

    def test_no_runtime_directory_means_no_file_anywhere(self) -> None:
        with mock.patch.dict(os.environ, {"HARNESS_RUNTIME_DIR": ""}):
            self.assertEqual(busy.runtime(), "")
            busy.cancel()
            busy.activate(0, 0, unicode=False, styled=False)
        self.assertEqual([entry.name for entry in self.root.iterdir()], [busy.CLAIM])

    def test_the_render_lock_is_given_up_rather_than_waited_on(self) -> None:
        # Someone else holds it. The contract is a quick "no": a spinner that blocks the picker is
        # worse than a spinner that skips a frame, so this must not spin until it wins.
        descriptor = os.open(self.root / busy.LOCK, os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        self.addCleanup(os.close, descriptor)
        started = time.monotonic()
        with busy.rendering(str(self.root)) as locked:
            self.assertFalse(locked, "a held lock must not be waited out")
        self.assertLess(time.monotonic() - started, 2.0)

    def test_the_render_lock_is_reported_held_by_nobody_when_it_is_free(self) -> None:
        with busy.rendering(str(self.root)) as locked:
            self.assertTrue(locked)

    def test_a_claim_is_read_as_ascii_and_an_empty_directory_claims_nothing(self) -> None:
        self.assertEqual(busy.claim(str(self.root)), "token-1")
        self.assertEqual(busy.claim(""), "", "no directory means no screen to take over")
        (self.root / busy.CLAIM).write_bytes(b"\xff\xfe not ascii")
        self.assertEqual(busy.claim(str(self.root)), "", "a corrupt claim is no claim")


    def test_the_screen_claim_lands_at_0600_like_every_other_runtime_file(self) -> None:
        # The writer is screen.py, the reader is busy.py, so both are checked from here.
        # A lax umask left the file 0666 (world-writable) while every sibling in the instance
        # directory is private, so any local process could spoof who owns the borrowed screen.
        previous = os.umask(0o000)
        self.addCleanup(os.umask, previous)
        (self.root / busy.CLAIM).unlink()
        screen._claim(str(self.root), True)
        mode = stat.S_IMODE(Path(screen.claim_path(str(self.root))).stat().st_mode)
        self.assertEqual(mode, 0o600, "the claim is private, like the state file beside it")
        self.assertTrue(busy.claim(str(self.root)), "the reader accepts what the writer wrote")
        screen._claim(str(self.root), False)
        self.assertFalse((self.root / busy.CLAIM).exists())


if __name__ == "__main__":
    unittest.main()
