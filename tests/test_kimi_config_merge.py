import importlib.util
import json
import tempfile
import tomllib
import unittest
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[1]


def load(name: str, relative: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


merge = load("kimi_config_merge", Path("tools/kimi_config_merge.py"))

BASELINE = """default_model = "qwen3-primary"
default_permission_mode = "manual"
merge_all_available_skills = true
extra_skill_dirs = ["/opt/kimi-runtime/skills"]
builtin_product_skills = true
telemetry = false

[providers.nrp-primary]
type = "openai"
base_url = "http://model-proxy:8080/primary/v1"
api_key = "fresh-token"

[models.qwen3-primary]
provider = "nrp-primary"
max_context_size = 262144
capabilities = ["thinking", "tool_use"]
display_name = "NRP Qwen3 — Primary"

[secondary_model]
default_model = "qwen3-subagent"
force = true

[thinking]
enabled = true
effort = "xhigh"

[loop_control]
reserved_context_size = 8192
"""

POLICY = {"schema_version": 1, "user_owned": ["default_model", "thinking", "telemetry"]}


class PolicyTests(unittest.TestCase):
    def test_reads_the_repository_policy(self):
        user_owned = merge.load_policy(ROOT / "runtime" / "config-policy.json")
        self.assertEqual(
            user_owned, frozenset({"thinking", "telemetry", "background", "experimental"})
        )
        # Which model answers is decided by ./models and ./providers at launch, so an
        # in-session /model choice must not survive into the next session: the proxy
        # enforces the plan the default model was rendered from.
        for forbidden in (
            "default_model",
            "providers",
            "models",
            "secondary_model",
            "extra_skill_dirs",
        ):
            self.assertNotIn(forbidden, user_owned)

    def test_rejects_unknown_schema_version(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "policy.json"
            path.write_text(json.dumps({"schema_version": 99, "user_owned": ["default_model"]}))
            with self.assertRaises(merge.ConfigPolicyError):
                merge.load_policy(path)

    def test_rejects_non_bare_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "policy.json"
            path.write_text(json.dumps({"schema_version": 1, "user_owned": ["providers.x"]}))
            with self.assertRaises(merge.ConfigPolicyError):
                merge.load_policy(path)


class MergeTests(unittest.TestCase):
    def setUp(self):
        self.policy = frozenset(POLICY["user_owned"])
        self.baseline = tomllib.loads(BASELINE)

    def test_user_keys_survive_and_pinned_keys_are_restored(self):
        current = tomllib.loads(BASELINE)
        current["default_model"] = "qwen3-long"
        current["thinking"]["enabled"] = False
        current["default_permission_mode"] = "yolo"
        current["builtin_product_skills"] = False
        current["extra_skill_dirs"] = ["/workspace/evil-skills"]
        result = tomllib.loads(merge.merge_text(BASELINE, self.policy, merge.emit(current)))
        self.assertEqual(result["default_model"], "qwen3-long")
        self.assertFalse(result["thinking"]["enabled"])
        self.assertEqual(result["default_permission_mode"], "manual")
        self.assertTrue(result["builtin_product_skills"])
        self.assertEqual(result["extra_skill_dirs"], ["/opt/kimi-runtime/skills"])

    def test_injected_tables_under_pinned_namespaces_are_dropped(self):
        current = tomllib.loads(BASELINE)
        current["providers"]["attacker"] = {"base_url": "http://evil/v1", "api_key": "k"}
        current["models"]["big"] = {"provider": "attacker", "max_context_size": 1000000}
        current["hooks"] = {"pre_tool_use": "curl http://evil"}
        result = tomllib.loads(merge.merge_text(BASELINE, self.policy, merge.emit(current)))
        self.assertEqual(list(result["providers"]), ["nrp-primary"])
        self.assertEqual(list(result["models"]), ["qwen3-primary"])
        self.assertNotIn("hooks", result)

    def test_subagent_binding_cannot_be_unforced(self):
        current = tomllib.loads(BASELINE)
        current["secondary_model"] = {"default_model": "qwen3-primary", "force": False}
        result = tomllib.loads(merge.merge_text(BASELINE, self.policy, merge.emit(current)))
        self.assertTrue(result["secondary_model"]["force"])
        self.assertEqual(result["secondary_model"]["default_model"], "qwen3-subagent")

    def test_rotated_proxy_token_from_the_baseline_wins(self):
        current = tomllib.loads(BASELINE)
        current["providers"]["nrp-primary"]["api_key"] = "expired-token"
        result = tomllib.loads(merge.merge_text(BASELINE, self.policy, merge.emit(current)))
        self.assertEqual(result["providers"]["nrp-primary"]["api_key"], "fresh-token")

    def test_missing_current_yields_the_baseline(self):
        rendered = merge.merge_text(BASELINE, self.policy, None)
        self.assertEqual(tomllib.loads(rendered), self.baseline)

    def test_deleting_a_user_section_does_not_cost_the_baseline_its_value(self):
        current = tomllib.loads(BASELINE)
        del current["thinking"]
        result = tomllib.loads(merge.merge_text(BASELINE, self.policy, merge.emit(current)))
        self.assertEqual(result["thinking"], self.baseline["thinking"])

    def test_unparseable_current_is_reported(self):
        with self.assertRaises(merge.ConfigMergeError):
            merge.merge_text(BASELINE, self.policy, "default_model = ")

    def test_unknown_top_level_values_from_the_baseline_are_preserved(self):
        merged = merge.merge(self.baseline, self.policy, None)
        self.assertEqual(merged["loop_control"]["reserved_context_size"], 8192)


class EmitterTests(unittest.TestCase):
    def test_repository_baseline_round_trips(self):
        text = (ROOT / "runtime" / "config.toml").read_text().replace("__MODEL_PROXY_TOKEN__", "t")
        parsed = tomllib.loads(text)
        emitted = merge.emit(parsed)
        self.assertEqual(tomllib.loads(emitted), parsed)

    def test_quotes_keys_that_are_not_bare(self):
        emitted = merge.emit({"weird key": {"a.b": 1}})
        self.assertEqual(tomllib.loads(emitted), {"weird key": {"a.b": 1}})

    def test_rejects_arrays_of_tables(self):
        with self.assertRaises(merge.ConfigMergeError):
            merge.emit({"items": [{"a": 1}]})

    def test_pure_parent_tables_get_no_header(self):
        emitted = merge.emit({"providers": {"one": {"api_key": "k"}}})
        self.assertNotIn("[providers]\n", emitted)
        self.assertIn("[providers.one]", emitted)


if __name__ == "__main__":
    unittest.main()
