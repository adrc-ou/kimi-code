import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "select_versions", ROOT / "scripts" / "select_versions.py"
)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class VersionSelectionTests(unittest.TestCase):
    def test_catalog_contains_only_locked_platform_entries(self):
        catalog, latest = MODULE.comfy_catalog("wsl2-x86_64")
        self.assertTrue(catalog)
        self.assertEqual(catalog[0]["version"], latest)
        self.assertRegex(catalog[0]["commit"], r"^[0-9a-f]{40}$")

    def test_installed_version_replaces_tenth_choice(self):
        catalog = [{"version": f"1.{minor}.0"} for minor in range(20, 0, -1)]
        choices = MODULE.visible_choices(catalog, "1.1.0")
        self.assertEqual(len(choices), 10)
        self.assertIn("1.1.0", {item["version"] for item in choices})
        self.assertNotIn("1.11.0", {item["version"] for item in choices})
        self.assertEqual(
            [item["version"] for item in choices],
            [f"1.{minor}.0" for minor in range(20, 11, -1)] + ["1.1.0"],
        )

    def test_existing_recent_install_does_not_expand_list(self):
        catalog = [{"version": f"2.{minor}.0"} for minor in range(12, 0, -1)]
        choices = MODULE.visible_choices(catalog, "2.11.0")
        self.assertEqual(len(choices), 10)
        self.assertEqual(choices[0]["version"], "2.12.0")
        self.assertEqual(choices[1]["version"], "2.11.0")

    def test_semver_sort_is_numeric(self):
        catalog = [
            {"version": "1.9.0"},
            {"version": "1.11.0"},
            {"version": "1.10.0"},
        ]
        choices = MODULE.visible_choices(catalog, "")
        self.assertEqual(
            [item["version"] for item in choices],
            ["1.11.0", "1.10.0", "1.9.0"],
        )


if __name__ == "__main__":
    unittest.main()
