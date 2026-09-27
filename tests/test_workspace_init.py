import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from tools.safe_workspace_init import UnsafeWorkspace, initialize, retire_state


def git_available() -> bool:
    return shutil.which("git") is not None


def inside_a_repository(path: Path) -> bool:
    result = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "--is-inside-work-tree"],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0 and result.stdout.strip() == "true"


class SafeWorkspaceInitTests(unittest.TestCase):
    @unittest.skipUnless(git_available(), "the assertion asks Git what the project tracks")
    def test_creates_nothing_of_its_own_and_ignores_only_harness_droppings(self):
        # The harness keeps no state in the project any more. What it does ask of Git is the
        # exclude list for the paths tools drop at the root - and the retired scratch directory
        # is deliberately not on it, because the harness no longer writes one there.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "workspace with spaces"
            root.mkdir()
            subprocess.run(["git", "-C", str(root), "init", "-q"], check=True, capture_output=True)
            initialize(root)
            self.assertEqual([path.name for path in root.iterdir()], [".git"])
            exclude = (root / ".git/info/exclude").read_text()
            self.assertIn(".serena/", exclude)
            self.assertIn(".playwright-cli/", exclude)
            self.assertNotIn(".agent-state", exclude)

    def test_creates_module_directories_and_preserves_them(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "workspace"
            initialize(root, ("demo/user",))
            (root / "demo/user/data").write_text("keep\n")
            initialize(root, ("demo/user",))
            self.assertEqual((root / "demo/user/data").read_text(), "keep\n")

    def test_refuses_intermediate_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "workspace"
            outside = Path(directory) / "outside"
            root.mkdir()
            outside.mkdir()
            (root / "demo").symlink_to(outside, target_is_directory=True)
            with self.assertRaises(UnsafeWorkspace):
                initialize(root, ("demo/user",))

    def test_refuses_fifo_as_module_directory(self):
        if not hasattr(os, "mkfifo"):
            self.skipTest("FIFOs are unavailable")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "workspace"
            root.mkdir()
            os.mkfifo(root / "demo")
            with self.assertRaises(UnsafeWorkspace):
                initialize(root, ("demo/user",))


@unittest.skipUnless(git_available(), "retirement asks Git who owns the directory")
class RetiredStateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "workspace"
        self.root.mkdir()
        subprocess.run(
            ["git", "-C", str(self.root), "init", "-q"], check=True, capture_output=True
        )
        subprocess.run(
            ["git", "-C", str(self.root), "config", "user.email", "fixture@example.invalid"],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(self.root), "config", "user.name", "Fixture"],
            check=True,
            capture_output=True,
        )

    def seed(self) -> Path:
        state = self.root / ".agent-state"
        (state / "logs").mkdir(parents=True)
        (state / "STATE.md").write_text("# Agent State\n")
        (state / "logs" / "run.log").write_text("evidence\n")
        return state

    def test_removes_the_scratch_directory_the_harness_left_behind(self):
        state = self.seed()
        self.assertTrue(retire_state(self.root))
        self.assertFalse(state.exists())
        # Retiring twice is not an error, and a workspace with nothing to retire says so.
        self.assertFalse(retire_state(self.root))

    def test_leaves_a_directory_the_project_tracks(self):
        state = self.seed()
        subprocess.run(
            ["git", "-C", str(self.root), "add", ".agent-state/STATE.md"],
            check=True,
            capture_output=True,
        )
        self.assertFalse(retire_state(self.root))
        self.assertTrue((state / "STATE.md").exists())

    def test_leaves_a_symlink_pointing_outside(self):
        outside = self.root.parent / "outside"
        outside.mkdir()
        (outside / "keep.md").write_text("not ours\n")
        (self.root / ".agent-state").symlink_to(outside, target_is_directory=True)
        self.assertFalse(retire_state(self.root))
        self.assertTrue((outside / "keep.md").exists())

    def test_initialize_retires_without_recreating(self):
        state = self.seed()
        initialize(self.root)
        self.assertFalse(state.exists())

    def test_a_non_repository_keeps_what_it_has(self):
        outside = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, outside, True)
        if inside_a_repository(outside):
            self.skipTest("the temporary directory is not outside every repository")
        state = outside / ".agent-state"
        state.mkdir()
        (state / "STATE.md").write_text("unprovable\n")
        self.assertFalse(retire_state(outside))
        self.assertTrue((state / "STATE.md").exists())


if __name__ == "__main__":
    unittest.main()
