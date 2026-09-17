"""Definition-tree validation: what a bad ``./models`` or ``./providers`` entry must do.

These trees decide which endpoint receives an API key and how much traffic it may carry, so a
malformed manifest has to stop the launch loudly. Each case writes a minimal valid pair and
breaks exactly one thing in it.
"""

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(ROOT / "tools"))

import definitions  # noqa: E402

PROVIDER = """
schema_version = 1
label = "Fixture Provider"
policy_url = "https://fixture.invalid/policy"

[endpoint]
base_url = "https://fixture.invalid"
base_url_env = "FIXTURE_BASE_URL"
protocol = "openai"

[[credential]]
id = "default"
label = "Fixture API token"

[[rule]]
kind = "aggregate_context_fraction"
scope = "model"
percent = 35

[[rule]]
kind = "output_tokens_per_minute"
scope = "credential_model"
limit = 200000
"""

MODEL = """
schema_version = 1
label = "Fixture Model"
provider = "fixture"
model = "fixture-model"
slug = "fixturemodel"
credential = "default"

[context]
advertised_tokens = 16384

[lane.primary]
context_tokens = 8192
input_tokens = 6144
output_clamp_tokens = 2048

[lane.subagent]
context_tokens = 4096
input_tokens = 3072
output_clamp_tokens = 1024
"""


def write_tree(
    root: Path,
    provider: str = PROVIDER,
    models: dict[str, str] | None = None,
    provider_id: str = "fixture",
) -> Path:
    directory = root / "providers" / provider_id
    directory.mkdir(parents=True)
    (directory / "provider.toml").write_text(provider)
    for name, text in (models or {"fixture_model": MODEL}).items():
        model_dir = root / "models" / name
        model_dir.mkdir(parents=True)
        (model_dir / "model.toml").write_text(text)
    return root


class DefinitionFixture(unittest.TestCase):
    def tree(self, **kwargs) -> Path:
        base = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, base, ignore_errors=True)
        return write_tree(base, **kwargs)

    def load(self, **kwargs):
        return definitions.load_definitions(self.tree(**kwargs))

    def assertRefused(self, **kwargs) -> str:
        with self.assertRaises(definitions.DefinitionError) as caught:
            self.load(**kwargs)
        return str(caught.exception)


class ProviderTests(DefinitionFixture):
    def test_a_valid_tree_loads(self):
        providers, models = self.load()
        self.assertEqual(list(providers), ["fixture"])
        self.assertEqual([item["id"] for item in models], ["fixture_model"])
        provider = providers["fixture"]
        self.assertEqual(provider["base_url"], "https://fixture.invalid")
        self.assertEqual(provider["protocol"], "openai")
        self.assertEqual(provider["base_url_env"], "FIXTURE_BASE_URL")
        self.assertEqual(provider["context_margin_percent"], 100)
        self.assertEqual([rule["kind"] for rule in provider["rules"]][:2],
                         ["aggregate_context_fraction", "output_tokens_per_minute"])

    def test_credential_names_cannot_collide_across_providers(self):
        providers, _ = self.load()
        self.assertEqual(providers["fixture"]["credentials"]["default"]["secret_name"],
                         "fixture__default")

    def test_a_manifest_of_another_schema_version_is_refused(self):
        for name, text in (
            ("another version", PROVIDER.replace("schema_version = 1", "schema_version = 2")),
            ("no version at all", PROVIDER.replace("schema_version = 1\n", "")),
        ):
            with self.subTest(case=name), self.assertRaises(definitions.DefinitionError):
                self.load(provider=text)

    def test_an_unknown_table_is_refused_rather_than_ignored(self):
        message = self.assertRefused(provider=PROVIDER + '\n[somethingelse]\nx = 1\n')
        self.assertIn("unknown key", message)

    def test_a_malformed_endpoint_is_refused(self):
        cases = {
            "a path on the origin": PROVIDER.replace(
                'base_url = "https://fixture.invalid"', 'base_url = "https://fixture.invalid/v1"'
            ),
            "a non-http origin": PROVIDER.replace(
                'base_url = "https://fixture.invalid"', 'base_url = "ftp://fixture.invalid"'
            ),
            "an unknown protocol": PROVIDER.replace('protocol = "openai"', 'protocol = "grpc"'),
            "a lowercase override name": PROVIDER.replace(
                'base_url_env = "FIXTURE_BASE_URL"', 'base_url_env = "fixture_base_url"'
            ),
            "a missing endpoint": PROVIDER.replace("[endpoint]", "[not-endpoint]"),
        }
        for name, text in cases.items():
            with self.subTest(case=name), self.assertRaises(definitions.DefinitionError):
                self.load(provider=text)

    def test_a_policy_link_must_be_https(self):
        message = self.assertRefused(
            provider=PROVIDER.replace(
                'policy_url = "https://fixture.invalid/policy"',
                'policy_url = "http://fixture.invalid/policy"',
            )
        )
        self.assertIn("https URL", message)

    def test_a_credential_prompt_defaults_to_its_label(self):
        providers, _ = self.load()
        credential = providers["fixture"]["credentials"]["default"]
        self.assertEqual(credential["prompt"], credential["label"])

    def test_a_provider_with_no_credential_or_no_rule_is_refused(self):
        cases = {
            "no credential": PROVIDER.replace("[[credential]]", "[[nostream]]"),
            "no rule": PROVIDER.split("[[rule]]")[0],
        }
        for name, text in cases.items():
            with self.subTest(case=name), self.assertRaises(definitions.DefinitionError):
                self.load(provider=text)

    def test_duplicate_credential_ids_are_refused(self):
        message = self.assertRefused(
            provider=PROVIDER + '\n[[credential]]\nid = "default"\nlabel = "again"\n'
        )
        self.assertIn("twice", message)

    def test_safety_margins_are_percentages(self):
        for value in (0, 101):
            with self.subTest(percent=value):
                self.assertRefused(
                    provider=PROVIDER + f'\n[safety]\ncontext_margin_percent = {value}\n'
                )
        providers, _ = self.load(
            provider=PROVIDER + "\n[safety]\ncontext_margin_percent = 95\n"
        )
        self.assertEqual(providers["fixture"]["context_margin_percent"], 95)


