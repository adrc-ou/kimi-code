import json
import os
import tempfile
import unittest
from pathlib import Path

from modules.comfyui.scripts.verify_bind_paths import snapshot


class BindPathTests(unittest.TestCase):
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
            workspace = Path(directory).resolve() / "workspace"
            for kind in ("models", "custom_nodes", "input", "output", "temp", "user"):
                (workspace / "comfyui" / kind).mkdir(parents=True, exist_ok=True)
            result = snapshot(workspace)
            self.assertEqual(
                set(result), {"models", "custom_nodes", "input", "output", "temp", "user"}
            )
            json.dumps(result)


    def test_refuses_parent_reference(self):
        # A parent reference names a real directory at every step, so only an explicit
        # refusal keeps the bind source inside the tree the operator configured.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            workspace = root / "workspace"
            for kind in ("models", "custom_nodes", "input", "output", "temp", "user"):
                (workspace / "comfyui" / kind).mkdir(parents=True)
            escaped = workspace / "comfyui" / ".." / ".." / "outside"
            (root / "outside").mkdir()
            self.addCleanup(os.environ.pop, "COMFYUI_MODELS_PATH", None)
            os.environ["COMFYUI_MODELS_PATH"] = str(escaped)
            with self.assertRaisesRegex(ValueError, "parent reference"):
                snapshot(workspace)

    def test_records_the_verified_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory).resolve() / "workspace"
            for kind in ("models", "custom_nodes", "input", "output", "temp", "user"):
                (workspace / "comfyui" / kind).mkdir(parents=True)
            result = snapshot(workspace)
            models = workspace / "comfyui" / "models"
            self.assertEqual(result["models"]["path"], str(models))
            self.assertEqual(result["models"]["inode"], models.stat().st_ino)
            self.assertEqual(result["models"]["device"], models.stat().st_dev)
            self.assertEqual(result["models"]["uid"], os.getuid())


if __name__ == "__main__":
    unittest.main()
