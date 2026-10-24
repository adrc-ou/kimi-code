"""A failed copy or a new launch must never inherit an older passing verdict."""

import json
import tempfile
import unittest
from pathlib import Path

from tools import check_report
from tools.check_services import write_report


class ServiceReportTests(unittest.TestCase):
    def test_only_current_run_and_pass_can_replace_private_pending_report(self):
        with tempfile.TemporaryDirectory() as directory:
            target, source = (Path(directory) / name for name in ("host.json", "container.json"))
            check_report.pending(target, "current", "full")
            before = target.read_bytes()
            for run_id, mode, state in (("old", "full", "complete"),
                                        ("current", "quick", "complete"),
                                        ("current", "full", "pending")):
                write_report(source, mode, [], 0, run_id, state)
                self.assertFalse(check_report.collect(source, target, "current", "full"))
                self.assertEqual(target.read_bytes(), before)
            write_report(source, "full", [], 0, "current")
            self.assertTrue(check_report.collect(source, target, "current", "full"))
            self.assertEqual(json.loads(target.read_text())["state"], "complete")
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)

    def test_new_pending_report_invalidates_old_success_and_bad_copies_stay_pending(self):
        with tempfile.TemporaryDirectory() as directory:
            target, source = (Path(directory) / name for name in ("host.json", "container.json"))
            write_report(target, "full", [], 0, "old")
            check_report.pending(target, "new", "quick")
            self.assertIsNone(json.loads(target.read_text())["exit_code"])
            for content in ("not json", "[]", "{}"):
                source.write_text(content)
                self.assertFalse(check_report.collect(source, target, "new", "quick"))
            self.assertEqual(json.loads(target.read_text())["state"], "pending")
