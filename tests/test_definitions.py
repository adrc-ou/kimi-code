"""Definition-tree validation: what a bad ``./models`` or ``./providers`` entry must do.

These trees decide which endpoint receives an API key and how much traffic it may carry, so a
malformed manifest has to stop the launch loudly. Each case writes a minimal valid pair and
breaks exactly one thing in it.
"""

import os
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(ROOT / "tools"))

import definitions  # noqa: E402
import policy  # noqa: E402

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


#: Which provider a model manifest names, read out of the text so a fixture is filed where its
#: own definition says it belongs. Discovery refuses a model whose directory disagrees with its
#: ``provider`` key, so a writer that filed by some other rule could not build a legal tree.
PROVIDER_KEY = re.compile(r'^provider\s*=\s*"([^"]+)"', re.MULTILINE)


def write_tree(
    root: Path,
    provider: str = PROVIDER,
    models: dict[str, str] | None = None,
    provider_id: str = "fixture",
) -> Path:
    """One provider and its models, laid out the way ``./models`` is laid out.

    Each model is filed under ``models/<provider>/<name>/``, with ``<provider>`` read from the
    manifest rather than assumed from ``provider_id``, so a test can plant a model naming a
    provider that does not exist and discovery will drop it for the real reason. A fixture that
    had to be filed by hand could disagree with its own text, and the tree would then be illegal
    for a reason unrelated to what the test is asking about.
    """
    directory = root / "providers" / provider_id
    directory.mkdir(parents=True)
    (directory / "provider.toml").write_text(provider)
    for name, text in (models or {"fixture_model": MODEL}).items():
        declared = PROVIDER_KEY.search(text)
        model_dir = root / "models" / (declared.group(1) if declared else provider_id) / name
        model_dir.mkdir(parents=True)
        (model_dir / "model.toml").write_text(text)
    return root


class DefinitionFixture(unittest.TestCase):
    def tree_base(self) -> Path:
        """An empty directory to build a tree in, removed with the test."""
        base = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, base, ignore_errors=True)
        return base

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


class TreeShapeTests(DefinitionFixture):
    """Nothing the loader reads may be a link out of the model tree.

    A manifest is trusted to name weights, ports and provider endpoints, and `_walk_safe` exists
    so a definition cannot reach outside its own directory. These guards apply the same rule to the
    tree's own shape, and none of them was executed by any test before this class existed: a
    refactor that dropped one would have loaded a symlinked provider directory as though it were
    shipped source.
    """

    @staticmethod
    def link_over(root: Path, relative: str, holder: Path) -> Path:
        """Move ``relative`` out of the tree and leave a symlink in its place."""
        original = root / relative
        destination = holder / relative.replace("/", "_")
        holder.mkdir(parents=True, exist_ok=True)
        shutil.move(str(original), str(destination))
        original.symlink_to(destination)
        return root

    def test_a_symlinked_providers_root_is_refused(self):
        root = self.link_over(write_tree(self.tree_base()), "providers", self.tree_base())
        with self.assertRaisesRegex(
            definitions.DefinitionError, "providers/ must not be a symlink"
        ):
            definitions.load_definitions(root)

    def test_a_symlinked_models_root_is_refused(self):
        root = self.link_over(write_tree(self.tree_base()), "models", self.tree_base())
        with self.assertRaisesRegex(definitions.DefinitionError, "models/ must not be a symlink"):
            definitions.load_definitions(root)

    def test_a_symlinked_provider_manifest_is_refused(self):
        # Asserted against the message that ACTUALLY answers, found by running it rather than by
        # reading the reader: _walk_safe (called immediately before every manifest parse) already
        # refuses a symlink inside a definition directory, so the reader's own
        # "manifest must not be a symlink" branch is a backstop no public call path can reach.
        # Refusing here is correct either way; only the wording differs.
        root = write_tree(self.tree_base())
        self.link_over(root, "providers/fixture/provider.toml", self.tree_base())
        with self.assertRaisesRegex(
            definitions.DefinitionError, "Unsafe provider definition entry"
        ):
            definitions.load_definitions(root)

    def test_a_provider_directory_named_outside_the_grammar_is_refused(self):
        # The sibling of the model-directory guard below, and it survived a whole mutation pass
        # before this class existed. The directory name becomes the provider id that every selection
        # and every secret name is built from, so a name the grammar rejects has to stop the load.
        root = write_tree(self.tree_base())
        (root / "providers/fixture").rename(root / "providers/FIXTURE")
        with self.assertRaisesRegex(
            definitions.DefinitionError, "Provider directories must be real directories"
        ):
            definitions.load_definitions(root)

    def test_a_symlinked_model_directory_is_refused(self):
        root = write_tree(self.tree_base())
        self.link_over(root, "models/fixture/fixture_model", self.tree_base())
        with self.assertRaisesRegex(
            definitions.DefinitionError, "real directories with lowercase identifiers"
        ):
            definitions.load_definitions(root)

    def test_a_model_directory_named_outside_the_grammar_is_refused(self):
        # Same guard as the link above, reached the other way: the directory name becomes the model
        # id, so anything the identifier grammar rejects must stop the load rather than ship a model
        # whose id cannot be spelled in a selection.
        root = write_tree(self.tree_base())
        (root / "models/fixture/fixture_model").rename(root / "models/fixture/FIXTURE_Model")
        with self.assertRaisesRegex(
            definitions.DefinitionError, "real directories with lowercase identifiers"
        ):
            definitions.load_definitions(root)