class RuleTaxonomyTests(DefinitionFixture):
    def rule(self, text: str) -> str:
        return PROVIDER.split("[[rule]]")[0] + text

    def test_every_shipped_kind_parses_in_a_legal_scope(self):
        kinds = {
            "output_tokens_per_minute": ('scope = "model"', "limit = 100"),
            "input_tokens_per_minute": ('scope = "credential"', "limit = 100"),
            "tokens_per_minute": ('scope = "provider"', "limit = 100"),
            "requests_per_minute": ('scope = "credential_model"', "limit = 100"),
            "max_concurrent_requests": ('scope = "model"', "limit = 8"),
            "max_output_tokens_per_request": ('scope = "model"', "limit = 4096"),
            "max_context_tokens": ('scope = "provider"', "limit = 200000"),
            "aggregate_context_fraction": ('scope = "model"', "percent = 35"),
            "exclusive_above_context_fraction": ('scope = "model"', "percent = 35"),
        }
        for kind, (scope, value) in kinds.items():
            with self.subTest(kind=kind):
                providers, _ = self.load(
                    provider=self.rule(f'\n[[rule]]\nkind = "{kind}"\n{scope}\n{value}\n')
                )
                self.assertEqual(providers["fixture"]["rules"][0], {
                    "kind": kind,
                    "scope": scope.split('"')[1],
                    "value": int(value.split("= ")[1]),
                    "models": None,
                })

    def test_a_kind_this_harness_cannot_enforce_is_refused(self):
        message = self.assertRefused(
            provider=self.rule('\n[[rule]]\nkind = "dollars_per_day"\nscope = "model"\nlimit = 5\n')
        )
        self.assertIn("not a rule this harness can enforce", message)

    def test_a_scope_a_family_cannot_mean_is_refused(self):
        # A context fraction has no single window to measure when it is scoped to a whole
        # provider, so guessing at one would silently invent a limit the provider never set.
        for kind, scope in (
            ("aggregate_context_fraction", "provider"),
            ("exclusive_above_context_fraction", "credential"),
            ("max_output_tokens_per_request", "credential_model"),
        ):
            with self.subTest(kind=kind, scope=scope):
                message = self.assertRefused(
                    provider=self.rule(
                        f'\n[[rule]]\nkind = "{kind}"\nscope = "{scope}"\n'
                        + ("percent = 35\n" if "fraction" in kind else "limit = 100\n")
                    )
                )
                self.assertIn("cannot use scope", message)

    def test_a_rule_without_its_value_field_is_refused(self):
        message = self.assertRefused(
            provider=self.rule('\n[[rule]]\nkind = "max_concurrent_requests"\nscope = "model"\n')
        )
        self.assertIn("must set limit", message)
        self.assertRefused(
            provider=self.rule(
                '\n[[rule]]\nkind = "aggregate_context_fraction"\nscope = "model"\nlimit = 35\n'
            )
        )

    def test_a_rule_models_list_must_be_useful(self):
        for models in ("[]", '["ok", ""]', "42"):
            with self.subTest(models=models):
                self.assertRefused(
                    provider=self.rule(
                        '\n[[rule]]\nkind = "max_concurrent_requests"\nscope = "model"\n'
                        f"limit = 4\nmodels = {models}\n"
                    )
                )

    def test_a_rule_may_be_restricted_to_named_wire_models(self):
        providers, _ = self.load(
            provider=self.rule(
                '\n[[rule]]\nkind = "max_concurrent_requests"\nscope = "model"\n'
                'limit = 4\nmodels = ["fixture-model", "other-model"]\n'
            )
        )
        self.assertEqual(
            providers["fixture"]["rules"][0]["models"], ["fixture-model", "other-model"]
        )

    def test_a_rule_with_an_unknown_field_is_refused(self):
        message = self.assertRefused(
            provider=self.rule(
                '\n[[rule]]\nkind = "max_concurrent_requests"\nscope = "model"\n'
                "limit = 4\nburst = 9\n"
            )
        )
        self.assertIn("burst", message)


