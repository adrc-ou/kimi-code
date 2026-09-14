import tempfile
import unittest
from pathlib import Path

from tools.approve_extensions import approve, prepare, require_approval


class ApproveExtensionTests(unittest.TestCase):
    def test_approved_extension_is_snapshotted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            skill = workspace / ".kimi-code" / "skills" / "demo"
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text("safe\n")
            manifest = root / "approval.json"
            approve(workspace, manifest)
            output = root / "approved.yaml"
            prepare(workspace, manifest, root / "state", output)
            self.assertIn("read_only: true", output.read_text())
            self.assertEqual(
                (
                    root
                    / "state"
                    / "extension-snapshot"
                    / ".kimi-code"
                    / "skills"
                    / "demo"
                    / "SKILL.md"
                ).read_text(),
                "safe\n",
            )

    def test_changed_extension_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            skill = workspace / ".agents" / "skills" / "demo"
            skill.mkdir(parents=True)
            source = skill / "SKILL.md"
            source.write_text("one\n")
            manifest = root / "approval.json"
            approve(workspace, manifest)
            source.write_text("two\n")
            with self.assertRaises(SystemExit):
                require_approval(workspace, manifest)


if __name__ == "__main__":
    unittest.main()
