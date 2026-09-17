"""The generated half of the workspace ``AGENTS.md`` is replaced, never appended to.

Modules and the resolved model policy each own one marked region of a file the operator also
edits by hand, so the guarantee under test is that a relaunch rewrites only its own region -
and that a region it cannot parse is refused instead of guessed at.
"""

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(ROOT / "tools"))

import managed_section  # noqa: E402
import safe_workspace_init  # noqa: E402
from safe_workspace_init import UnsafeWorkspace  # noqa: E402

BEGIN = "<!-- kimi-harness model policy begin -->"
END = "<!-- kimi-harness model policy end -->"
OTHER_BEGIN = "<!-- kimi-harness modules begin -->"
OTHER_END = "<!-- kimi-harness modules end -->"


class RenderTests(unittest.TestCase):
    def render(self, existing, text="policy text"):
        return managed_section.render(existing, BEGIN, END, text)

    def test_a_section_is_added_to_a_file_that_has_none(self):
        result = self.render("# Operator guidance\n")
        self.assertEqual(
            result,
            f"# Operator guidance\n\n{BEGIN}\npolicy text\n{END}\n",
        )

    def test_a_second_run_replaces_rather_than_duplicates(self):
        once = self.render("# Operator guidance\n", "first envelope")
        twice = self.render(once, "second envelope")
        self.assertEqual(twice.count(BEGIN), 1)
        self.assertEqual(twice.count(END), 1)
        self.assertIn("second envelope", twice)
        self.assertNotIn("first envelope", twice)

    def test_text_outside_the_markers_survives(self):
        # The managed region is always emitted last, and everything the operator wrote is
        # kept: rewriting a region the launcher owns is not licence to drop the rest.
        existing = f"head\n{BEGIN}\nold\n{END}\ntail\n"
        self.assertEqual(
            self.render(existing),
            f"head\n\ntail\n\n{BEGIN}\npolicy text\n{END}\n",
        )

    def test_a_file_with_no_section_yet_is_left_intact_beside_the_new_one(self):
        self.assertEqual(
            self.render("# Operator guidance\n"),
            f"# Operator guidance\n\n{BEGIN}\npolicy text\n{END}\n",
        )

    def test_an_empty_section_removes_the_markers(self):
        existing = f"head\n{BEGIN}\nold\n{END}\ntail\n"
        self.assertEqual(self.render(existing, ""), "head\n\ntail\n")

    def test_a_duplicated_marker_pair_is_refused(self):
        for existing in (
            f"{BEGIN}\na\n{END}\n{BEGIN}\nb\n{END}\n",
            f"{END}\n{BEGIN}\nx\n",
            f"{BEGIN}\nx\n",
            f"{END}\n",
        ):
            with self.subTest(existing=existing.strip()), self.assertRaises(UnsafeWorkspace):
                self.render(existing)

    def test_the_two_producers_sections_coexist(self):
        first = managed_section.render("# Operator\n", BEGIN, END, "policy")
        both = managed_section.render(first, OTHER_BEGIN, OTHER_END, "modules")
        self.assertEqual(both.count(BEGIN), 1)
        self.assertEqual(both.count(OTHER_BEGIN), 1)
        # Rewriting one region leaves the other one's text exactly as it was.
        again = managed_section.render(both, BEGIN, END, "newer policy")
        self.assertIn("modules", again)
        self.assertIn("newer policy", again)
        self.assertNotIn("\npolicy\n", again)


class ReplaceSectionTests(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(self.base, ignore_errors=True))
        self.workspace = self.base / "workspace"
        self.workspace.mkdir()
        self.path = self.workspace / "AGENTS.md"

    def replace(self, text="policy text"):
        managed_section.replace_section(self.workspace, BEGIN, END, text)

    def read(self):
        return self.path.read_text()

    def test_the_file_is_created_when_the_workspace_has_none(self):
        self.replace()
        self.assertIn(BEGIN, self.read())

    def test_operator_text_around_the_region_survives_a_relaunch(self):
        self.path.write_text("# Mine\n\nKeep this.\n")
        self.replace("first envelope")
        self.replace("second envelope")
        result = self.read()
        self.assertEqual(result.count(BEGIN), 1)
        self.assertIn("# Mine", result)
        self.assertIn("Keep this.", result)
        self.assertIn("second envelope", result)
        self.assertNotIn("first envelope", result)

    def test_a_run_that_changes_nothing_rewrites_nothing(self):
        self.replace()
        before = self.path.stat().st_mtime_ns
        self.replace()
        self.assertEqual(self.path.stat().st_mtime_ns, before)

    def test_a_symlinked_agents_file_is_refused(self):
        outside = self.base / "elsewhere.md"
        outside.write_text("not the workspace\n")
        self.path.symlink_to(outside)
        with self.assertRaises(UnsafeWorkspace):
            self.replace()
        self.assertEqual(outside.read_text(), "not the workspace\n")

    def test_a_directory_owned_by_somebody_else_is_refused(self):
        # The launcher is the only writer here, and it refuses to follow a workspace that
        # another uid controls rather than trusting the path string.
        handle = os.open(self.workspace, safe_workspace_init.OPEN_DIR)
        self.addCleanup(os.close, handle)
        with self.assertRaisesRegex(UnsafeWorkspace, "owned by uid"):
            safe_workspace_init.require_directory(
                handle, str(self.workspace), os.getuid() + 1
            )

    def test_an_overgrown_file_is_refused_rather_than_rewritten(self):
        self.path.write_text("x" * (managed_section.MAX_BYTES + 1))
        with self.assertRaisesRegex(UnsafeWorkspace, "exceeds"):
            self.replace()


if __name__ == "__main__":
    unittest.main()