class ModelTests(DefinitionFixture):
    def test_a_model_is_only_offered_when_its_provider_exists(self):
        # Dropping rather than failing is the documented behaviour: an operator may keep a
        # model directory in place while its provider definition is absent.
        orphan = MODEL.replace('provider = "fixture"', 'provider = "nowhere"')
        providers, models = self.load(models={"fixture_model": MODEL, "orphan": orphan})
        self.assertEqual([item["id"] for item in models], ["fixture_model"])
        self.assertNotIn("nowhere", providers)

    def test_a_model_with_no_lane_entry_is_refused(self):
        for lane in ("primary", "subagent"):
            with self.subTest(lane=lane):
                message = self.assertRefused(
                    models={"fixture_model": MODEL.replace(f"[lane.{lane}]", "[lane.none]")}
                )
                self.assertIn(lane, message)

    def test_a_lane_the_harness_does_not_serve_is_refused(self):
        message = self.assertRefused(
            models={"fixture_model": MODEL.replace("[lane.subagent]", "[lane.background]")}
        )
        self.assertIn("the harness serves", message)

    def test_a_long_lane_is_optional_but_must_still_be_sane(self):
        long_lane = (
            "\n[lane.long]\ncontext_tokens = 16384\n"
            "input_tokens = 12288\noutput_clamp_tokens = 4096\n"
        )
        _, models = self.load(models={"fixture_model": MODEL + long_lane})
        self.assertEqual(sorted(models[0]["lanes"]), ["long", "primary", "subagent"])
        self.assertRefused(
            models={"fixture_model": MODEL + long_lane.replace("output_clamp_tokens = 4096\n", "")}
        )

    def test_advertised_context_must_cover_every_lane(self):
        message = self.assertRefused(
            models={
                "fixture_model": MODEL.replace(
                    "advertised_tokens = 16384", "advertised_tokens = 4096"
                )
            }
        )
        self.assertIn("smaller than a declared lane window", message)

    def test_advertised_context_defaults_to_the_widest_lane(self):
        _, models = self.load(
            models={
                "fixture_model": MODEL.replace("[context]\nadvertised_tokens = 16384\n", "")
            }
        )
        self.assertEqual(models[0]["advertised_tokens"], 8192)

    def test_a_slug_that_cannot_form_an_alias_is_refused(self):
        for slug in ('"Fixture Model"', '"9lives"', '""'):
            with self.subTest(slug=slug):
                self.assertRefused(
                    models={
                        "fixture_model": MODEL.replace(
                            'slug = "fixturemodel"', f"slug = {slug}"
                        )
                    }
                )

    def test_two_models_cannot_share_a_slug(self):
        message = self.assertRefused(
            models={
                "a_model": MODEL,
                "b_model": MODEL.replace('label = "Fixture Model"', 'label = "Other"'),
            }
        )
        self.assertIn("share slug", message)

    def test_a_model_naming_an_unknown_credential_is_refused(self):
        # The provider is where credential ids are defined, so the resolver - not the parser -
        # is what catches this one; loading both trees must still refuse the pair.
        import policy

        providers, models = self.load()
        models[0]["credential"] = "nope"
        with self.assertRaises(ValueError):
            policy.resolve(
                providers,
                {item["id"]: item for item in models},
                {"primary": "fixture_model", "subagent": "fixture_model"},
                reserved_context_size=1024,
            )

    def test_lane_numbers_must_be_positive(self):
        # Each value occurs once in the fixture, on the primary lane.
        cases = {
            "a zero window": MODEL.replace("context_tokens = 8192", "context_tokens = 0"),
            "a negative input": MODEL.replace("input_tokens = 6144", "input_tokens = -1"),
            "a boolean clamp": MODEL.replace(
                "output_clamp_tokens = 2048", "output_clamp_tokens = true"
            ),
            "a missing input": MODEL.replace("input_tokens = 6144\n", ""),
        }
        for name, text in cases.items():
            with self.subTest(case=name):
                self.assertRefused(models={"fixture_model": text})


