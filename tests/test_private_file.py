"""The private publish, which is where every staged secret actually lands.

The launcher writes the resolved plan, the credential fragment, the module environment and the
panel's preferences through `write_private`, and the guarantee is stated in three parts: a unique
temporary name per attempt, the mode fixed at create time so a secret is never briefly readable by
the group or world, and a rename as the publish with the directory fsynced after it. Each is a
property a plain `write_text` cannot give, and each is a property a later caller could quietly stop
getting by writing the file itself -- which this session found twice.
"""

from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
for _directory in (ROOT, ROOT / "tests", ROOT / "tools"):
    if str(_directory) not in sys.path:
        sys.path.insert(0, str(_directory))

from private_file import write_private, write_private_json  # noqa: E402


class WritePrivateTests(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.root = Path(self._dir.name)
        self.target = self.root / "launcher.lock"

    def test_the_file_arrives_at_mode_0600(self) -> None:
        write_private(self.target, "secret\n")
        self.assertEqual(stat.S_IMODE(self.target.stat().st_mode), 0o600)

    def test_a_permissive_umask_cannot_widen_the_mode(self) -> None:
        # Two claims, and the end state alone cannot separate them: the file is 0600 when published,
        # and it was 0600 from the moment it was created (so a secret is never briefly readable
        # between create and chmod). The spy is what makes the second one observable.
        previous = os.umask(0o000)
        self.addCleanup(os.umask, previous)
        requested: list[int] = []
        real_open = os.open

        def spy(file, flags, mode=0o777, **kwargs):
            requested.append(mode)
            return real_open(file, flags, mode, **kwargs)

        path = self.root / "creds.env"
        with mock.patch.object(os, "open", spy):
            write_private(path, "TOKEN=x\n")
        self.assertIn(0o600, requested, "the mode is passed to O_CREAT, not applied afterwards")
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_replacing_a_looser_existing_file_narrows_it(self) -> None:
        self.target.write_text("stale\n", encoding="utf-8")
        self.target.chmod(0o644)
        write_private(self.target, "fresh\n")
        self.assertEqual(stat.S_IMODE(self.target.stat().st_mode), 0o600)
        self.assertEqual(self.target.read_text(encoding="utf-8"), "fresh\n")

    def test_no_temporary_is_left_behind(self) -> None:
        write_private(self.target, "x\n")
        self.assertEqual([p.name for p in self.root.iterdir()], ["launcher.lock"])

    def test_two_writes_to_one_path_do_not_collide_or_leave_a_temp(self) -> None:
        # What this shows: a repeated publish is fine and nothing stray survives it. What it cannot
        # show is the anti-collision reason for the random suffix -- that needs an orphan whose name
        # is unpredictable by construction -- so it is not claimed here.
        write_private(self.target, "one\n")
        write_private(self.target, "two\n")
        self.assertEqual(self.target.read_text(encoding="utf-8"), "two\n")
        self.assertEqual([p.name for p in self.root.iterdir()], ["launcher.lock"])

    def test_a_failed_publish_leaves_no_temporary_and_still_raises(self) -> None:
        with mock.patch.object(os, "replace", side_effect=OSError(28, "No space left on device")):
            with self.assertRaises(OSError):
                write_private(self.target, "x\n")
        self.assertFalse(self.target.exists())
        self.assertEqual(list(self.root.iterdir()), [], "the half-written temp is gone")

    def test_a_missing_parent_directory_is_created(self) -> None:
        nested = self.root / "run" / "instance" / "plan.json"
        write_private(nested, "{}\n")
        self.assertTrue(nested.is_file())

    def test_json_is_written_with_the_encoding_and_layout_decided_once(self) -> None:
        path = self.root / "model-policy.json"
        write_private_json(path, {"b": 2, "a": [1, 2]})
        text = path.read_text(encoding="utf-8")
        self.assertTrue(text.endswith("\n"), "a trailing newline, so it is never glued to a prompt")
        self.assertIn('\n  "a": [', text, "indent=2, the form the launcher diffs in logs")
        self.assertEqual(json.loads(text), {"b": 2, "a": [1, 2]})


if __name__ == "__main__":
    unittest.main()
