import json
import os
import tempfile
import unittest
from pathlib import Path

from modules.comfyui.scripts.verify_bind_paths import KINDS, snapshot


class BindPathTests(unittest.TestCase):
    def make_sources(self, workspace: Path) -> Path:
        """Every published kind, as ``sources()`` names it, each under workspace/comfyui."""
        for kind in KINDS:
            (workspace / "comfyui" / kind).mkdir(parents=True, exist_ok=True)
        return workspace

    def test_refuses_nested_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            workspace = root / "workspace"
            workspace.mkdir()
            target = root / "target"
            target.mkdir()
            (workspace / "comfyui").symlink_to(target, target_is_directory=True)
            with self.assertRaises(ValueError):
                snapshot(workspace)

    def test_records_real_directories(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = self.make_sources(Path(directory).resolve() / "workspace")
            result = snapshot(workspace)
            self.assertEqual(set(result), set(KINDS))
            # The snapshot is written to a JSON manifest and re-read from it by the verify pass,
            # which compares the decoded structure rather than the bytes, so a value that will
            # not survive a JSON round trip is a failed launch rather than a false match.
            self.assertEqual(json.loads(json.dumps(result)), result)


    def test_refuses_parent_reference(self):
        # A parent reference names a real directory at every step, so only an explicit
        # refusal keeps the bind source inside the tree the operator configured.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            workspace = self.make_sources(root / "workspace")
            escaped = workspace / "comfyui" / ".." / ".." / "outside"
            (root / "outside").mkdir()
            self.addCleanup(os.environ.pop, "COMFYUI_MODELS_PATH", None)
            os.environ["COMFYUI_MODELS_PATH"] = str(escaped)
            with self.assertRaisesRegex(ValueError, "parent reference"):
                snapshot(workspace)

    def test_records_the_verified_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = self.make_sources(Path(directory).resolve() / "workspace")
            result = snapshot(workspace)
            models = workspace / "comfyui" / "models"
            self.assertEqual(result["models"]["path"], str(models))
            self.assertEqual(result["models"]["inode"], models.stat().st_ino)
            self.assertEqual(result["models"]["device"], models.stat().st_dev)
            self.assertEqual(result["models"]["uid"], os.getuid())


if __name__ == "__main__":
    unittest.main()
