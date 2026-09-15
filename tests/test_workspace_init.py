import os
import tempfile
import unittest
from pathlib import Path

from tools.safe_workspace_init import UnsafeWorkspace, initialize


class SafeWorkspaceInitTests(unittest.TestCase):
    def test_creates_expected_layout_and_preserves_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "workspace with spaces"
            initialize(root)
            state = root / ".agent-state" / "STATE.md"
            self.assertIn("# Agent State", state.read_text())
            state.write_text("kept\n")
            initialize(root)
            self.assertEqual(state.read_text(), "kept\n")

    def test_refuses_intermediate_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "workspace"
            outside = Path(directory) / "outside"
            root.mkdir()
            outside.mkdir()
            (root / ".agent-state").symlink_to(outside, target_is_directory=True)
            with self.assertRaises(UnsafeWorkspace):
                initialize(root)

    def test_refuses_fifo_as_state_file(self):
        if not hasattr(os, "mkfifo"):
            self.skipTest("FIFOs are unavailable")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "workspace"
            (root / ".agent-state").mkdir(parents=True)
            os.mkfifo(root / ".agent-state" / "STATE.md")
            with self.assertRaises(UnsafeWorkspace):
                initialize(root)


if __name__ == "__main__":
    unittest.main()
