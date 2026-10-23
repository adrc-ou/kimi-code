"""Interface drift is detected without changing permission policy or trusting server prose."""

import copy
import json
import math
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools import mcp_compatibility as compat

ROOT = Path(__file__).resolve().parents[1]


def tool(name="read", **fields):
    return {"name": name, "description": "Read public documentation",
            "inputSchema": {"type": "object", "properties": {"repo": {"type": "string"}},
                            "required": ["repo"]}, **fields}


def snapshot(config=None, tools=None):
    catalog = compat.Catalog()
    catalog.add_page([tool()] if tools is None else tools)
    return catalog.snapshot(config or {"url": "https://example.invalid/mcp"})


def contract(config=None, tools=None):
    return {"reviewed_at": "2026-10-23", **snapshot(config, tools)}


class CompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.config = {"url": "https://example.invalid/mcp", "enabledTools": ["read"]}
        self.baseline = contract(self.config)

    def compare(self, tools, config=None):
        config = config or self.config
        return compat.compare(config, self.baseline, snapshot(config, tools))

    def test_match_does_not_change_configuration_or_contract(self):
        before = copy.deepcopy((self.config, self.baseline))
        result = self.compare([tool()])
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["compatibility"]["verdict"], "matched")
        self.assertEqual((self.config, self.baseline), before)

    def test_rename_reports_missing_and_added_without_guessing_an_alias(self):
        result = self.compare([tool("renamed_read")])
        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(result["compatibility"]["missing"], ["read"])
        self.assertEqual(result["compatibility"]["added"], ["renamed_read"])

    def test_new_excluded_tool_is_a_notice_and_stays_excluded(self):
        result = self.compare([tool(), tool("write")])
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["compatibility"]["added"], ["write"])
        self.assertEqual(set(result["compatibility"]["observed"]["tools"]), {"read"})

    def test_removed_unselected_tool_does_not_fail_selected_contract(self):
        baseline = contract(self.config, [tool(), tool("write")])
        result = compat.compare(self.config, baseline, snapshot(self.config))
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["compatibility"]["removed"], ["write"])

    def test_unrestricted_catalog_addition_or_removal_requires_review(self):
        config = {"url": "https://example.invalid/mcp"}
        baseline = contract(config, [tool(), tool("write")])
        for tools in ([tool()], [tool(), tool("write"), tool("delete")]):
            result = compat.compare(config, baseline, snapshot(config, tools))
            self.assertEqual(result["status"], "FAIL")
            self.assertEqual(result["compatibility"]["verdict"], "review")

    def test_input_and_output_schema_changes_require_review(self):
        for fields in ({"inputSchema": {"type": "object"}},
                       {"outputSchema": {"type": "object"}}):
            result = self.compare([tool(**fields)])
            self.assertEqual(result["status"], "FAIL")
            self.assertEqual(result["compatibility"]["schema_changed"], ["read"])

    def test_description_and_annotations_are_metadata_not_proven_wire_breakage(self):
        for fields in ({"description": "Different semantics"},
                       {"annotations": {"readOnlyHint": False}}):
            result = self.compare([tool(**fields)])
            self.assertEqual(result["status"], "SETUP")
            self.assertEqual(result["compatibility"]["metadata_changed"], ["read"])
            self.assertEqual(result["compatibility"]["schema_changed"], [])

    def test_reports_include_fingerprints_and_never_server_prose(self):
        result = self.compare([tool(description="private-untrusted-fixture")])
        self.assertNotIn("private-untrusted-fixture", json.dumps(result))
        self.assertNotIn("inputSchema", json.dumps(result))

    def test_missing_hosted_baseline_is_setup_and_local_server_is_not_enrolled(self):
        for config, status, verdict in ((self.config, "SETUP", "unbaselined"),
                                        ({"command": "local"}, "PASS", "not_enrolled")):
            result = compat.compare(config, None, snapshot(config))
            self.assertEqual(result["status"], status)
            self.assertEqual(result["compatibility"]["verdict"], verdict)

    def test_authentication_profile_mismatch_is_never_a_pass(self):
        config = {**self.config, "bearerTokenEnvVar": "FIXTURE_TOKEN"}
        result = compat.compare(config, self.baseline, snapshot(config))
        self.assertEqual(result["status"], "SETUP")
        self.assertEqual(result["compatibility"]["verdict"], "unverified")

    def test_hashing_is_deterministic_but_preserves_array_order(self):
        self.assertEqual(compat.fingerprint({"b": 2, "a": 1}),
                         compat.fingerprint({"a": 1, "b": 2}))
        self.assertNotEqual(compat.fingerprint(["a", "b"]), compat.fingerprint(["b", "a"]))

    def test_invalid_duplicate_and_repeated_cursor_catalogs_fail(self):
        for tools in ([tool(), tool()], [tool("forged\nPASS")],
                      [tool(inputSchema="invalid")], [tool(outputSchema=[])],
                      [tool(inputSchema={"value": math.nan})]):
            with self.subTest(tools=tools), self.assertRaises(compat.CatalogError):
                snapshot(self.config, tools)
        catalog = compat.Catalog()
        catalog.add_page([tool()], "cursor")
        with self.assertRaises(compat.CatalogError):
            catalog.add_page([tool("next")], "cursor")

    def test_incremental_catalog_limits_and_nested_or_cyclic_data_fail(self):
        with patch.object(compat, "MAX_TOOLS", 1), self.assertRaises(compat.CatalogError):
            snapshot(self.config, [tool(), tool("second")])
        with patch.object(compat, "MAX_CATALOG_BYTES", 1), \
                self.assertRaises(compat.CatalogError):
            snapshot()
        catalog = compat.Catalog()
        with patch.object(compat, "MAX_PAGES", 1):
            catalog.add_page([tool()], "next")
            with self.assertRaises(compat.CatalogError):
                catalog.add_page([tool("next")])
        nested = {}
        nested["cycle"] = nested
        with self.assertRaises(compat.CatalogError):
            compat.canonical(nested)
        with self.assertRaises(compat.CatalogError):
            compat.canonical({"long": "x" * 1000}, 100)

    def test_source_contracts_cover_current_hosted_configuration_offline(self):
        compat.validate_sources(ROOT)


class ContractManifestTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "mcp-contracts.json"
        self.servers = {"demo": {"url": "https://example.invalid", "enabledTools": ["read"]}}

    def write(self, entry):
        self.path.write_text(json.dumps({"schema_version": 1, "servers": {"demo": entry}}))

    def test_valid_contract_loads_and_missing_manifest_does_not_auto_enroll(self):
        self.assertEqual(compat.load_manifest(self.path, self.servers), {})
        self.write(contract(self.servers["demo"]))
        self.assertEqual(set(compat.load_manifest(self.path, self.servers)), {"demo"})

    def test_ownership_fingerprints_profile_and_coverage_are_validated(self):
        original = contract(self.servers["demo"])
        variants = []
        entry = copy.deepcopy(original)
        entry["tools"]["read"]["input_schema_sha256"] = "invalid"
        variants.append(entry)
        variants.append({**original, "tools": {}})
        variants.append({**original, "auth_profile": "configured-bearer"})
        variants.append({**original, "catalog_names": ["read", "read"]})
        variants.append({**original, "reviewed_at": "not-a-date"})
        variants.append({**original, "unexpected": "data"})
        for entry in variants:
            self.write(entry)
            with self.assertRaises(compat.ContractError):
                compat.load_manifest(self.path, self.servers)
        self.write(original)
        with self.assertRaises(compat.ContractError):
            compat.load_manifest(self.path, {})

    def test_missing_required_source_baseline_is_an_offline_failure(self):
        runtime = self.path.parent / "runtime"
        runtime.mkdir()
        (runtime / "mcp.json").write_text(json.dumps({"mcpServers": self.servers}))
        with self.assertRaises(compat.ContractError):
            compat.validate_sources(self.path.parent)

    def test_duplicate_json_fields_and_broken_symlinks_are_rejected(self):
        self.path.write_text('{"schema_version":1,"schema_version":1,"servers":{}}')
        with self.assertRaises(compat.ContractError):
            compat.load_manifest(self.path, self.servers)
        self.path.unlink()
        self.path.symlink_to(self.path.parent / "absent")
        with self.assertRaises(compat.ContractError):
            compat.load_manifest(self.path, self.servers)
