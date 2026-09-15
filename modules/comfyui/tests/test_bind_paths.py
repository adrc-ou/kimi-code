import json
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


if __name__ == "__main__":
    unittest.main()
