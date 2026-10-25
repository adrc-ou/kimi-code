"""Static host eligibility is separate from available-memory context fitting."""

import io
import json
import os
import unittest
from unittest import mock

from tests.test_capacity_policy import CapacityFixture
from tools import capacity, models
from tools.tui.app import Result


class Screen(io.StringIO):
    def isatty(self):
        return True


class NativePickerTests(CapacityFixture):
    def setUp(self):
        super().setUp()
        self.runtime = self.root / "runtime"
        self.runtime.mkdir()
        (self.runtime / "config.toml").write_text("[loop_control]\nreserved_context_size = 512\n")
        self.measured = {
            "system": "Darwin",
            "arch": "arm64",
            "capabilities": ["metal"],
            "memory_gib": 0,
            "memory_bytes": 0,
            "available_memory_bytes": 0,
            "free_disk_gib": 100,
        }

    def test_uninstalled_models_without_a_ram_floor_remain_selectable(self):
        steps = []

        def choose(step, *_):
            steps.append(step)
            option = next(choice for choice in step.choices if choice.id == self.local)
            self.assertTrue(option.enabled)
            self.assertEqual(option.cells[3], "Not Installed")
            return Result(value=self.local)

        with (
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch.object(models.host_caps, "caps", return_value=self.measured),
            mock.patch.object(models.host_caps, "probe") as fresh,
            mock.patch.object(capacity, "project") as estimate,
            mock.patch.object(capacity.local_llm, "engine_binary") as binary,
            mock.patch.object(models.sys, "stdin", Screen()),
            mock.patch.object(models.sys, "stdout", Screen()),
            mock.patch.object(models, "run", side_effect=choose),
        ):
            self.assertEqual(models.cmd_select(self.root, self.runtime, non_interactive=False), 0)
        fresh.assert_not_called()
        estimate.assert_not_called()
        binary.assert_not_called()
        self.assertEqual(len(steps), 2)
        selected = json.loads((self.runtime / models.SELECTION).read_text())
        self.assertEqual(selected["primary"], self.local)
        self.assertEqual(selected["subagent"], self.local)
        self.assertFalse((self.runtime / models.POLICY_FILE).exists())

    def test_remembered_selection_and_overrides_work_without_memory_measurements(self):
        previous = {"primary": self.local, "subagent": self.local}
        (self.runtime / models.PREVIOUS_SELECTION).write_text(json.dumps(previous))
        for environment in (
            {},
            {"HARNESS_PRIMARY_MODEL": self.local, "HARNESS_SUBAGENT_MODEL": self.local},
        ):
            with (
                self.subTest(environment=environment),
                mock.patch.dict(os.environ, environment, clear=True),
                mock.patch.object(models.host_caps, "caps", return_value=self.measured),
                mock.patch.object(models.sys, "stdout", Screen()),
            ):
                self.assertEqual(
                    models.cmd_select(self.root, self.runtime, non_interactive=True), 0
                )
                chosen = json.loads((self.runtime / models.SELECTION).read_text())
                self.assertEqual(chosen["primary"], self.local)
                self.assertEqual(chosen["subagent"], self.local)

    def test_platform_and_installation_disk_gates_remain(self):
        for snapshot in (
            dict(self.measured, capabilities=[]),
            dict(self.measured, free_disk_gib=0),
        ):
            with (
                self.subTest(snapshot=snapshot),
                mock.patch.dict(os.environ, {"HARNESS_PRIMARY_MODEL": self.local}, clear=True),
                mock.patch.object(models.host_caps, "caps", return_value=snapshot),
                self.assertRaises(models.DefinitionError),
            ):
                models.cmd_select(self.root, self.runtime, non_interactive=True)

    def test_installed_ram_floor_filters_choices_and_cannot_be_overridden(self):
        path = self.root / "models/local_fixture/local/model.toml"
        path.write_text(path.read_text().replace("[requires]", "[requires]\nmemory_gib = 4"))
        for installed in (0, 3, 4):
            snapshot = dict(self.measured, memory_gib=installed)
            with (
                self.subTest(installed=installed),
                mock.patch.dict(
                    os.environ,
                    {"HARNESS_PRIMARY_MODEL": self.local, "HARNESS_SUBAGENT_MODEL": self.local},
                    clear=True,
                ),
                mock.patch.object(models.host_caps, "caps", return_value=snapshot),
                mock.patch.object(models.sys, "stdout", Screen()),
            ):
                if installed < 4:
                    with self.assertRaises(models.DefinitionError):
                        models.cmd_select(self.root, self.runtime, non_interactive=True)
                else:
                    self.assertEqual(
                        models.cmd_select(self.root, self.runtime, non_interactive=True), 0
                    )
                    # Available RAM is still zero: it must not shrink the native window.
                    providers, items = models.load_definitions(self.root)
                    model = next(item for item in items if item["id"] == self.local)
                    engine = capacity.allocate([model], providers, 512)["engines"][self.local]
                    self.assertEqual(engine["context_tokens"], model["native_tokens"])
                    self.assertEqual(engine["sequences"], 1)

    def test_stale_local_plan_requires_a_relaunch(self):
        (self.runtime / models.POLICY_FILE).write_text(json.dumps({"schema_version": 2}))
        with self.assertRaisesRegex(models.DefinitionError, "relaunch"):
            models.load_plan(self.runtime)


if __name__ == "__main__":
    unittest.main()
