"""Approved archives are copied without tar links, stale content, or agent write access."""

import io
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools import extension_volume as volume


def archive(extra=(), *, complete=True):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as output:
        for name in volume.ROOTS if complete else ():
            member = tarfile.TarInfo(name)
            if name.endswith(".json"):
                member.size = 3
                output.addfile(member, io.BytesIO(b"{}\n"))
            else:
                member.type = tarfile.DIRTYPE
                output.addfile(member)
        for name, kind in extra:
            member = tarfile.TarInfo(name)
            member.type = kind
            member.linkname = "/outside"
            data = b"approved content" if kind == tarfile.REGTYPE else b""
            member.size = len(data)
            output.addfile(member, io.BytesIO(data))
    stream.seek(0)
    return stream


class ExtensionVolumeTests(unittest.TestCase):
    def setUp(self):
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.root = Path(holder.name)
        self.addCleanup(lambda: volume.clear(self.root))
        chown = patch.object(volume.os, "chown")
        self.chown = chown.start()
        self.addCleanup(chown.stop)

    def test_replacement_clears_stale_content_and_sets_read_only_group_permissions(self):
        (self.root / "stale").write_text("old content")
        name = ".kimi-code/skills/demo/SKILL.md"
        volume.receive(self.root, archive([(name, tarfile.REGTYPE)]), 1234)
        self.assertFalse((self.root / "stale").exists())
        self.assertEqual((self.root / name).read_text(), "approved content")
        self.assertEqual((self.root / name).stat().st_mode & 0o777, 0o440)
        self.assertEqual((self.root / name).parent.stat().st_mode & 0o777, 0o550)
        self.assertTrue(all(call.args[1:] == (0, 1234) for call in self.chown.call_args_list))
        volume.receive(self.root, archive(), 1234)
        self.assertFalse((self.root / name).exists())
        self.assertEqual((self.root / volume.ROOTS[-1]).read_text(), "{}\n")
        volume.clear(self.root)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_links_special_objects_and_paths_outside_approved_roots_fail_closed(self):
        for name, kind in (
            ("../outside", tarfile.REGTYPE), ("/outside", tarfile.REGTYPE),
            (".kimi-code/skills/../../outside", tarfile.REGTYPE),
            (".kimi-code/skills/link", tarfile.SYMTYPE),
            (".kimi-code/skills/link", tarfile.LNKTYPE),
            (".kimi-code/skills/fifo", tarfile.FIFOTYPE),
        ):
            with self.subTest(name=name, kind=kind), self.assertRaises(ValueError):
                volume.receive(self.root, archive([(name, kind)]), 1234)

    def test_incomplete_archive_is_not_a_successful_stage(self):
        with self.assertRaisesRegex(ValueError, "incomplete"):
            volume.receive(self.root, archive(complete=False), 1234)