class TreeHygieneTests(DefinitionFixture):
    def test_a_directory_without_a_manifest_is_ignored(self):
        root = self.tree()
        (root / "models" / "notes").mkdir()
        (root / "models" / "notes" / "README.md").write_text("work in progress\n")
        _, models = definitions.load_definitions(root)
        self.assertEqual([item["id"] for item in models], ["fixture_model"])

    def test_an_underscored_or_uppercase_directory_is_refused(self):
        for name in ("Fixture", "fix ture"):
            base = Path(tempfile.mkdtemp())
            self.addCleanup(shutil.rmtree, base, ignore_errors=True)
            write_tree(base, provider_id="f")
            os.rename(base / "providers" / "f", base / "providers" / name)
            with self.subTest(name=name), self.assertRaises(definitions.DefinitionError):
                definitions.load_definitions(base)

    def test_a_symlinked_manifest_is_refused(self):
        root = self.tree()
        target = root / "elsewhere.toml"
        target.write_text(MODEL)
        (root / "models" / "fixture_model" / "model.toml").unlink()
        (root / "models" / "fixture_model" / "model.toml").symlink_to(target)
        with self.assertRaisesRegex(definitions.DefinitionError, "Unsafe model definition"):
            definitions.load_definitions(root)

    def test_a_symlink_inside_a_tree_is_refused(self):
        root = self.tree()
        (root / "models" / "fixture_model" / "link").symlink_to(root / "providers")
        with self.assertRaisesRegex(definitions.DefinitionError, "Unsafe"):
            definitions.load_definitions(root)

    def test_invalid_toml_is_refused_with_a_readable_message(self):
        message = self.assertRefused(models={"fixture_model": "this is not toml"})
        self.assertIn("not valid TOML", message)


class ShippedTreeTests(unittest.TestCase):
    """The definitions this repository ships with are themselves valid."""

    def test_the_shipped_trees_load(self):
        providers, models = definitions.load_definitions(ROOT)
        self.assertTrue(providers and models)
        for model in models:
            with self.subTest(model=model["id"]):
                self.assertIn(model["provider"], providers)
                self.assertIn(model["credential"], providers[model["provider"]]["credentials"])
                self.assertTrue({"primary", "subagent"} <= set(model["lanes"]))

    def test_every_shipped_rule_kind_has_an_enforcer_family(self):
        families = (
            definitions.CONTEXT_RULE_KINDS
            | definitions.RATE_RULE_KINDS
            | definitions.COUNT_RULE_KINDS
            | definitions.PER_REQUEST_RULE_KINDS
        )
        self.assertEqual(set(definitions.RULE_KINDS), families)
        for family, scopes in definitions.ALLOWED_SCOPES.items():
            self.assertTrue(family <= set(definitions.RULE_KINDS))
            self.assertTrue(scopes <= definitions.SCOPES)


if __name__ == "__main__":
    unittest.main()
