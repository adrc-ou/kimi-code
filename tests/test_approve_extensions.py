import tempfile
import unittest
from pathlib import Path

from tools.approve_extensions import PRIVILEGED, approve, prepare, require_approval


class ApproveExtensionTests(unittest.TestCase):
    def test_empty_docker_mountpoints_do_not_require_approval(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            for relative in PRIVILEGED:
                path = workspace / relative
                if relative.endswith(".json"):
                    path.touch()
                else:
                    path.mkdir(parents=True)
            manifest = root / "approval.json"
            for _ in range(2):
                prepare(workspace, manifest, root / "state", root / "approved.yaml")
            self.assertFalse(manifest.exists())
            self.assertEqual(
                (root / "state/extension-snapshot/.kimi-code/mcp.json").read_text(), "{}\n"
            )
            skill = workspace / ".agents/skills/SKILL.md"
            skill.write_text("New executable guidance\n")
            with self.assertRaisesRegex(SystemExit, r"\./extensions.sh list"):
                require_approval(workspace, manifest)

    def test_nonempty_mcp_file_requires_approval(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "workspace/.kimi-code/mcp.json"
            config.parent.mkdir(parents=True)
            config.write_text('{"mcpServers": {"demo": {"command": "example"}}}\n')
            with self.assertRaisesRegex(SystemExit, "require approval"):
                require_approval(root / "workspace", root / "approval.json")

    def test_empty_linked_mcp_file_is_still_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "empty"
            source.touch()
            config = root / "workspace/.kimi-code/mcp.json"
            config.parent.mkdir(parents=True)
            config.symlink_to(source)
            with self.assertRaisesRegex(ValueError, "linked extension"):
                require_approval(root / "workspace", root / "approval.json")

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
