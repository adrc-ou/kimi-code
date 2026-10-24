import json
import tempfile
import unittest
from pathlib import Path

from tools.approve_extensions import (
    PRIVILEGED,
    approve,
    copy_snapshot,
    inspect_path,
    prepare,
    prepare_mountpoints,
    require_approval,
)


class ApproveExtensionTests(unittest.TestCase):
    @staticmethod
    def sources(snapshot):
        return [snapshot / relative for relative in PRIVILEGED]

    def test_new_preparation_never_replaces_a_previously_published_bind_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace, state, output = root / "workspace", root / "state", root / "approved.yaml"
            workspace.mkdir()
            snapshot = prepare(workspace, root / "approval.json", state, output)
            first = self.sources(snapshot)
            before = [path.stat().st_ino for path in first]
            snapshot = prepare(workspace, root / "approval.json", state, output)
            second = self.sources(snapshot)
            self.assertEqual(len(first), len(PRIVILEGED))
            self.assertTrue(set(first).isdisjoint(second))
            self.assertEqual([path.stat().st_ino for path in first], before)
            self.assertTrue(all(path.exists() for path in second))
            mounts = json.loads(output.read_text())["services"]["kimi-agent"]["volumes"]
            self.assertEqual([mount["volume"]["subpath"] for mount in mounts], list(PRIVILEGED))
            self.assertTrue(all(mount["type"] == "volume" and mount["read_only"]
                                for mount in mounts))
            self.assertNotIn(str(state), output.read_text())
            self.assertEqual((workspace / ".kimi-code/mcp.json").read_bytes(), b"")
            self.assertFalse((root / "approval.json").exists())

    def test_mountpoint_preparation_preserves_project_config_and_existing_approval(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace, manifest = root / "workspace", root / "approval.json"
            config = workspace / ".kimi-code/mcp.json"
            config.parent.mkdir(parents=True)
            original = b'{"mcpServers": {"demo": {"command": "example"}}}\n'
            config.write_bytes(original)
            approve(workspace, manifest)
            before = manifest.read_bytes()
            output = root / "approved.yaml"
            snapshot = prepare(workspace, manifest, root / "state", output)
            self.assertEqual(config.read_bytes(), original)
            self.assertEqual(manifest.read_bytes(), before)
            require_approval(workspace, manifest)
            self.assertEqual(self.sources(snapshot)[-1].read_bytes(), original)

    def test_legacy_empty_directory_approval_remains_valid_but_added_code_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace, manifest = root / "workspace", root / "approval.json"
            (workspace / ".kimi-code/agents").mkdir(parents=True)
            approve(workspace, manifest)
            legacy = json.loads(manifest.read_text())
            legacy["extensions"][".kimi-code/agents"] = {".kimi-code/agents/": "directory"}
            manifest.write_text(json.dumps(legacy))
            before = manifest.read_bytes()
            prepare(workspace, manifest, root / "state", root / "approved.yaml")
            require_approval(workspace, manifest)
            self.assertEqual(manifest.read_bytes(), before)
            (workspace / ".kimi-code/agents/new.md").write_text("new instructions")
            with self.assertRaisesRegex(SystemExit, "extensions changed"):
                require_approval(workspace, manifest)

    def test_mountpoint_creation_cannot_follow_a_project_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace, outside = root / "workspace", root / "outside"
            workspace.mkdir()
            outside.mkdir()
            (workspace / ".kimi-code").symlink_to(outside, target_is_directory=True)
            with self.assertRaisesRegex(RuntimeError, "not a real directory"):
                prepare_mountpoints(workspace)
            self.assertEqual(list(outside.iterdir()), [])

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
                snapshot = prepare(workspace, manifest, root / "state", root / "approved.yaml")
            self.assertFalse(manifest.exists())
            self.assertEqual(self.sources(snapshot)[-1].read_text(), "{}\n")
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
            snapshot = prepare(workspace, manifest, root / "state", output)
            skill_source = self.sources(snapshot)[PRIVILEGED.index(".kimi-code/skills")]
            self.assertEqual((skill_source / "demo/SKILL.md").read_text(), "safe\n")
            require_approval(workspace, manifest)

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
