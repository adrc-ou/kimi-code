import tempfile
import unittest
from pathlib import Path

from tools.approve_extensions import (
    PRIVILEGED,
    approve,
    copy_snapshot,
    inspect_path,
    prepare,
    require_approval,
)


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


    def test_symlinked_extension_ancestor_is_refused(self):
        # POSIX resolves every intermediate component, so a link at .kimi-code or .agents
        # used to make host content look like project content to both the scan and the copy.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            outside = root / "host"
            skill = outside / "skills" / "demo"
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text("host content\n")
            workspace = root / "workspace"
            workspace.mkdir()
            (workspace / ".kimi-code").symlink_to(outside, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "ancestor is not a real directory"):
                inspect_path(workspace, ".kimi-code/skills")
            with self.assertRaisesRegex(ValueError, "ancestor is not a real directory"):
                require_approval(workspace, root / "approval.json")
            with self.assertRaisesRegex(ValueError, "ancestor is not a real directory"):
                approve(workspace, root / "approval.json")

    def test_snapshot_copy_refuses_a_link_planted_after_the_scan(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            outside = root / "host" / "agents"
            outside.mkdir(parents=True)
            (outside / "AGENT.md").write_text("host content\n")
            workspace = root / "workspace"
            workspace.mkdir()
            (workspace / ".kimi-code").symlink_to(root / "host", target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "ancestor is not a real directory"):
                copy_snapshot(workspace, root / "state", ".kimi-code/agents")
            self.assertFalse((root / "state" / ".kimi-code" / "agents" / "AGENT.md").exists())

    def test_real_ancestors_still_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            skill = workspace / ".agents" / "skills" / "demo"
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text("safe\n")
            self.assertEqual(
                set(inspect_path(workspace, ".agents/skills")),
                {".agents/skills/", ".agents/skills/demo/", ".agents/skills/demo/SKILL.md"},
            )
            # The name is about the copy rather than the scan: a real ``.agents`` ancestor has to
            # be walkable by copy_snapshot too, which is the positive half of the refusal the
            # next test checks for a link planted after the scan.
            copy_snapshot(workspace, root / "state", ".agents/skills")
            self.assertEqual(
                (root / "state" / ".agents" / "skills" / "demo" / "SKILL.md").read_text(),
                "safe\n",
            )


if __name__ == "__main__":
    unittest.main()
