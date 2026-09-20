import json
import os
import shutil
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.helpers import load_script  # noqa: E402

GID = os.getgid()

init = load_script("initialize_agent_state", Path("container/initialize-agent-state.py"))
merge = load_script("kimi_config_merge", Path("tools/kimi_config_merge.py"))

BASELINE = """default_model = "qwen3-primary"
default_permission_mode = "manual"

[thinking]
enabled = true

[secondary_model]
force = true
"""


class Fixture:
    """A stand-in for the stage directory and the volumes the initializer mounts."""

    def __init__(self, directory: Path) -> None:
        self.root = directory
        self.stage = directory / "stage"
        self.assets_source = self.stage / "assets"
        self.home = directory / "state" / "kimi"
        self.serena = directory / "state" / "serena"
        self.assets = directory / "state" / "assets"
        self.managed = directory / "state" / "managed"
        self.stage.mkdir(parents=True)
        for path in (self.home / "sessions", self.serena, self.assets, self.managed):
            path.mkdir(parents=True)
        for name in init.SUPPRESSED_DIRECTORIES:
            (self.managed / name).mkdir(exist_ok=True)
        (self.stage / "AGENTS.md").write_text("contract\n")
        (self.stage / "SYSTEM.md").write_text("")
        (self.stage / "kimi-config.toml").write_text(BASELINE)
        shutil.copy(ROOT / "runtime" / "config-policy.json", self.stage / "config-policy.json")
        shutil.copy(ROOT / "runtime" / "serena-config.yml", self.stage / "serena-config.yml")
        for category in ("skills", "agents", "tools"):
            (self.assets_source / category).mkdir(parents=True)
        (self.assets_source / "skills" / "playwright-cli").mkdir()
        (self.assets_source / "skills" / "playwright-cli" / "SKILL.md").write_text("skill\n")
        tool = self.assets_source / "tools" / "check_services.py"
        tool.write_text("print(1)\n")
        tool.chmod(0o755)
        (self.assets_source / "mcp.json").write_text('{"mcpServers": {}}')