class ProviderTests(DefinitionFixture):
    def test_a_valid_tree_loads(self):
        providers, models = self.load()
        self.assertEqual(list(providers), ["fixture"])
        self.assertEqual([item["id"] for item in models], ["fixture/fixture_model"])
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
            ("another version", PROVIDER.replace("schema_version = 1", "schema_version = 3")),
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

    def test_an_identifier_cannot_contain_the_secret_separator(self):
        # Secret names are these identifiers joined with "__", so an identifier carrying the
        # separator itself would make one mounted file reachable under two different names.
        for name, kwargs in (
            ("a credential id", {"provider": PROVIDER.replace('id = "default"', 'id = "a__b"')}),
            ("a provider id", {"provider_id": "a__b"}),
        ):
            with self.subTest(case=name):
                self.assertRefused(**kwargs)

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
                    "default": False,
                })

    def test_a_default_count_rule_parses_at_every_looser_scope(self):
        for scope in ("provider", "credential", "credential_model"):
            with self.subTest(scope=scope):
                providers, _ = self.load(
                    provider=self.rule(
                        f'\n[[rule]]\nkind = "max_concurrent_requests"\nscope = "{scope}"\n'
                        "limit = 2\ndefault = true\n"
                    )
                )
                self.assertEqual(providers["fixture"]["rules"][0]["default"], True)

    def test_only_a_count_rule_may_be_declared_a_default(self):
        # A rate window or a context fraction at a loose scope is a second, genuinely
        # simultaneous cap; displacing with one would drop a rule the provider really stated.
        for kind, scope, value in (
            ("output_tokens_per_minute", "provider", "limit = 100"),
            ("aggregate_context_fraction", "model", "percent = 35"),
            ("max_context_tokens", "provider", "limit = 200000"),
        ):
            with self.subTest(kind=kind):
                message = self.assertRefused(
                    provider=self.rule(
                        f'\n[[rule]]\nkind = "{kind}"\nscope = "{scope}"\n{value}\n'
                        "default = true\n"
                    )
                )
                self.assertIn("default is meaningful only for", message)

    def test_a_default_belongs_at_a_scope_looser_than_one_model(self):
        message = self.assertRefused(
            provider=self.rule(
                '\n[[rule]]\nkind = "max_concurrent_requests"\nscope = "model"\nlimit = 4\n'
                "default = true\n"
            )
        )
        self.assertIn("cannot be a fallback", message)

    def test_a_default_that_is_not_a_boolean_is_refused(self):
        # Accepting "true" would make a quoting mistake look like a deliberate policy choice.
        for literal in ('"true"', "1", '"1"'):
            with self.subTest(literal=literal):
                message = self.assertRefused(
                    provider=self.rule(
                        '\n[[rule]]\nkind = "max_concurrent_requests"\nscope = "provider"\n'
                        f"limit = 2\ndefault = {literal}\n"
                    )
                )
                self.assertIn("must be a boolean", message)

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
        self.assertEqual([item["id"] for item in models], ["fixture/fixture_model"])
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
        providers, models = self.load()
        models[0]["credential"] = "nope"
        with self.assertRaises(ValueError):
            policy.resolve(
                providers,
                {item["id"]: item for item in models},
                {"primary": "fixture/fixture_model", "subagent": "fixture/fixture_model"},
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

    def test_a_model_may_name_the_variable_holding_its_own_key(self):
        scoped = MODEL.replace(
            'credential = "default"',
            'credential = "default"\nkey_env = "FIXTURE_MODEL_API_KEY"',
        )
        _, models = self.load(models={"fixture_model": scoped})
        self.assertEqual(models[0]["key_env"], "FIXTURE_MODEL_API_KEY")

    def test_a_model_without_one_authenticates_with_its_providers_credential(self):
        _, models = self.load()
        self.assertEqual(models[0]["key_env"], "")

    def test_a_model_key_variable_must_look_like_one(self):
        for name, value in (("lowercase", "fixture_key"), ("a spaced value", "BAD NAME")):
            with self.subTest(case=name):
                self.assertRefused(
                    models={
                        "fixture_model": MODEL.replace(
                            'credential = "default"',
                            f'credential = "default"\nkey_env = "{value}"',
                        )
                    }
                )

    def test_a_model_cannot_reuse_a_variable_its_provider_already_names(self):
        # Two spellings of one variable would make the fallback indistinguishable from the
        # model-scoped choice, which is the distinction the whole mechanism rests on.
        provider = PROVIDER.replace(
            'label = "Fixture API token"', 'label = "Fixture API token"\nenv = "FIXTURE_API_KEY"'
        )
        message = self.assertRefused(
            provider=provider,
            models={
                "fixture_model": MODEL.replace(
                    'credential = "default"',
                    'credential = "default"\nkey_env = "FIXTURE_API_KEY"',
                )
            },
        )
        self.assertIn("key_env", message)


class AgentLaneTests(DefinitionFixture):
    """Which window the main agent launches in, and what refuses to supply one.

    This is the only lane choice a launch makes rather than a request making, so the interesting
    half of the contract is the refusals: a designation the model cannot serve has to stop the
    launch instead of quietly becoming the native window.
    """

    LONG = (
        "\n[lane.long]\ncontext_tokens = 16384\n"
        "input_tokens = 12288\noutput_clamp_tokens = 4096\n"
    )

    def resolve(self, model_text: str = MODEL, **kwargs) -> dict:
        # The shipped fixture budgets 35% of its window, which none of its lanes could ever fit,
        # so the rule is widened here the way the other resolving cases widen it: this class is
        # about which lane a launch designates, not about what an impossible budget does.
        provider = PROVIDER.replace("percent = 35", "percent = 100")
        providers, parsed = definitions.load_definitions(
            self.tree(provider=provider, models={"fixture_model": model_text})
        )
        return policy.resolve(
            providers,
            {item["id"]: item for item in parsed},
            {"primary": "fixture/fixture_model", "subagent": "fixture/fixture_model"},
            reserved_context_size=1024,
            **kwargs,
        )

    def test_a_plan_designates_a_lane_and_every_reader_sees_the_same_one(self):
        for lane in ("primary", "long"):
            with self.subTest(lane=lane):
                plan = self.resolve(MODEL + self.LONG, agent_lane=lane)
                self.assertEqual(plan["agent_lane"], lane)
                self.assertIn(lane, plan["lanes"])

    def test_the_designation_defaults_to_the_wide_queued_window(self):
        # An omitted argument is what a caller that has no panel passes, and the shipped default
        # is the wider lane: under the utilisation mandate a bigger window beats the in-flight
        # headroom a smaller lane would have left. A caller that must not pay for the wide
        # reservation passes agent_lane="primary" explicitly.
        self.assertEqual(self.resolve(MODEL + self.LONG)["agent_lane"], "long")

    def test_a_model_without_a_long_lane_cannot_be_launched_on_one(self):
        # The window can be withdrawn from a definition while a saved preference still names it,
        # and the resolver is where that stops: silently serving the native window would let a
        # launch believe it had the bigger one.
        with self.assertRaises(policy.ResolutionError) as caught:
            self.resolve(agent_lane="long")
        self.assertIn("not available", str(caught.exception))

    def test_a_lane_that_is_not_a_window_a_main_agent_can_use_is_refused(self):
        # Naming the subagent lane here would put unbounded primary traffic on a route metered for
        # small bounded children.
        for lane in ("subagent", "", "Primary", "nowhere"):
            with self.subTest(lane=lane), self.assertRaises(policy.ResolutionError) as caught:
                self.resolve(MODEL + self.LONG, agent_lane=lane)
            self.assertIn("not selectable", str(caught.exception))

    def test_the_designed_lane_carries_the_alias_and_size_the_renderer_will_publish(self):
        plan = self.resolve(MODEL + self.LONG, agent_lane="long")
        lane = plan["lanes"][plan["agent_lane"]]
        self.assertEqual(lane["route"], "/long/v1")
        self.assertGreater(lane["context_tokens"], plan["lanes"]["primary"]["context_tokens"])

    def test_the_window_choice_leaves_the_routes_and_the_counters_alone(self):
        # Designating a lane is not the same as opening one: both routes exist either way, and the
        # provider's aggregate budget is metered across all of them regardless of the preference.
        for lane in ("primary", "long"):
            with self.subTest(lane=lane):
                plan = self.resolve(MODEL + self.LONG, agent_lane=lane)
                self.assertEqual(sorted(plan["lanes"]), ["long", "primary", "subagent"])
                self.assertEqual(
                    sorted(plan["selection"]), sorted(["primary", "subagent"])
                )


class KeyScopeCounterTests(DefinitionFixture):
    """A counter must follow the key a lane actually authenticates with.

    Two model definitions may share one wire model and still hold two different keys, and
    upstream those are two ledgers. Collapsing them by wire name alone would meter one
    tenant's traffic against another's quota.
    """

    def resolve(self, values: dict[str, str]) -> dict:
        provider = PROVIDER.replace(
            'label = "Fixture API token"',
            'label = "Fixture API token"\nenv = "FIXTURE_API_KEY"',
        )
        # The shipped fixture budgets 35% of its window, which no lane of it could ever fit;
        # this case needs a pair that resolves, so the rule is the whole window.
        provider = provider.replace("percent = 35", "percent = 100")
        models = {
            "a_model": MODEL.replace(
                'label = "Fixture Model"\n',
                'label = "A Model"\n',
            ).replace('slug = "fixturemodel"', 'slug = "am"\nkey_env = "AMA_API_KEY"'),
            "b_model": MODEL.replace(
                'label = "Fixture Model"\n',
                'label = "B Model"\n',
            ).replace('slug = "fixturemodel"', 'slug = "bm"\nkey_env = "AMB_API_KEY"'),
        }
        providers, parsed = definitions.load_definitions(
            self.tree(provider=provider, models=models)
        )
        return policy.resolve(
            providers,
            {item["id"]: item for item in parsed},
            {"primary": "fixture/a_model", "subagent": "fixture/b_model"},
            reserved_context_size=1024,
            key_values=values,
            # These fixtures declare no long lane, and this class is about key scoping.
            agent_lane="primary",
        )

    def ledgers(self, plan: dict) -> dict[str, list[str]]:
        return {
            counter["subject"]: counter["lanes"]
            for counter in plan["counters"].values()
            if counter["family"] == "rate"
        }

    def test_two_keys_on_one_wire_model_are_metered_as_two_ledgers(self):
        plan = self.resolve({"AMA_API_KEY": "own-key", "FIXTURE_API_KEY": "shared-key"})
        self.assertEqual(
            self.ledgers(plan),
            {
                "fixture/default__am/fixture-model": ["primary"],
                "fixture/default/fixture-model": ["subagent"],
            },
            "the model with its own key must not share a ledger with the one that fell back",
        )
        context = next(c for c in plan["counters"].values() if c["family"] == "context")
        self.assertEqual(
            context["lanes"],
            ["primary", "subagent"],
            "the provider publishes the context rule per model, so both lanes still contend "
            "for it whatever key each one uses",
        )

    def test_one_shared_key_still_meters_both_models_together(self):
        plan = self.resolve({"FIXTURE_API_KEY": "shared-key"})
        self.assertEqual(
            self.ledgers(plan), {"fixture/default/fixture-model": ["primary", "subagent"]}
        )

    def test_two_models_without_any_key_are_asked_for_separately(self):
        # Nothing is set in either scope, so ./start.sh must prompt once per selected model,
        # and a key typed for one of them may not land in the file the other reads.
        import models as model_setup

        items = model_setup.used_credentials(self.resolve({}))
        self.assertEqual(
            [(item["secret"], item["env"], item["label"]) for item in items],
            [
                ("fixture__default__am", "AMA_API_KEY", "A Model API key"),
                ("fixture__default__bm", "AMB_API_KEY", "B Model API key"),
            ],
        )
        self.assertEqual(
            model_setup.key_scopes(items[0]),
            "AMA_API_KEY for this model alone, or FIXTURE_API_KEY for every model "
            "of Fixture Provider",
        )


class TreeHygieneTests(DefinitionFixture):
    def test_a_directory_without_a_manifest_is_ignored(self):
        root = self.tree()
        (root / "models" / "notes").mkdir()
        (root / "models" / "notes" / "README.md").write_text("work in progress\n")
        _, models = definitions.load_definitions(root)
        self.assertEqual([item["id"] for item in models], ["fixture/fixture_model"])

    def test_an_uppercase_or_spaced_directory_is_refused(self):
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
        (root / "models" / "fixture" / "fixture_model" / "model.toml").unlink()
        (root / "models" / "fixture" / "fixture_model" / "model.toml").symlink_to(target)
        with self.assertRaisesRegex(definitions.DefinitionError, "Unsafe model definition"):
            definitions.load_definitions(root)

    def test_a_symlink_inside_a_tree_is_refused(self):
        root = self.tree()
        (root / "models" / "fixture" / "fixture_model" / "link").symlink_to(root / "providers")
        with self.assertRaisesRegex(definitions.DefinitionError, "Unsafe"):
            definitions.load_definitions(root)

    def test_invalid_toml_is_refused_with_a_readable_message(self):
        message = self.assertRefused(models={"fixture_model": "this is not toml"})
        self.assertIn("not valid TOML", message)


#: A provider that serves models on the machine the harness is launching on, and one of its
#: models with weights to install. ``requires`` is what lets the picker hide them from a host
#: without Metal; ``[install]`` is what gives ``Ready?`` something to report on.
LOCAL_PROVIDER = """
schema_version = 1
label = "Local (Fixture)"
requires = ["metal"]

[endpoint]
base_url = "http://host.docker.internal:8181"
base_url_env = "LOCAL_FIXTURE_BASE_URL"
protocol = "openai"
sends_cache_salt = false

[[credential]]
id = "default"
label = "Local engine token"
env = "LOCAL_FIXTURE_API_KEY"
generate = true

[[rule]]
kind = "max_concurrent_requests"
scope = "model"
limit = 2
"""

LOCAL_MODEL = MODEL.replace('provider = "fixture"', 'provider = "local_fixture"') + """
[requires]
memory_gib = 32
disk_gib = 40

[install]
variant = "UD-Q4_K_XL"

[install.sampling]
temperature = 1.0
top_p = 0.95
top_k = 20
"""

#: The shape is what discovery checks; :mod:`local_llm` is what reads the digests inside it.
#: A complete, valid schema-1 lock. It used to be an empty stub because only the digest of the file
#: mattered to the resolver; ``prepare()`` now parses it through an adapter, so a fixture that will
#: not survive ``read_lock`` describes a tree no launcher could install from.
#: Tests that need a *broken* lock pass one explicitly via ``local_tree(lock=...)``.
_LOCAL_REVISION = "0" * 40
_LOCAL_DIGEST = "a" * 64
LOCAL_LOCK = (
    '{"schema_version": 1, '
    f'"source": {{"repo": "fixture/Model-GGUF", "revision": "{_LOCAL_REVISION}"}}, '
    f'"files": [{{"path": "fixture-Q4.gguf", "sha256": "{_LOCAL_DIGEST}", "size": 4096}}]}}\n'
)


def local_tree(
    root: Path,
    *,
    provider: str = LOCAL_PROVIDER,
    model: str = LOCAL_MODEL,
    lock: str | None = LOCAL_LOCK,
) -> Path:
    """A local-serving provider, one model with weights to install, and its lock file."""
    root = write_tree(root, provider, {"local_model": model}, provider_id="local_fixture")
    if lock is not None:
        model_dir(root).joinpath("weights.lock.json").write_text(lock)
    return root


def model_dir(root: Path) -> Path:
    return root / "models" / "local_fixture" / "local_model"


class UnknownKeyTests(DefinitionFixture):
    """Every table refuses a key it does not define, instead of ignoring it.

    Manifests are edited by hand, so a misspelt limit or option is the likeliest real defect an
    operator can introduce. Ignoring the extra key would run the provider or the model on a default
    nobody agreed to, which is why each table closes its own key set -- and none of these guards was
    executed by any test before this class existed.

    Each expectation names the FULL table path, not just "has unknown key". The first draft of this
    class asserted the bare phrase and passed while testing the wrong guard: appending a key at the
    end of a manifest puts it in the LAST table, so the model-root refusal never fired and a lane
    guard answered instead.
    """

    ROOT = "models/fixture/fixture_model"

    def test_a_remote_table_refuses_a_key_it_does_not_define(self):
        cases = (
            ("endpoint",
             PROVIDER.replace('protocol = "openai"', 'protocol = "openai"\nstray = 1'),
             None,
             r"providers/fixture\.endpoint has unknown key 'stray'"),
            ("model root", None,
             MODEL.replace('provider = "fixture"', 'provider = "fixture"\nstray = 1', 1),
             rf"{self.ROOT} has unknown key 'stray'"),
            ("context", None,
             MODEL.replace("[context]\n", "[context]\nstray = 1\n"),
             rf"{self.ROOT}\.context has unknown key 'stray'"),
            ("lane.primary", None,
             MODEL.replace("[lane.primary]\n", "[lane.primary]\nstray = 1\n"),
             rf"{self.ROOT}\.lane\.primary has unknown key 'stray'"),
        )
        for label, provider, model, refused in cases:
            with self.subTest(table=label):
                with self.assertRaisesRegex(definitions.DefinitionError, refused):
                    self.load(provider=provider or PROVIDER,
                              models={"fixture_model": model or MODEL})

    def test_the_provider_root_and_its_repeated_tables_close_their_keys(self):
        # Same rule applied to the tables a provider repeats: a stray key inside [[credential]] or
        # [[rule]] would otherwise ride along into the resolved plan unnoticed, and the two messages
        # differ ("unknown key 'x'" versus "unknown key(s): x"), so each is asserted in full.
        root = PROVIDER.replace(
            'policy_url = "https://fixture.invalid/policy"',
            'policy_url = "https://fixture.invalid/policy"\nstray = 1',
            1,
        )
        with self.subTest(table="provider root"):
            with self.assertRaisesRegex(
                definitions.DefinitionError, r"providers/fixture has unknown key 'stray'"
            ):
                self.load(provider=root)
        credential = PROVIDER.replace(
            'label = "Fixture API token"', 'label = "Fixture API token"\nstray = 1', 1
        )
        with self.subTest(table="credential"):
            with self.assertRaisesRegex(
                definitions.DefinitionError,
                r"providers/fixture\.credential\[0\] has unknown key 'stray'",
            ):
                self.load(provider=credential)
        rule = PROVIDER.replace("percent = 35", "percent = 35\nstray = 1", 1)
        with self.subTest(table="rule"):
            with self.assertRaisesRegex(
                definitions.DefinitionError,
                r"providers/fixture\.rule\[0\] has unknown key\(s\): stray",
            ):
                self.load(provider=rule)

    def test_a_local_serving_table_refuses_a_key_it_does_not_define(self):
        local = "models/local_fixture/local_model"
        cases = (
            ("requires", LOCAL_MODEL.replace("[requires]\n", "[requires]\nstray = 1\n"),
             rf"{local}\.requires has unknown key 'stray'"),
            ("install", LOCAL_MODEL.replace("[install]\n", "[install]\nstray = 1\n"),
             rf"{local}\.install has unknown key 'stray'"),
            ("install.sampling",
             LOCAL_MODEL.replace("[install.sampling]\n", "[install.sampling]\nstray = 1\n"),
             rf"{local}\.install\.sampling has unknown key 'stray'"),
        )
        for label, text, refused in cases:
            with self.subTest(table=label):
                root = local_tree(self.tree_base(), model=text)
                with self.assertRaisesRegex(definitions.DefinitionError, refused):
                    definitions.load_definitions(root)



#: A version-3 provider: named serving profiles, an explicit memory policy, and a host capacity
#: table. Nothing in this file could build one before, which is why the whole v3 branch set --
#: profile identity, engine choice, memory policy, capacity, and the model's [serving] binding --
#: was never executed by a test, even though every shipped provider is version 3.
V3_PROVIDER = """\

schema_version = 3
label = "Local (Fixture)"
requires = ["metal"]

[endpoint]
base_url = "http://host.docker.internal:8181"
base_url_env = "LOCAL_FIXTURE_BASE_URL"
protocol = "openai"
sends_cache_salt = false

[[credential]]
id = "default"
label = "Local engine token"
env = "LOCAL_FIXTURE_API_KEY"
generate = true

[[rule]]
kind = "max_concurrent_requests"
scope = "model"
limit = 2

[execution]
kind = "local"
[execution.profiles.llama_cpp_q4]
engine = "llama_cpp"
context_granularity = 256
max_sequences = 256
batch_tokens = 2048
microbatch_tokens = 512
cache_type_k = "f16"
cache_type_v = "f16"
flash_attention = "auto"
load_mode = "auto"
lazy_mode = "auto"
gpu_layers = "all"
kv_unified = false
cache_ram_mib = 0
context_checkpoints = 0
[execution.profiles.mlx_lm]
engine = "mlx_lm"
context_granularity = 256
max_sequences = 1
prefill_chunk_tokens = 4096
allocator_cache_bytes = 0
kv_allocation = "growing"
prefix_cache = "none"
prefill_chunk_candidates = [1024, 4096]

[execution.memory]
strategy = "uncontrolled"
target = "none"
distribution = "equal"
unknown_measurements = "unwired"
[capacity]
strategy = "native_context"
sequence_slots = 4
"""

V3_MODEL = """\

schema_version = 3
label = "Fixture Model"
provider = "local_fixture"
model = "fixture-model"
slug = "fixturemodel"
credential = "default"

[context]
native_tokens = 16384

[lane.primary]
allocation = "engine"
output_mode = "remaining_context"

[lane.subagent]
allocation = "engine"
output_mode = "remaining_context"

[serving]
profile = "llama_cpp_q4"

[requires]
memory_gib = 32
disk_gib = 40

[install]
variant = "UD-Q4_K_XL"

[install.sampling]
temperature = 1.0
top_p = 0.95
top_k = 20
"""


class VersionThreeTests(DefinitionFixture):
    """The version-3 manifest rules, which decide the engine by declaration and never by ordering.

    AGENTS.md is explicit that engine selection is a manifest fact and that a provider offering
    several profiles declares no default. Each case below is one line away from a valid tree, so
    every refusal names the rule it guards.
    """

    def load_v3(self, *, provider=V3_PROVIDER, model=V3_MODEL):
        root = local_tree(self.tree_base(), provider=provider, model=model)
        return definitions.load_definitions(root)

    def test_the_plain_v3_tree_loads(self):
        # Positive control for the two cases below: without it, a refusal could come from the
        # fixture rather than from the rule under test.
        providers, models = self.load_v3()
        self.assertEqual(
            sorted(providers["local_fixture"]["execution"]["profiles"]),
            ["llama_cpp_q4", "mlx_lm"],
        )
        # A v3 provider declares no default: choosing by dictionary order would pick a runtime from
        # a key nobody wrote down.
        self.assertIsNone(providers["local_fixture"]["execution"].get("engine"))
        self.assertEqual(models[0]["serving"], {"profile": "llama_cpp_q4"})

    def test_each_v3_manifest_rule_refuses_one_bad_line(self):
        cases = (
            ("execution unknown key",
             'kind = "local"', 'kind = "local"\nstray = 1',
             r"\.execution has unknown key\(s\)"),
            ("v2 style kind not local", 'kind = "local"', 'kind = "remote"',
             "must select kind = local"),
            ("profile id not lowercase",
             "[execution.profiles.llama_cpp_q4]", "[execution.profiles.LLAMA]",
             "must be a lowercase identifier"),
            ("engine with no settings table",
             'engine = "llama_cpp"', 'engine = "vllm"',
             r"\.engine must be one of"),
            ("profile missing a setting",
             "batch_tokens = 2048\n", "",
             "must declare exactly"),
            ("microbatch above batch",
             "microbatch_tokens = 512", "microbatch_tokens = 4096",
             "microbatch_tokens exceeds"),
            # Measured, not assumed: the four `integer` keys go through _positive_int, and the
            # non-negative branch belongs to `nonnegative` alone -- mlx's allocator_cache_bytes,
            # where zero means "retain no free device buffers" and is a real value.
            ("integer holds a string", "batch_tokens = 2048", 'batch_tokens = "2048"',
             "batch_tokens must be a positive integer"),
            ("non-negative holds a bool", "allocator_cache_bytes = 0",
             "allocator_cache_bytes = true",
             "allocator_cache_bytes must be a non-negative integer"),
            ("max_sequences above microbatch", "max_sequences = 256", "max_sequences = 1024",
             "max_sequences exceeds microbatch_tokens"),
            ("choice outside its set", 'cache_type_k = "f16"', 'cache_type_k = "f32"',
             "cache_type_k must be one of f16"),
            ("fixed value overridden", "kv_unified = false", "kv_unified = true",
             "kv_unified must be False"),
            ("chunk outside its candidates", "prefill_chunk_tokens = 4096",
             "prefill_chunk_tokens = 8192",
             "prefill_chunk_tokens must be one of the declared"),
            # An empty candidate list cannot satisfy the membership check above it, so that guard
            # is what answers here -- the "must list at least one" branch is unreachable, and this
            # case records which one really fires.
            ("empty candidate list", "prefill_chunk_candidates = [1024, 4096]",
             "prefill_chunk_candidates = []",
             "must be one of the declared"),
            ("non-positive candidate", "prefill_chunk_candidates = [1024, 4096]",
             "prefill_chunk_candidates = [0, 4096]",
             "holds a non-positive or repeated value"),
            ("repeated candidate", "prefill_chunk_candidates = [1024, 4096]",
             "prefill_chunk_candidates = [4096, 4096]",
             "lists 4096 twice"),
            ("capacity status unknown", "sequence_slots = 4",
             'sequence_slots = 4\nvalidation_status = "bogus"', "validation_status is unknown"),
            ("credential id not lowercase", 'id = "default"', 'id = "DEFAULT"',
             "credential\\[0\\].id must be a lowercase identifier"),
            # A scope outside the vocabulary, and a rule that is not a table at all: both are legal
            # TOML, and both would otherwise reach the field reads inside the rule loop.
            ("rule scope is unknown", 'scope = "model"', 'scope = "cluster"',
             r"rule\[0\]\.scope 'cluster' is unknown"),
            # A local provider that names no host capability cannot be gated at all: `[execution]`
            # says an engine runs on this machine, and `requires` is what makes the launcher ask
            # whether this machine can. Both safety rows insert ahead of [capacity], the last table,
            # because a header added at the end would re-parent the capacity keys instead.
            ("execution with no host requirement", 'requires = ["metal"]', 'requires = []',
             "needs a measured host capability"),
            # `[[endpoint]]` is an array of one table and not the table the resolver reads, and
            # `requires = [1]` is a list of the wrong thing -- both legal TOML, both arriving at a
            # field read that assumes otherwise.
            ("endpoint is not a table", "[endpoint]", "[[endpoint]]",
             "must declare an \\[endpoint\\] table"),
            ("requires is not a list of names", 'requires = ["metal"]', "requires = [1]",
             "must be a list of host capabilit"),
            ("safety is not a table", "[capacity]", "[[safety]]\n\n[capacity]",
             r"\.safety must be a table"),
            ("safety unknown key", "[capacity]", '[safety]\nbogus = 1\n\n[capacity]',
             r"\.safety has unknown key"),
            ("memory strategy unknown",
             'strategy = "uncontrolled"', 'strategy = "auto"',
             "strategy must be bounded_wiring or uncontrolled"),
            ("bounded wiring with no target",
             'strategy = "uncontrolled"', 'strategy = "bounded_wiring"',
             "bounds wiring but names no target"),
            ("memory target unknown", 'target = "none"', 'target = "bogus"',
             "target is unknown"),
            ("memory distribution unknown", 'distribution = "equal"',
             'distribution = "bogus"', "distribution is unknown"),
            ("memory unknown_measurements invalid", 'unknown_measurements = "unwired"',
             'unknown_measurements = "guess"', "must be unwired or refuse"),
            ("capacity unknown key",
             "sequence_slots = 4", "sequence_slots = 4\nstray = 1",
             r"\.capacity has unknown key\(s\)"),
            ("capacity strategy wrong",
             'strategy = "native_context"', 'strategy = "fixed"',
             "capacity.strategy must be native_context"),
            # [[capacity]] arrives as a list of one table rather than as a table, which is the shape
            # guard above every capacity field check.
            ("capacity is not a table", "[capacity]", "[[capacity]]",
             "must declare the host allocation policy"),
            # Each of these stays inside its own table: the replaced line is a key of the table the
            # mutant adds a key to, which is why a stray key can be one edit here but not at the end
            # of the document, where it would be absorbed by whichever table was named last.
            ("memory unknown key", 'target = "none"', 'target = "none"\nstray = 1',
             r"execution\.memory has unknown key\(s\)"),
            ("credential env not upper snake", 'env = "LOCAL_FIXTURE_API_KEY"',
             'env = "local_fixture_api_key"', r"credential\[0\]\.env must be an UPPER_SNAKE"),
            ("credential key_url not https", 'env = "LOCAL_FIXTURE_API_KEY"',
             'env = "LOCAL_FIXTURE_API_KEY"\nkey_url = "http://dashboard.invalid/key"',
             r"credential\[0\]\.key_url must be an https URL"),
            ("generated credential names no env", 'env = "LOCAL_FIXTURE_API_KEY"\ngenerate = true',
             "generate = true", "generates its own value but names no env variable"),
            ("validated without evidence",
             "sequence_slots = 4", 'sequence_slots = 4\nvalidation_status = "validated"',
             "cannot claim validated"),
        )
        for label, find, replace, refused in cases:
            with self.subTest(rule=label):
                self.assertIn(find, V3_PROVIDER, f"fixture no longer contains {find!r}")
                broken = V3_PROVIDER.replace(find, replace, 1)
                self.assertNotEqual(broken, V3_PROVIDER)
                with self.assertRaisesRegex(definitions.DefinitionError, refused):
                    self.load_v3(provider=broken)

    def test_each_v3_model_rule_refuses_one_bad_line(self):
        # The provider table above cannot carry these: a model manifest is where [requires],
        # [install], [install.sampling], [context] and the lane tables live, and each refusal below
        # guards a field the resolver reads without re-checking.
        cases = (
            ("requires is not a table", "[requires]", "[[requires]]",
             r"\.requires must be a table"),
            ("requires unknown key", "memory_gib = 32", "memory_gib = 32\nstray = 1",
             r"\.requires has unknown key"),
            ("install unknown key", 'variant = "UD-Q4_K_XL"',
             'variant = "UD-Q4_K_XL"\nstray = 1', r"\.install has unknown key"),
            ("install variant is not a bare word", 'variant = "UD-Q4_K_XL"',
             'variant = "two words"', "variant must be a bare word"),
            ("install sampling is not a table", "[install.sampling]", "[[install.sampling]]",
             r"\.install\.sampling must be a table"),
            ("install sampling negative", "top_k = 20", "top_k = -1",
             r"sampling\.top_k must be a non-negative number"),
            ("install port out of range", 'variant = "UD-Q4_K_XL"',
             'variant = "UD-Q4_K_XL"\nport = 70000', "install.port must be a usable TCP port"),
            # Read by the shared integer helper before the range check above it, so a negative port
            # is refused for being negative rather than for being out of range.
            ("install port must not be negative", 'variant = "UD-Q4_K_XL"',
             'variant = "UD-Q4_K_XL"\nport = -1', "port must be a non-negative integer"),
            ("lane effort must be a bare word", 'output_mode = "remaining_context"',
             'output_mode = "remaining_context"\ndefault_effort = "Medium"',
             "default_effort must be a bare lowercase word"),
            ("credential id must be lowercase", 'credential = "default"',
             'credential = "DEFAULT"', "credential must be a lowercase credential identifier"),
            # The same field at the two levels it appears: the lane reads it through the same helper
            # the model now does, and before that only the lane had any check at all.
            ("model effort must be a bare word", 'slug = "fixturemodel"',
             'slug = "fixturemodel"\ndefault_effort = "Medium"',
             r"\.default_effort must be a bare lowercase word"),
            ("model effort must not be a number", 'slug = "fixturemodel"',
             'slug = "fixturemodel"\ndefault_effort = 3',
             r"\.default_effort must be a bare lowercase word"),
            ("lane must take its window from the engine", 'allocation = "engine"',
             'allocation = "fixed"', "must use allocation = engine and output_mode"),
            ("context is not a table", "[context]", "[[context]]", r"\.context must be a table"),
            ("context has no native_tokens", "native_tokens = 16384", "",
             "native_tokens must be declared"),
            ("lane is not a table", "[lane.primary]", "[[lane.primary]]",
             r"\.lane\.primary must be a table"),
        )
        for label, find, replace, refused in cases:
            with self.subTest(rule=label):
                self.assertIn(find, V3_MODEL, f"fixture no longer contains {find!r}")
                broken = V3_MODEL.replace(find, replace, 1)
                self.assertNotEqual(broken, V3_MODEL)
                with self.assertRaisesRegex(definitions.DefinitionError, refused):
                    self.load_v3(model=broken)

    def test_a_bare_lowercase_effort_still_loads_at_both_levels(self):
        # Control for tightening the effort check. Every shipped manifest spells it in lowercase
        # ("xhigh", "low"), so a refusal there would be the worse bug -- and the lane value has to
        # survive as itself, not as the empty string that absence reads as.
        model = V3_MODEL.replace(
            'slug = "fixturemodel"', 'slug = "fixturemodel"\ndefault_effort = "xhigh"', 1
        ).replace(
            'allocation = "engine"', 'allocation = "engine"\ndefault_effort = "low"', 1
        )
        self.assertEqual(model.count("default_effort"), 2)
        _providers, models = self.load_v3(model=model)
        self.assertEqual(models[0]["default_effort"], "xhigh")
        self.assertEqual(models[0]["lanes"]["primary"]["default_effort"], "low")

    def test_a_v3_model_that_declares_no_lane_is_refused(self):
        # An engine-bound model with no lane table has nothing for the resolver to bind an alias to,
        # and dropping one lane rather than all of them is the more likely mistake: that names the
        # missing role instead.
        none = self.without_table(self.without_table(V3_MODEL, "[lane.primary]"), "[lane.subagent]")
        self.assertNotIn("[lane.", none)
        with self.assertRaisesRegex(
            definitions.DefinitionError, "must declare at least one \\[lane\\.\\*\\] table"
        ):
            self.load_v3(model=none)
        one = self.without_table(V3_MODEL, "[lane.subagent]")
        with self.assertRaisesRegex(
            definitions.DefinitionError, r"must declare \[lane\.subagent\] to be selectable"
        ):
            self.load_v3(model=one)

    def test_a_v3_provider_with_no_profile_is_refused(self):
        # Restores a case I dropped when splicing this class together: v3 has no default to fall
        # back on, so a provider that names an engine kind but declares no profile could not serve
        # anything, and that has to be a manifest error rather than a launch failure.
        head = V3_PROVIDER.split("[execution.profiles.")[0]
        tail = V3_PROVIDER[V3_PROVIDER.index("[execution.memory]"):]
        broken = head + tail
        self.assertNotIn("[execution.profiles.", broken)
        with self.assertRaisesRegex(
            definitions.DefinitionError, "must declare at least one"
        ):
            self.load_v3(provider=broken)

    @staticmethod
    def without_table(manifest: str, opener: str) -> str:
        """``manifest`` with one whole TOML table removed, from its header to the next one.

        These manifests are read as text and re-parsed, so dropping a *value* is a one-line
        replace while dropping a *table* is not: leaving its header behind would re-parent every
        key that followed it.
        """
        start = manifest.index(opener)
        rest = manifest[start + len(opener):]
        following = [i for i in (rest.find("\n[["), rest.find("\n[")) if i != -1]
        return manifest[:start] + rest[min(following, default=len(rest)):].lstrip("\n")

    def test_a_provider_with_no_credential_table_is_refused(self):
        # Nothing downstream can authenticate, so this belongs in the manifest error rather than
        # in a launch that has no token to read. The message has to name the missing table and not
        # one of the per-field checks below it, which never run.
        broken = self.without_table(V3_PROVIDER, "[[credential]]")
        self.assertNotIn("[[credential]]", broken)
        self.assertIn("[[rule]]", broken, "only the credential block was removed")
        with self.assertRaisesRegex(
            definitions.DefinitionError, r"must declare at least one \[\[credential\]\] table"
        ):
            self.load_v3(provider=broken)

    def test_an_unreadable_manifest_is_named_rather_than_raised_through(self):
        # ./providers is edited by hand and can be present-but-sealed (a mode, an odd mount, a file
        # still being written by an editor). The loader has to say which manifest it could not read;
        # an OSError escaping here reaches the launcher as a traceback with no file in it.
        real = Path.open

        def flaky(self, *args, **kwargs):
            if self.name == "provider.toml":
                raise PermissionError(13, "Permission denied", str(self))
            return real(self, *args, **kwargs)

        base = local_tree(self.tree_base(), provider=V3_PROVIDER, model=V3_MODEL)
        with mock.patch.object(Path, "open", flaky):
            with self.assertRaisesRegex(
                definitions.DefinitionError, "cannot read provider manifest"
            ):
                definitions.load_definitions(base)

    def test_a_credential_that_is_not_a_table_is_refused(self):
        # Same shape as the rule case: an array of scalars is legal TOML, and the loop reads fields
        # off each item. The [[credential]] form must go first, or the top-level key collides.
        broken = self.without_table(V3_PROVIDER, "[[credential]]").replace(
            'requires = ["metal"]', 'requires = ["metal"]\ncredential = [1]', 1
        )
        self.assertNotIn("[[credential]]", broken)
        self.assertIn("credential = [1]", broken)
        with self.assertRaisesRegex(
            definitions.DefinitionError, r"credential\[0\] must be a table"
        ):
            self.load_v3(provider=broken)

    def test_a_profile_that_is_not_a_table_is_refused(self):
        # An inline table lets a profile name carry a scalar, which the sub-table form cannot
        # produce; both named tables come out first so `profiles` is defined exactly once.
        broken = V3_PROVIDER
        for opener in ("[execution.profiles.llama_cpp_q4]", "[execution.profiles.mlx_lm]"):
            broken = self.without_table(broken, opener)
        broken = broken.replace('kind = "local"', 'kind = "local"\nprofiles = {bad = 1}', 1)
        self.assertNotIn("[execution.profiles.", broken)
        self.assertIn("profiles = {bad = 1}", broken)
        with self.assertRaisesRegex(definitions.DefinitionError, r"profiles\.bad must be a table"):
            self.load_v3(provider=broken)

    def test_a_memory_declaration_that_is_not_a_table_is_refused(self):
        # The scalar has to arrive where the table was, or TOML itself refuses the manifest for
        # defining `memory` twice and the shape guard under test never runs.
        broken = self.without_table(V3_PROVIDER, "[execution.memory]").replace(
            'kind = "local"', 'kind = "local"\nmemory = 1', 1
        )
        self.assertIn("memory = 1", broken)
        self.assertNotIn("[execution.memory]", broken)
        with self.assertRaisesRegex(
            definitions.DefinitionError, r"execution\.memory must be a table"
        ):
            self.load_v3(provider=broken)

    def test_a_rule_that_is_not_a_table_is_refused(self):
        # `rule = [1]` is legal TOML and holds no rule: the loop reads fields off each item, so the
        # shape has to be answered before any field check. The block form is removed first because a
        # top-level key cannot follow a table header.
        broken = self.without_table(V3_PROVIDER, "[[rule]]").replace(
            'requires = ["metal"]', 'requires = ["metal"]\nrule = [1]', 1
        )
        self.assertNotIn("[[rule]]", broken)
        self.assertIn("rule = [1]", broken)
        with self.assertRaisesRegex(definitions.DefinitionError, r"rule\[0\] must be a table"):
            self.load_v3(provider=broken)

    def test_a_model_on_a_local_provider_must_declare_what_it_installs(self):
        # [install] is what pins the weights, the port, and anything "Ready" may claim. The sampling
        # table has to go first: leaving it behind re-creates the parent table implicitly, and the
        # loader would then see an install block that is present but empty rather than missing.
        broken = self.without_table(self.without_table(V3_MODEL, "[install.sampling]"), "[install]")
        self.assertNotIn("[install", broken)
        with self.assertRaisesRegex(
            definitions.DefinitionError, "declares no \\[install\\] block"
        ):
            self.load_v3(model=broken)

    def test_a_model_and_its_provider_must_agree_on_being_local(self):
        # A v1 model on an engine-backed provider would be served by a profile nothing bound to it,
        # and the converse would demand weights no remote gateway has a download step for. The
        # version and the provider's [execution] are one decision, not two.
        with self.assertRaisesRegex(
            definitions.DefinitionError, "must agree on engine-derived local capacity"
        ):
            self.load_v3(model=MODEL.replace('provider = "fixture"', 'provider = "local_fixture"'))

    def test_an_install_block_that_is_not_a_table_is_refused(self):
        # [[install]] is an array of one table. The sampling table has to be lifted out first, since
        # [install.sampling] beneath an array parent is not something TOML can express at all -- and
        # a manifest that fails to parse is not evidence that the shape guard works.
        broken = self.without_table(V3_MODEL, "[install.sampling]").replace(
            "[install]", "[[install]]", 1
        )
        self.assertNotIn("[install.sampling]", broken)
        self.assertIn("[[install]]", broken)
        with self.assertRaisesRegex(definitions.DefinitionError, r"\.install must be a table"):
            self.load_v3(model=broken)

    def serving_model(self, port: int, slug: str) -> str:
        return (
            V3_MODEL.replace('variant = "UD-Q4_K_XL"', f'variant = "UD-Q4_K_XL"\nport = {port}', 1)
            .replace('model = "fixture-model"', f'model = "fixture-{slug}"', 1)
            .replace('slug = "fixturemodel"', f'slug = "{slug}"', 1)
        )

    def test_two_models_on_one_provider_may_not_share_an_engine_port(self):
        # One engine process per model, so two models naming one port is a definition error the
        # loader can see and a bind failure it cannot: by the time anything tries to listen, the
        # manifest has already been accepted and shipped.
        def tree(first: int, second: int) -> Path:
            base = write_tree(
                self.tree_base(),
                V3_PROVIDER,
                {
                    "first": self.serving_model(first, "fixturefirst"),
                    "second": self.serving_model(second, "fixturesecond"),
                },
                provider_id="local_fixture",
            )
            for name in ("first", "second"):
                (base / "models" / "local_fixture" / name / "weights.lock.json").write_text(
                    LOCAL_LOCK
                )
            return base

        _providers, models = definitions.load_definitions(tree(18183, 18184))
        self.assertEqual(len(models), 2, "distinct ports are two serveable models")
        with self.assertRaisesRegex(
            definitions.DefinitionError, "both serve on port 18183; one engine per port"
        ):
            definitions.load_definitions(tree(18183, 18183))

    def test_a_v3_model_must_bind_one_of_its_provider_profiles(self):
        cases = (
            ("no serving table",
             lambda s: s.split("[serving]")[0].rstrip() + "\n",
             r"must declare \[serving\] profile"),
            ("serving carries a stray key",
             lambda s: s.replace('profile = "llama_cpp_q4"',
                                 'profile = "llama_cpp_q4"\nstray = 1'),
             r"\.serving has unknown key"),
            ("profile the provider does not declare",
             lambda s: s.replace('profile = "llama_cpp_q4"', 'profile = "q8_0"'),
             r"serving\.profile is 'q8_0', which providers/local_fixture does not declare"),
        )
        for label, mutate, refused in cases:
            with self.subTest(rule=label):
                broken = mutate(V3_MODEL)
                self.assertNotEqual(broken, V3_MODEL)
                with self.assertRaisesRegex(definitions.DefinitionError, refused):
                    self.load_v3(model=broken)

class LocalServingTests(DefinitionFixture):
    """The keys that make one model serveable by two providers, and what they may not do.

    ``[requires]`` and ``[install]`` describe bytes that run on the operator's own machine. On a
    model a remote gateway serves, they would be a gate no code can evaluate and a download no
    one performs - and a requirement nobody checks reads to the next reader as protection.
    """

    def test_a_local_provider_and_model_load(self):
        base = local_tree(self.tree_base())
        providers, models = definitions.load_definitions(base)
        provider = providers["local_fixture"]
        self.assertEqual(provider["requires"], ["metal"])
        self.assertFalse(provider["sends_cache_salt"])
        self.assertTrue(provider["credentials"]["default"]["generate"])
        model = models[0]
        self.assertEqual(model["where"], "models/local_fixture/local_model")
        self.assertEqual(model["requires"], {"memory_gib": 32, "disk_gib": 40})
        self.assertEqual(model["install"]["variant"], "UD-Q4_K_XL")
        self.assertEqual(model["install"]["lock"], "weights.lock.json")
        self.assertEqual(
            model["install"]["sampling"], {"temperature": 1.0, "top_p": 0.95, "top_k": 20.0}
        )
        self.assertEqual(model["weights_dir"], model_dir(base) / "weights")
        self.assertEqual(model["lock_path"], model_dir(base) / "weights.lock.json")

    def test_a_remote_model_declares_nothing_about_the_host(self):
        providers, models = definitions.load_definitions(self.tree())
        self.assertEqual(providers["fixture"]["requires"], [])
        self.assertTrue(providers["fixture"]["sends_cache_salt"])
        self.assertFalse(providers["fixture"]["credentials"]["default"]["generate"])
        self.assertEqual(models[0]["requires"], {"memory_gib": 0, "disk_gib": 0})
        self.assertIsNone(models[0]["install"])

    def test_a_remote_model_cannot_require_host_memory(self):
        base = self.tree_base()
        write_tree(base, models={"fixture_model": MODEL + "\n[requires]\nmemory_gib = 8\n"})
        with self.assertRaises(definitions.DefinitionError) as caught:
            definitions.load_definitions(base)
        self.assertIn("serves this model remotely", str(caught.exception))

    def test_a_remote_model_cannot_have_weights_to_install(self):
        base = self.tree_base()
        write_tree(base, models={"fixture_model": MODEL + '\n[install]\nvariant = "Q4"\n'})
        (base / "models" / "fixture" / "fixture_model" / "weights.lock.json").write_text(LOCAL_LOCK)
        with self.assertRaises(definitions.DefinitionError) as caught:
            definitions.load_definitions(base)
        self.assertIn("[install]", str(caught.exception))

    def test_install_needs_the_lock_that_pins_it(self):
        """A row that could never be verified must not be offered at all."""
        with self.assertRaises(definitions.DefinitionError) as caught:
            definitions.load_definitions(local_tree(self.tree_base(), lock=None))
        message = str(caught.exception)
        self.assertIn("weights.lock.json", message)
        self.assertIn("cannot be verified", message)

    def test_an_unknown_host_capability_is_refused(self):
        broken = LOCAL_PROVIDER.replace('requires = ["metal"]', 'requires = ["opencl"]')
        with self.assertRaisesRegex(definitions.DefinitionError, "opencl"):
            definitions.load_definitions(local_tree(self.tree_base(), provider=broken))

    def test_a_generated_credential_does_not_also_name_an_issuer(self):
        broken = LOCAL_PROVIDER.replace(
            'generate = true', 'generate = true\nkey_url = "https://fixture.invalid/keys"'
        )
        with self.assertRaisesRegex(definitions.DefinitionError, "one of those is a lie"):
            definitions.load_definitions(local_tree(self.tree_base(), provider=broken))

    def test_install_lock_must_be_a_name_not_a_path(self):
        broken = LOCAL_MODEL.replace(
            'variant = "UD-Q4_K_XL"', 'variant = "Q4"\nlock = "../evil.json"'
        )
        with self.assertRaisesRegex(definitions.DefinitionError, "not a path"):
            definitions.load_definitions(local_tree(self.tree_base(), model=broken))

    def test_weights_are_data_and_not_definition(self):
        """A symlink inside a downloaded weight set cannot fail a launch.

        The publisher chooses that tree's layout, and the HuggingFace cache is built out of
        symlinks. Exempting it from the walk is not exempting it from verification - only
        :mod:`local_llm` opens it, and it digests every file there.
        """
        base = local_tree(self.tree_base())
        weights = model_dir(base) / "weights"
        weights.mkdir()
        (weights / "blob").symlink_to(base / "providers")
        _, models = definitions.load_definitions(base)
        self.assertEqual([item["id"] for item in models], ["local_fixture/local_model"])
        # ...and one directory up, the same object is still refused.
        (model_dir(base) / "link").symlink_to(base / "providers")
        with self.assertRaisesRegex(definitions.DefinitionError, "Unsafe model definition"):
            definitions.load_definitions(base)


class DefinitionTreeLayoutTests(DefinitionFixture):
    """Where a model manifest must live, and what the two-level layout is owed.

    The provider directory is not decoration: it is what makes ``rm -rf models/<provider>/`` a
    complete removal, and what lets a provider the host cannot serve drop its models by itself.
    """

    def test_a_model_at_the_old_depth_is_refused_with_the_move(self):
        base = self.tree_base()
        write_tree(base)
        (base / "models" / "fixture" / "fixture_model").rename(base / "models" / "fixture_model")
        with self.assertRaises(definitions.DefinitionError) as caught:
            definitions.load_definitions(base)
        message = str(caught.exception)
        self.assertIn("one level too shallow", message)
        self.assertIn("models/<provider>/<model>/model.toml", message)

    def test_the_directory_must_agree_with_the_provider_key(self):
        """Built by hand, because :func:`write_tree` files by the key and cannot mis-file.

        The mismatch is the one thing the two-level layout can get wrong silently - a model read
        as belonging to provider A while its manifest says B - so it is refused rather than
        resolved by preferring either side.
        """
        base = self.tree_base()
        write_tree(base)
        misplaced = base / "models" / "fixture" / "misfiled"
        misplaced.mkdir(parents=True)
        misfiled_text = MODEL.replace('provider = "fixture"', 'provider = "elsewhere"')
        (misplaced / "model.toml").write_text(misfiled_text)
        with self.assertRaises(definitions.DefinitionError) as caught:
            definitions.load_definitions(base)
        self.assertIn("its directory says", str(caught.exception))

    def test_a_provider_the_host_cannot_serve_takes_its_models_with_it(self):
        """The one mechanism the eligibility filter reuses instead of a second hiding rule."""
        base = local_tree(self.tree_base())
        _, both = definitions.load_definitions(base)
        self.assertEqual(len(both), 1)
        providers, _ = definitions.load_definitions(base)
        del providers["local_fixture"]
        remaining = definitions.discover_models(base, providers)
        self.assertEqual(remaining, [])


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