class StagingTests(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.fixture = Fixture(Path(self._temporary.name))
        # Ownership moves require root and immutable flags require CAP_LINUX_IMMUTABLE; the mode
        # logic is what these tests exercise, and real flag behaviour is covered by
        # tests/compose-config.sh --runtime and test_flag_support_is_verified_rather_than_assumed.
        chown = mock.patch.object(init.os, "chown")
        chown.start()
        self.addCleanup(chown.stop)
        flagger = mock.patch.object(init, "require_immutable")
        self.require_immutable = flagger.start()
        self.addCleanup(flagger.stop)

    def test_managed_files_are_published_read_only_for_the_agent_group(self):
        init.stage_managed_files(self.fixture.stage, self.fixture.home, GID)
        for name in init.MANAGED_FILES:
            mode = (self.fixture.home / name).stat().st_mode & 0o777
            self.assertEqual(mode, 0o440, name)
        self.assertEqual(self.require_immutable.call_count, len(init.MANAGED_FILES))
        self.assertEqual((self.fixture.home / "AGENTS.md").read_text(), "contract\n")
        self.assertEqual((self.fixture.home / "mcp.json").read_text(), '{"mcpServers": {}}')

    def test_managed_files_are_replaced_on_the_next_launch(self):
        init.stage_managed_files(self.fixture.stage, self.fixture.home, GID)
        (self.fixture.stage / "AGENTS.md").write_text("revised contract\n")
        init.stage_managed_files(self.fixture.stage, self.fixture.home, GID)
        self.assertEqual((self.fixture.home / "AGENTS.md").read_text(), "revised contract\n")

    def test_missing_stage_source_fails_closed(self):
        (self.fixture.stage / "AGENTS.md").unlink()
        with self.assertRaises(init.StagingError):
            init.stage_managed_files(self.fixture.stage, self.fixture.home, GID)

    def stage_serena(self):
        return init.stage_serena_config(
            self.fixture.stage, self.fixture.serena, os.getuid(), GID
        )

    def test_serena_config_is_staged_as_agent_owned_writable_content(self):
        target = self.stage_serena()
        info = target.stat()
        self.assertEqual(target.name, init.SERENA_CONFIG_TARGET)
        self.assertEqual(info.st_uid, os.getuid())
        self.assertEqual(info.st_mode & 0o777, 0o600)
        self.assertEqual(
            target.read_text(),
            (self.fixture.stage / init.SERENA_CONFIG_SOURCE).read_text(),
        )
        # Serena re-saves this file itself when it registers the project it was pointed at, so
        # the flag that protects the managed kimi-home files would make first use fatal.
        self.require_immutable.assert_not_called()

    def test_serena_config_edited_by_a_session_is_re_pinned_at_the_next_launch(self):
        target = self.stage_serena()
        target.write_text("language_backend: JetBrains\nprojects: []\n")
        self.stage_serena()
        self.assertIn("language_backend: LSP", target.read_text())

    def test_missing_serena_config_source_fails_closed(self):
        (self.fixture.stage / init.SERENA_CONFIG_SOURCE).unlink()
        with self.assertRaises(init.StagingError):
            self.stage_serena()

    def test_assets_tree_is_readable_and_only_executables_stay_executable(self):
        count = init.stage_assets_tree(self.fixture.stage, self.fixture.assets, GID)
        self.assertEqual(count, 3)
        skill = self.fixture.assets / "skills" / "playwright-cli" / "SKILL.md"
        tool = self.fixture.assets / "tools" / "check_services.py"
        self.assertEqual(skill.stat().st_mode & 0o777, 0o440)
        self.assertEqual(tool.stat().st_mode & 0o777, 0o550)
        self.assertEqual(skill.parent.stat().st_mode & 0o777, 0o550)

    @unittest.skipUnless(hasattr(os, "geteuid") and os.geteuid() == 0, "requires root")
    def test_assets_tree_drops_content_removed_from_the_stage(self):
        init.stage_assets_tree(self.fixture.stage, self.fixture.assets, GID)
        stale = self.fixture.assets / "skills" / "deselect-me"
        stale.mkdir()
        (stale / "SKILL.md").write_text("gone\n")
        shutil.rmtree(self.fixture.assets_source / "skills" / "playwright-cli")
        init.stage_assets_tree(self.fixture.stage, self.fixture.assets, GID)
        self.assertFalse(stale.exists())
        self.assertFalse((self.fixture.assets / "skills" / "playwright-cli").exists())

    def test_symlinked_asset_refuses_staging(self):
        os.symlink("/etc/passwd", self.fixture.assets_source / "tools" / "sneaky.py")
        with self.assertRaises(init.StagingError):
            init.stage_assets_tree(self.fixture.stage, self.fixture.assets, GID)

    def test_suppression_directories_are_kept_empty(self):
        planted = self.fixture.managed / "skills" / "unapproved"
        planted.mkdir()
        (planted / "SKILL.md").write_text("payload\n")
        init.stage_suppressions(self.fixture.managed, GID)
        self.assertEqual(list((self.fixture.managed / "skills").iterdir()), [])
        self.assertEqual((self.fixture.managed / "skills").stat().st_mode & 0o777, 0o550)

    def test_missing_suppression_volume_fails_closed(self):
        shutil.rmtree(self.fixture.managed / "plugins")
        with self.assertRaises(init.StagingError):
            init.stage_suppressions(self.fixture.managed, GID)


class ConfigMergeIntegrationTests(unittest.TestCase):
    MODULE = ROOT / "tools" / "kimi_config_merge.py"

    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.fixture = Fixture(Path(self._temporary.name))
        self.uid, self.gid = os.getuid(), os.getgid()

    def merged(self):
        return tomllib.loads((self.fixture.home / "config.toml").read_text())

    def test_first_launch_writes_the_baseline_for_the_agent(self):
        outcome = init.merge_config(
            self.fixture.stage, self.fixture.home, self.MODULE, self.uid, self.gid
        )
        self.assertEqual(outcome, "baseline")
        info = (self.fixture.home / "config.toml").stat()
        self.assertEqual(info.st_mode & 0o777, 0o600)
        self.assertEqual((info.st_uid, info.st_gid), (self.uid, self.gid))
        # First launch is a copy, so the whole document is the contract. Comparing one key to a
        # string from this file's own baseline proves nothing; equality also catches a key that
        # is quietly dropped on the way in.
        self.assertEqual(self.merged(), tomllib.loads(BASELINE))

    def test_settings_written_by_the_ui_survive_but_model_choice_does_not(self):
        init.merge_config(self.fixture.stage, self.fixture.home, self.MODULE, self.uid, self.gid)
        stored = merge.parse_config((self.fixture.home / "config.toml").read_text())
        stored["default_model"] = "qwen3-long"
        stored["thinking"]["enabled"] = False
        stored["default_permission_mode"] = "yolo"
        (self.fixture.home / "config.toml").write_text(merge.emit(stored))
        outcome = init.merge_config(
            self.fixture.stage, self.fixture.home, self.MODULE, self.uid, self.gid
        )
        self.assertEqual(outcome, "merged")
        self.assertFalse(self.merged()["thinking"]["enabled"])
        # The model is selected by ./start.sh from ./models, and the proxy enforces the plan
        # that selection resolved, so an in-session change is re-pinned at the next launch.
        self.assertEqual(self.merged()["default_model"], "qwen3-primary")
        self.assertEqual(self.merged()["default_permission_mode"], "manual")

    def test_unreadable_stored_config_is_quarantined_and_rebuilt(self):
        (self.fixture.home / "config.toml").write_text("[[unterminated\n")
        outcome = init.merge_config(
            self.fixture.stage, self.fixture.home, self.MODULE, self.uid, self.gid
        )
        self.assertEqual(outcome, "baseline")
        quarantined = list(self.fixture.home.glob("config.toml.unreadable-*"))
        self.assertEqual(len(quarantined), 1)
        # The rebuild has to be a usable document, not merely a present file.
        self.assertEqual(self.merged(), tomllib.loads(BASELINE))

    def test_unusable_baseline_fails_closed(self):
        (self.fixture.stage / "kimi-config.toml").write_text("= broken\n")
        with self.assertRaises(init.StagingError):
            init.merge_config(
                self.fixture.stage, self.fixture.home, self.MODULE, self.uid, self.gid
            )


class OwnershipTests(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)

    @unittest.skipUnless(hasattr(os, "geteuid") and os.geteuid() == 0, "requires root")
    def test_repair_reowns_state_but_never_follows_a_planted_symlink(self):
        target = self.root / "sessions"
        target.mkdir()
        (target / "wire.jsonl").write_text("x\n")
        link = self.root / "escape"
        link.symlink_to("/etc/passwd")
        fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            init.repair_tree(fd, 1234, 2345)
        finally:
            os.close(fd)
        self.assertEqual((target / "wire.jsonl").stat().st_uid, 1234)
        self.assertTrue(link.is_symlink())
        self.assertEqual(os.stat(link, follow_symlinks=False).st_uid, 1234)

    def test_flag_support_is_verified_rather_than_assumed(self):
        # Without CAP_LINUX_IMMUTABLE the kernel refuses the flag, and staging must not continue
        # believing the file is protected.
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            self.skipTest("root may set the flag; covered by tests/compose-config.sh --runtime")
        path = self.root / "AGENTS.md"
        path.write_text("contract\n")
        with self.assertRaises(init.StagingError):
            init.require_immutable(path)
        init.clear_immutable(path)  # best effort, never raises

    def test_write_private_sets_mode_and_replaces_existing_content(self):
        path = self.root / "config.toml"
        path.write_text("old = true\n")
        init.write_private(path, b"new = true\n", uid=os.getuid(), gid=os.getgid(), mode=0o600)
        self.assertEqual(path.read_text(), "new = true\n")
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertFalse(list(self.root.glob("*.staging")))

    def test_write_private_refuses_to_follow_a_symlink(self):
        outside = self.root / "outside"
        outside.write_text("victim\n")
        link = self.root / "mcp.json"
        link.symlink_to(outside)
        init.write_private(
            link, b'{"mcpServers": {}}\n', uid=os.getuid(), gid=os.getgid(), mode=0o440
        )
        self.assertTrue(link.is_file() and not link.is_symlink())
        self.assertEqual(outside.read_text(), "victim\n")

    def test_managed_targets_cover_the_launcher_owned_home_files(self):
        self.assertEqual(set(init.MANAGED_FILES), {"AGENTS.md", "SYSTEM.md", "mcp.json"})
        self.assertEqual(init.SUPPRESSED_DIRECTORIES, ("agents", "skills", "plugins"))


class StaticPolicyTests(unittest.TestCase):
    def setUp(self):
        self.compose = (ROOT / "compose.yaml").read_text()

    def test_launcher_no_longer_overlays_files_inside_the_agent_home(self):
        for target in ("config.toml", "mcp.json", "AGENTS.md", "SYSTEM.md",
                       "agents", "skills", "plugins"):
            self.assertNotIn(f"/home/agent/.kimi-code/{target}:ro", self.compose)

    def test_staged_content_reaches_the_agent_through_named_volumes(self):
        for volume in ("kimi-assets", "kimi_user_agents", "kimi_user_skills", "kimi_user_plugins"):
            self.assertIn(f"source: {volume}", self.compose)

    def test_retired_environment_variables_are_gone(self):
        for retired in ("KIMI_EMPTY_SYSTEM", "KIMI_EMPTY_USER_AGENTS",
                        "KIMI_EMPTY_USER_SKILLS", "KIMI_EMPTY_USER_PLUGINS"):
            self.assertNotIn(retired, self.compose)
        self.assertIn("KIMI_SYSTEM_MD", self.compose)


class SerenaConfigTemplateTests(unittest.TestCase):
    """What the shipped Serena configuration has to satisfy to start at all."""

    @classmethod
    def setUpClass(cls):
        cls.document = (ROOT / "runtime" / init.SERENA_CONFIG_SOURCE).read_text()

    def test_the_one_key_serena_refuses_to_default_is_present(self):
        # Serena aborts with "`projects` key not found in Serena configuration" rather than
        # treating a missing project list as empty.
        self.assertIn("\nprojects: []\n", self.document)

    def test_the_python_language_server_is_the_binary_the_image_builds(self):
        self.assertIn("ls_path: /usr/local/bin/pyright-langserver", self.document)
        dockerfile = (ROOT / "container" / "Dockerfile").read_text()
        self.assertIn(
            "ln -s /opt/node-tools/node_modules/.bin/pyright-langserver"
            " /usr/local/bin/pyright-langserver",
            dockerfile,
        )

    def test_the_workspace_is_not_a_trusted_project_path(self):
        # Serena's own default is ["**"], which would let a session name an arbitrary language
        # server executable through the agent-writable .serena/project.local.yml.
        self.assertIn("trusted_project_path_patterns: []", self.document)

    def test_pyright_is_pinned_and_hashed_in_the_reviewed_lock(self):
        manifest = json.loads((ROOT / "container" / "package.json").read_text())
        lock = json.loads((ROOT / "container" / "package-lock.json").read_text())
        pin = manifest["dependencies"]["pyright"]
        entry = lock["packages"]["node_modules/pyright"]
        self.assertEqual(entry["version"], pin)
        self.assertTrue(entry["integrity"].startswith("sha512-"), entry.get("integrity"))
        self.assertEqual(sorted(entry["bin"]), ["pyright", "pyright-langserver"])

    def test_the_initializer_mounts_and_stages_it(self):
        compose = (ROOT / "compose.yaml").read_text()
        self.assertIn("- ./runtime/serena-config.yml:/stage/serena-config.yml:ro", compose)
        initializer = (ROOT / "container" / "initialize-agent-state.py").read_text()
        self.assertIn("stage_serena_config(args.stage, args.serena_home", initializer)


if __name__ == "__main__":
    unittest.main()
