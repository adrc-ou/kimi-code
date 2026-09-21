"""Provider-policy enforcement, tested against the plan the launcher would mount.

The proxy no longer knows any model number, so nothing here may introduce one either. Every
fixture is built by resolving the shipped ``./models`` and ``./providers`` the same way
``./start.sh`` does, rendering the Kimi configuration from that plan, and letting the proxy
read both. Assertions are then made about the plan's own arithmetic, so a definition change
moves the expectations instead of breaking them.
"""

import asyncio
import contextlib
import copy
import importlib.util
import io
import json
import os
import sys
import tempfile
import time
import tomllib
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
# The repository root as well as tools/: unittest only puts the start directory on the path, not
# its parent, so `tests.helpers` needs the root added explicitly to resolve under every way of
# running the suite - `discover -s tests`, `python -m unittest tests.test_x`, and a bare
# `python -m unittest test_x` from inside tests/.
for _directory in (ROOT, ROOT / "tools"):
    if str(_directory) not in sys.path:
        sys.path.insert(0, str(_directory))


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # dataclasses resolves string annotations through sys.modules, so the module
    # has to be registered before it is executed.
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


from tests.helpers import shipped_plan  # noqa: E402

CONFIG_MERGE = load_module("kimi_config_merge", ROOT / "tools" / "kimi_config_merge.py")
RENDER = load_module("render_runtime", ROOT / "tools" / "render_runtime.py")

INTERNAL_TOKEN = "i" * 32
CACHE_SALT = "c" * 43
PROXY_TOKEN = "p" * 40
PROVIDER_KEY = "provider-fixture"

# The proxy reads its plan, its rendered configuration and its credentials at import time, so
# the fixture has to outlive every test in this module.
FIXTURES = tempfile.TemporaryDirectory()
FIXTURE_DIR = Path(FIXTURES.name)


PLAN = shipped_plan()


def write_file(name: str, text: str) -> str:
    """One file per call, in its own directory, so plan/config stamps never alias."""
    directory = Path(tempfile.mkdtemp(dir=FIXTURE_DIR))
    path = directory / name
    path.write_text(text, encoding="utf-8")
    return str(path)


def write_plan(plan: dict) -> str:
    return write_file("model-policy.json", json.dumps(plan))


def render_config(plan: dict, mutate=None) -> str:
    """The Kimi configuration this plan produces, optionally broken the way an agent might."""
    text = RENDER.kimi_config(ROOT, plan, PROXY_TOKEN)
    if mutate is not None:
        document = tomllib.loads(text)
        mutate(document)
        text = CONFIG_MERGE.emit(document)
    return write_file("kimi-config.toml", text)


SECRETS_DIR = FIXTURE_DIR / "secrets"
SECRETS_DIR.mkdir()
for _provider in PLAN["providers"].values():
    for _credential in _provider["credentials"].values():
        (SECRETS_DIR / _credential["secret_name"]).write_text(f"{PROVIDER_KEY}\n")

POLICY_PATH = write_plan(PLAN)
KIMI_CONFIG_PATH = render_config(PLAN)

os.environ.update(
    {
        "MODEL_PROXY_POLICY": POLICY_PATH,
        "KIMI_CONFIG_PATH": KIMI_CONFIG_PATH,
        "MODEL_PROXY_SECRETS_DIR": str(SECRETS_DIR),
        "MODEL_PROXY_INTERNAL_TOKEN": INTERNAL_TOKEN,
        "MODEL_PROXY_CACHE_SALT": CACHE_SALT,
    }
)

try:
    PROXY = load_module("model_proxy", ROOT / "proxy" / "model_proxy.py")
except ImportError as exc:
    # Without this, unittest discovery replaces all 131 tests in this module with a single
    # reported *error*, so a host missing aiohttp looks like a host with one broken test while
    # the whole suite has silently not run. A skip names the missing prerequisite instead.
    raise unittest.SkipTest(
        f"the proxy cannot be imported without its dependencies ({exc}); install what "
        "proxy/requirements.in pins to run these tests"
    ) from None

POLICY = PROXY.BASELINE_POLICY
LANES = POLICY.lanes
PRIMARY, LONG, SUBAGENT = LANES["primary"], LANES["long"], LANES["subagent"]
PRIMARY_RESERVATION = PRIMARY.reservation
SUBAGENT_RESERVATION = SUBAGENT.reservation
SECRET_NAMES = sorted({lane.secret_name for lane in LANES.values()})


def counters(family: str) -> list[dict]:
    return [item for item in POLICY.counters.values() if item.get("family") == family]


assert len(counters("context")) == 1 and len(counters("rate")) == 1, PLAN.keys()
CONTEXT_COUNTER = counters("context")[0]
RATE_COUNTER = counters("rate")[0]
CONTEXT_ID = CONTEXT_COUNTER["id"]
RATE_ID = RATE_COUNTER["id"]
BUDGET = CONTEXT_COUNTER["budget"]
CEILING = CONTEXT_COUNTER["ceiling"]
EXCLUSIVE_AT = CONTEXT_COUNTER["exclusive_at"]
MODEL_LIMIT = CONTEXT_COUNTER["max"]
FAN_OUT = POLICY.limits["subagent_concurrency"]
RATE_CAPACITY = RATE_COUNTER["capacity"]
RATE_UNIT = RATE_COUNTER["unit"]


def mutated() -> dict:
    """A copy of the shipped plan for a single test to break."""
    return copy.deepcopy(PLAN)


@contextlib.contextmanager
def live(plan: dict, mutate_config=None):
    """Mount a plan together with the Kimi configuration that plan would produce."""
    with (
        patch.object(PROXY, "POLICY_PATH", write_plan(plan)),
        patch.object(PROXY, "KIMI_CONFIG_PATH", render_config(plan, mutate=mutate_config)),
    ):
        yield


@contextlib.contextmanager
def live_plan(plan: dict):
    """Mount a plan beside the shipped configuration, for refusals the config cannot cause.

    A structurally broken plan often cannot be rendered at all, which is exactly the point:
    the plan is authoritative, and the proxy has to refuse it before the configuration is
    consulted.
    """
    with (
        patch.object(PROXY, "POLICY_PATH", write_plan(plan)),
        patch.object(PROXY, "KIMI_CONFIG_PATH", KIMI_CONFIG_PATH),
    ):
        yield


def loaded_policy(plan: dict, mutate_config=None) -> PROXY.RuntimePolicy:
    with live(plan, mutate_config):
        return PROXY.load_runtime_policy()


def reset_policy_state() -> None:
    PROXY._policy_cache = None
    PROXY._policy_error = None
    PROXY._policy_error_logged = False


def clean_counters() -> None:
    """Return the shared enforcement objects to a known-empty state."""
    PROXY.enforcement.sync(POLICY)
    for ledger in PROXY.enforcement.ledgers.values():
        ledger.bookings.clear()
        ledger.committed = 0
        ledger.server_remaining = None
        ledger.server_expires_at = 0.0
    gate = PROXY.enforcement.gates[CONTEXT_ID]
    gate.reserved = gate.active = gate.active_primary = gate.active_subagents = 0
    # A test that failed while a primary request was queued would otherwise leave this set,
    # and line 846 makes every later subagent admission wait on it.
    gate.waiting_primary = 0


class Response:
    def __init__(self, headers):
        self.headers = headers


class Request:
    def __init__(self, method="POST", content_type="application/json", headers=None):
        self.method = method
        self.content_type = content_type
        self.headers = headers or {}


class SecretTests(unittest.TestCase):
    def test_a_short_provider_key_is_accepted_at_startup(self):
        # Provider keys are whatever the issuer hands out; only the harness's own internal
        # secrets have a minimum length.
        with patch.dict(os.environ, {"TEST_KEY_FILE": "", "TEST_KEY": "x"}):
            self.assertEqual(PROXY.secret("TEST_KEY_FILE", "TEST_KEY", min_length=1), "x")

    def test_file_secret_takes_precedence(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fixture-key"
            path.write_text(f"{PROVIDER_KEY}\n")
            with patch.dict(os.environ, {"TEST_KEY_FILE": str(path), "TEST_KEY": "fallback"}):
                self.assertEqual(
                    PROXY.secret("TEST_KEY_FILE", "TEST_KEY", min_length=1),
                    PROVIDER_KEY,
                )
                with self.assertRaisesRegex(RuntimeError, "at least 32"):
                    PROXY.secret("TEST_KEY_FILE", "TEST_KEY")
            path.write_text(" \n")
            with patch.dict(os.environ, {"TEST_KEY_FILE": str(path), "TEST_KEY": "fallback"}):
                with self.assertRaises(RuntimeError):
                    PROXY.secret("TEST_KEY_FILE", "TEST_KEY", min_length=1)

    def test_empty_provider_key_and_short_internal_secrets_are_rejected(self):
        for value in ("", " ", "\n"):
            with self.subTest(value=value), patch.dict(
                os.environ, {"TEST_KEY_FILE": "", "TEST_KEY": value}
            ), self.assertRaises(RuntimeError):
                PROXY.secret("TEST_KEY_FILE", "TEST_KEY", min_length=1)
        with patch.dict(os.environ, {"TEST_KEY_FILE": "", "TEST_KEY": "too-short"}):
            with self.assertRaisesRegex(RuntimeError, "at least 32") as error:
                PROXY.secret("TEST_KEY_FILE", "TEST_KEY")
            self.assertNotIn("too-short", str(error.exception))

    def test_the_startup_secrets_come_from_the_model_proxy_variables(self):
        # The fixture sets only MODEL_PROXY_* names, so this also fails if the proxy ever
        # reads a legacy NRP_* name again - which would mean the launcher and the container
        # disagree about who owns the internal bearer, leaving an unauthenticated agent.
        self.assertEqual(PROXY.INTERNAL_BEARER_TOKEN, INTERNAL_TOKEN)
        self.assertEqual(PROXY.CACHE_SALT, CACHE_SALT)


class CredentialTests(unittest.TestCase):
    """Provider keys arrive as per-credential files, never as environment values."""

    def test_credential_for_every_planned_lane_is_mounted(self):
        for name in SECRET_NAMES:
            with self.subTest(credential=name):
                self.assertEqual(PROXY.upstream_credential(name), PROVIDER_KEY)

    def test_two_credentials_on_one_provider_stay_separate(self):
        # Models may share a key or not; the credential id the lane names decides which
        # file its traffic is authenticated with, and the proxy never holds the value.
        plan = mutated()
        plan["providers"]["nrp"]["credentials"]["second"] = {
            "label": "second",
            "prompt": "second",
            "env": "HARNESS_SECOND_KEY",
            "key_url": "",
            "secret_name": "nrp__second",
        }
        plan["lanes"]["primary"]["credential"] = "second"
        path = SECRETS_DIR / "nrp__second"
        path.write_text("other-fixture\n")
        self.addCleanup(path.unlink)
        with live(plan):
            policy = PROXY.load_runtime_policy()
        self.assertEqual(policy.lanes["primary"].secret_name, "nrp__second")
        self.assertEqual(PROXY.upstream_credential("nrp__second"), "other-fixture")
        self.assertEqual(PROXY.upstream_credential(SUBAGENT.secret_name), PROVIDER_KEY)

    def test_credential_name_cannot_reach_outside_the_secrets_directory(self):
        for name in ("nrp/../default", "NRP__DEFAULT", "a" * 65, "nrp default"):
            with self.subTest(name=name), self.assertRaisesRegex(PROXY.PolicyDrift, "invalid"):
                PROXY.upstream_credential(name)

    def test_missing_credential_fails_closed(self):
        with self.assertRaisesRegex(PROXY.PolicyDrift, "not mounted"):
            PROXY.upstream_credential("nrp__absent")

    def test_empty_credential_fails_closed(self):
        path = SECRETS_DIR / "nrp__empty_fixture"
        path.write_text("\n")
        self.addCleanup(path.unlink)
        with self.assertRaisesRegex(PROXY.PolicyDrift, "is empty"):
            PROXY.upstream_credential("nrp__empty_fixture")


class RequestValidationTests(unittest.TestCase):
    def test_only_planned_lane_routes_are_registered(self):
        app = PROXY.create_app()
        routes = {(route.method, route.resource.canonical) for route in app.router.routes()}

        # aiohttp reports a dynamic resource by its canonical template; it does
        # not expand the lane regex into one canonical path per allowed value.
        # add_get also registers the corresponding HEAD route automatically.
        self.assertEqual(
            routes,
            {
                ("GET", "/healthz"),
                ("HEAD", "/healthz"),
                ("POST", "/{lane}/v1/chat/completions"),
                ("GET", "/{lane}/v1/models"),
                ("HEAD", "/{lane}/v1/models"),
            },
        )

        # The canonical form omits the variable's regex, so verify separately that both
        # dynamic resources restrict traffic to the lanes the plan actually published.
        alternation = "|".join(sorted(LANES))
        dynamic_patterns = {
            route.resource.get_info()["pattern"].pattern
            for route in app.router.routes()
            if route.resource.canonical.startswith("/{lane}/")
        }
        self.assertEqual(
            dynamic_patterns,
            {
                rf"/(?P<lane>{alternation})/v1/chat/completions",
                rf"/(?P<lane>{alternation})/v1/models",
            },
        )

    def test_a_lane_the_plan_does_not_publish_is_not_routable(self):
        plan = mutated()
        del plan["lanes"]["long"]
        name = "model_proxy_without_the_long_lane"
        environment = {
            "MODEL_PROXY_POLICY": write_plan(plan),
            "KIMI_CONFIG_PATH": render_config(plan),
            "MODEL_PROXY_SECRETS_DIR": str(SECRETS_DIR),
            "MODEL_PROXY_INTERNAL_TOKEN": INTERNAL_TOKEN,
            "MODEL_PROXY_CACHE_SALT": CACHE_SALT,
        }
        with patch.dict(os.environ, environment):
            variant = load_module(name, ROOT / "proxy" / "model_proxy.py")
        self.addCleanup(sys.modules.pop, name, None)

        self.assertNotIn("long", variant.BASELINE_POLICY.lanes)
        patterns = {
            route.resource.get_info()["pattern"].pattern
            for route in variant.create_app().router.routes()
            if route.resource.canonical.startswith("/{lane}/")
        }
        # Built from the variant's own lanes rather than counted, so this says both things at
        # once: the deselected lane is out of the alternation, and the lanes that remain are
        # still routable. A count would pass on an empty app, and "long" would slip through as a
        # substring of some other lane's name.
        remaining = "|".join(sorted(variant.BASELINE_POLICY.lanes))
        self.assertEqual(
            patterns,
            {
                rf"/(?P<lane>{remaining})/v1/chat/completions",
                rf"/(?P<lane>{remaining})/v1/models",
            },
        )

    def test_model_is_rewritten_to_the_lane_model(self):
        body = PROXY.rewrite_model(
            json.dumps({"model": "a-different-alias", "messages": []}).encode(),
            Request(),
            SUBAGENT,
        )
        payload = json.loads(body)
        self.assertEqual(payload["model"], SUBAGENT.model)
        self.assertEqual(payload["cache_salt"], CACHE_SALT)

    def test_a_body_cannot_ask_for_a_bigger_window_than_its_lane_reserved(self):
        oversized = PROXY.rewrite_model(
            json.dumps({"messages": [], "max_tokens": 10 * PRIMARY.output_clamp}).encode(),
            Request(),
            PRIMARY,
        )
        self.assertEqual(json.loads(oversized)["max_tokens"], PRIMARY.output_clamp)

    def test_malformed_json_is_rejected(self):
        with self.assertRaises(PROXY.web.HTTPBadRequest):
            PROXY.rewrite_model(b"{not-json", Request(), SUBAGENT)

    def test_non_finite_numbers_are_rejected(self):
        for body in (b'{"messages":[],"temperature":NaN}', b'{"messages":[],"top_p":Infinity}'):
            with self.subTest(body=body), self.assertRaises(PROXY.web.HTTPBadRequest):
                PROXY.rewrite_model(body, Request(), SUBAGENT)

    def test_duplicate_json_keys_are_rejected(self):
        with self.assertRaises(PROXY.web.HTTPBadRequest):
            PROXY.rewrite_model(b'{"messages":[],"messages":[]}', Request(), SUBAGENT)

    def test_a_missing_messages_array_is_rejected(self):
        with self.assertRaises(PROXY.web.HTTPBadRequest):
            PROXY.rewrite_model(json.dumps({"prompt": "hi"}).encode(), Request(), SUBAGENT)

    def test_a_non_string_model_is_rejected(self):
        with self.assertRaises(PROXY.web.HTTPBadRequest):
            PROXY.rewrite_model(
                json.dumps({"messages": [], "model": {"evil": True}}).encode(),
                Request(),
                SUBAGENT,
            )

    def test_incorrect_internal_bearer_is_rejected(self):
        with self.assertRaises(PROXY.web.HTTPUnauthorized):
            PROXY.authorize_client(Request(headers={"Authorization": "Bearer wrong"}))

    def test_valid_internal_bearer_is_accepted(self):
        PROXY.authorize_client(Request(headers={"Authorization": f"Bearer {INTERNAL_TOKEN}"}))

    def test_conflicting_output_limits_are_rejected(self):
        with self.assertRaises(PROXY.web.HTTPBadRequest):
            PROXY.rewrite_model(
                json.dumps({"messages": [], "max_tokens": 2, "max_completion_tokens": 3}).encode(),
                Request(),
                SUBAGENT,
            )

    def test_connection_nominated_header_is_removed(self):
        request = Request(headers={"Connection": "X-Remove", "X-Remove": "secret", "X-Keep": "yes"})
        headers = PROXY.filtered_request_headers(request, PROVIDER_KEY)
        self.assertNotIn("X-Remove", headers)
        self.assertEqual(headers["X-Keep"], "yes")

    def test_the_provider_key_replaces_the_internal_one(self):
        # The agent's bearer is the harness token; it must never reach the provider, and
        # the provider key must never be readable by the agent.
        request = Request(headers={"Authorization": f"Bearer {INTERNAL_TOKEN}"})
        headers = PROXY.filtered_request_headers(request, PROVIDER_KEY)
        self.assertEqual(headers["Authorization"], f"Bearer {PROVIDER_KEY}")

    def test_upstream_requests_ask_for_identity_encoding(self):
        # Usage is read out of the response stream, so it must not be compressed.
        headers = PROXY.filtered_request_headers(
            Request(headers={"Accept-Encoding": "gzip"}), PROVIDER_KEY
        )
        self.assertEqual(headers["Accept-Encoding"], "identity")

    def test_traffic_goes_to_the_endpoint_the_provider_publishes(self):
        with live(mutated()):
            policy = PROXY.load_runtime_policy()
        for name, lane in policy.lanes.items():
            with self.subTest(lane=name):
                provider = PLAN["providers"][PLAN["lanes"][name]["provider"]]
                self.assertEqual(lane.base_url, provider["base_url"].rstrip("/"))
                self.assertEqual(lane.model, PLAN["lanes"][name]["model"])


class RetryDelayTests(unittest.TestCase):
    def test_numeric_retry_after_is_clamped(self):
        self.assertEqual(PROXY.retry_delay(Response({"Retry-After": "900"}), 1), 300)

    def test_http_date_retry_after(self):
        with patch.object(PROXY.time, "time", return_value=1_000.0):
            delay = PROXY.retry_delay(Response({"Retry-After": "Thu, 01 Jan 1970 00:18:20 GMT"}), 1)
        self.assertEqual(delay, 100)

    def test_epoch_millisecond_reset(self):
        now = time.time()
        with patch.object(PROXY.time, "time", return_value=now):
            delay = PROXY.retry_delay(Response({"x-ratelimit-reset": str((now + 45) * 1000)}), 1)
        self.assertAlmostEqual(delay, 45, places=3)


class PlanLoadingTests(unittest.TestCase):
    """A plan the proxy cannot serve is refused before it can cost anybody anything."""

    def load(self, plan):
        with live_plan(plan):
            return PROXY.load_runtime_policy()

    def test_the_shipped_plan_loads(self):
        policy = loaded_policy(mutated())
        self.assertEqual(sorted(policy.lanes), sorted(PLAN["lanes"]))
        self.assertEqual(sorted(policy.counters), sorted(PLAN["counters"]))
        self.assertEqual(policy.reserved, PLAN["reserved_context_size"])
        self.assertEqual(
            sorted(policy.providers),
            sorted({entry["provider_name"] for entry in PLAN["lanes"].values()}),
        )

    def test_a_plan_of_another_schema_version_is_refused(self):
        plan = mutated()
        plan["schema_version"] = plan["schema_version"] + 1
        with self.assertRaisesRegex(PROXY.PolicyDrift, "schema"):
            self.load(plan)

    def test_an_unreadable_plan_is_refused(self):
        with patch.object(PROXY, "POLICY_PATH", str(FIXTURE_DIR / "nope.json")):
            with self.assertRaisesRegex(PROXY.PolicyDrift, "cannot read model policy"):
                PROXY.load_runtime_policy()

    def test_a_plan_with_no_lanes_or_no_providers_is_refused(self):
        for key in ("lanes", "providers"):
            plan = mutated()
            plan[key] = {}
            with self.subTest(key=key), self.assertRaisesRegex(PROXY.PolicyDrift, key):
                self.load(plan)

    def test_a_lane_naming_a_provider_the_plan_omits_is_refused(self):
        plan = mutated()
        plan["lanes"]["primary"]["provider"] = "who"
        with self.assertRaisesRegex(PROXY.PolicyDrift, "which the plan omits"):
            self.load(plan)

    def test_a_lane_naming_a_credential_the_provider_omits_is_refused(self):
        plan = mutated()
        plan["lanes"]["primary"]["credential"] = "nope"
        with self.assertRaisesRegex(PROXY.PolicyDrift, "credential"):
            self.load(plan)

    def test_a_provider_without_a_usable_endpoint_is_refused(self):
        plan = mutated()
        for provider in plan["providers"].values():
            provider["base_url"] = "not-a-url"
        with self.assertRaisesRegex(PROXY.PolicyDrift, "no usable endpoint"):
            self.load(plan)

    def test_a_lane_number_must_be_a_positive_integer(self):
        for key, value in (
            ("context_tokens", 0),
            ("input_tokens", "not-an-integer"),
            ("output_clamp_tokens", -1),
        ):
            plan = mutated()
            plan["lanes"]["subagent"][key] = value
            with self.subTest(key=key), self.assertRaisesRegex(PROXY.PolicyDrift, "positive"):
                self.load(plan)

    def test_a_lane_name_that_cannot_be_a_route_is_refused(self):
        plan = mutated()
        plan["lanes"]["Pri"] = plan["lanes"].pop("primary")
        with self.assertRaisesRegex(PROXY.PolicyDrift, "cannot be used in a route"):
            self.load(plan)


class ConfigurationDriftTests(unittest.TestCase):
    """The rendered Kimi configuration is cross-checked, never trusted."""

    def drift(self, mutate):
        with self.assertRaises(PROXY.PolicyDrift) as caught:
            loaded_policy(mutated(), mutate)
        return str(caught.exception)

    def test_secondary_model_must_stay_forced(self):
        def mutate(document):
            document["secondary_model"]["force"] = False

        self.assertIn("force must be true", self.drift(mutate))

    def test_forced_secondary_model_must_be_the_subagent_lane_model(self):
        def mutate(document):
            document["secondary_model"]["default_model"] = PRIMARY.alias

        self.assertIn(SUBAGENT.alias, self.drift(mutate))

    def test_the_default_model_must_be_the_primary_lane_model(self):
        def mutate(document):
            document["default_model"] = SUBAGENT.alias

        self.assertIn(PRIMARY.alias, self.drift(mutate))

    def test_kimi_must_still_offer_every_planned_lane(self):
        def mutate(document):
            del document["models"][LONG.alias]

        self.assertIn("does not offer", self.drift(mutate))

    def test_a_lane_model_must_keep_its_provider_binding(self):
        def mutate(document):
            document["models"][SUBAGENT.alias]["provider"] = PRIMARY.provider_name

        self.assertIn("binds", self.drift(mutate))

    def test_a_lane_must_keep_the_size_the_plan_priced(self):
        for key in ("max_context_size", "max_input_size"):

            def mutate(document, key=key):
                document["models"][PRIMARY.alias][key] += 1

            self.assertIn("sizes", self.drift(mutate))

    def test_the_output_reserve_must_match_the_plan(self):
        def mutate(document):
            document["loop_control"]["reserved_context_size"] += 1

        self.assertIn("budgeted", self.drift(mutate))

    def test_a_config_that_cannot_be_parsed_is_refused(self):
        with live(mutated()):
            with patch.object(PROXY, "KIMI_CONFIG_PATH", str(FIXTURE_DIR / "gone.toml")):
                with self.assertRaisesRegex(PROXY.PolicyDrift, "cannot read the rendered"):
                    PROXY.load_runtime_policy()


class ValidationTests(unittest.TestCase):
    """Startup proves the plan is servable; it never decides a limit itself.

    The servability of the shipped plan itself is not repeated here as a test: importing the
    module above already runs ``validate_policy(BASELINE_POLICY)``, which is why a plan that
    could not be served fails every test in this file rather than one of them.
    """

    def test_every_lane_input_allowance_fits_its_own_window(self):
        for name, lane in POLICY.lanes.items():
            with self.subTest(lane=name):
                self.assertLessEqual(lane.max_input + lane.output_clamp, lane.context)
                self.assertLessEqual(lane.max_input + lane.reserved, lane.context)

    def test_a_lane_that_could_never_be_admitted_stops_startup(self):
        plan = mutated()
        lane = plan["lanes"]["primary"]
        # Anything strictly between the aggregate budget and the exclusivity threshold is
        # both too large to share the budget and too small to be allowed to run alone.
        reservation = BUDGET + (EXCLUSIVE_AT - BUDGET) // 2
        lane["output_clamp_tokens"] = reservation // 2
        lane["input_tokens"] = reservation - lane["output_clamp_tokens"]
        lane["context_tokens"] = reservation + plan["reserved_context_size"]
        policy = loaded_policy(plan)
        with self.assertRaisesRegex(RuntimeError, "could ever be admitted"):
            PROXY.validate_policy(policy)

    def test_an_exclusive_reservation_may_exceed_the_budget(self):
        # The long lane is priced above the budget on purpose: the provider lets a request
        # that large run provided nothing else runs beside it.
        self.assertGreater(LONG.reservation, BUDGET)
        gate = PROXY.FairUseGate(CONTEXT_ID, budget=BUDGET, exclusive_at=EXCLUSIVE_AT)
        self.assertTrue(gate.is_exclusive(LONG.reservation))

    def test_a_subagent_batch_over_the_budget_stops_startup(self):
        plan = mutated()
        plan["limits"]["subagent_concurrency"] = FAN_OUT + 1
        policy = loaded_policy(plan)
        with self.assertRaisesRegex(RuntimeError, "exceed the"):
            PROXY.validate_policy(policy)

    def test_a_budget_above_the_aggregate_ceiling_stops_startup(self):
        plan = mutated()
        plan["counters"][CONTEXT_ID]["budget"] = CEILING + 1
        policy = loaded_policy(plan)
        with self.assertRaisesRegex(RuntimeError, "aggregate ceiling"):
            PROXY.validate_policy(policy)

    def test_a_lane_naming_an_unknown_counter_stops_startup(self):
        plan = mutated()
        plan["lanes"]["subagent"]["counters"] = ["context:nope"]
        policy = loaded_policy(plan)
        with self.assertRaisesRegex(RuntimeError, "unknown counter"):
            PROXY.validate_policy(policy)

    def test_a_zero_ingress_bound_stops_startup(self):
        with patch.object(PROXY, "MAX_QUEUED", 0):
            with self.assertRaisesRegex(RuntimeError, "MODEL_PROXY_MAX_QUEUED must be positive"):
                PROXY.validate_policy(POLICY)

    def test_an_input_guard_below_the_window_stops_startup(self):
        with patch.object(PROXY, "INPUT_GUARD_PERCENT", 99):
            with self.assertRaisesRegex(RuntimeError, "at least 100"):
                PROXY.validate_policy(POLICY)


class EnforcementSyncTests(unittest.TestCase):
    """The live counter objects are whatever the plan publishes, no more and no less."""

    def setUp(self):
        clean_counters()
        self.addCleanup(clean_counters)

    def test_the_shipped_plan_yields_one_context_gate_and_one_rate_ledger(self):
        self.assertEqual(sorted(PROXY.enforcement.gates), [CONTEXT_ID])
        self.assertEqual(sorted(PROXY.enforcement.ledgers), [RATE_ID])

    def test_context_gate_carries_the_plan_numbers(self):
        gate = PROXY.enforcement.gates[CONTEXT_ID]
        self.assertEqual(gate.budget, BUDGET)
        self.assertEqual(gate.exclusive_at, EXCLUSIVE_AT)
        self.assertEqual(gate.limit, MODEL_LIMIT)
        self.assertEqual(gate.subagent_limit, FAN_OUT)

    def test_rate_ledger_meters_the_unit_the_rule_names(self):
        ledger = PROXY.enforcement.ledgers[RATE_ID]
        self.assertEqual(ledger.capacity, RATE_CAPACITY)
        self.assertEqual(ledger.unit, RATE_UNIT)

    def test_syncing_a_smaller_plan_forgets_the_counters_it_no_longer_publishes(self):
        policy = PROXY.RuntimePolicy(
            lanes={"subagent": SUBAGENT},
            counters={RATE_ID: RATE_COUNTER},
            limits={},
            reserved=POLICY.reserved,
            providers=("nrp",),
        )
        PROXY.enforcement.sync(policy)
        self.assertEqual(PROXY.enforcement.gates, {})
        self.assertEqual(sorted(PROXY.enforcement.ledgers), [RATE_ID])

    def test_sync_moves_the_numbers_but_keeps_the_objects(self):
        before = PROXY.enforcement.gates[CONTEXT_ID]
        plan = mutated()
        plan["counters"][CONTEXT_ID]["budget"] = BUDGET - 1000
        loaded_policy(plan)
        PROXY.enforcement.sync(
            PROXY.RuntimePolicy(
                lanes=dict(POLICY.lanes),
                counters={
                    CONTEXT_ID: plan["counters"][CONTEXT_ID],
                    RATE_ID: RATE_COUNTER,
                },
                limits=POLICY.limits,
                reserved=POLICY.reserved,
                providers=POLICY.providers,
            )
        )
        self.assertIs(PROXY.enforcement.gates[CONTEXT_ID], before)
        self.assertEqual(before.budget, BUDGET - 1000)

    def test_a_lane_only_holds_its_own_counters_in_sorted_order(self):
        for name, lane in POLICY.lanes.items():
            with self.subTest(lane=name):
                self.assertEqual(sorted(lane.counters), list(lane.counters))
                gates = PROXY.enforcement.gates_for(lane)
                self.assertEqual(
                    [gate.counter_id for gate in gates],
                    [key for key in lane.counters if key in PROXY.enforcement.gates],
                )
                ledgers = PROXY.enforcement.ledgers_for(lane)
                self.assertEqual(
                    [key for key, _ledger in ledgers],
                    [key for key in lane.counters if key in PROXY.enforcement.ledgers],
                )


class FairUseGateTests(unittest.IsolatedAsyncioTestCase):
    def gate(self, **kwargs):
        arguments = {
            "budget": BUDGET,
            "exclusive_at": EXCLUSIVE_AT,
            "limit": MODEL_LIMIT,
            "subagent_limit": FAN_OUT,
        }
        arguments.update(kwargs)
        return PROXY.FairUseGate("fixture", **arguments)

    async def test_waiting_primary_blocks_a_new_subagent(self):
        gate = self.gate(budget=PRIMARY_RESERVATION + SUBAGENT_RESERVATION - 1)
        events = []
        release_first = asyncio.Event()

        async def first_subagent():
            async with gate.slot("subagent", SUBAGENT_RESERVATION):
                events.append("subagent-one-enter")
                await release_first.wait()
                events.append("subagent-one-exit")

        async def primary():
            async with gate.slot("primary", PRIMARY_RESERVATION):
                events.append("primary")

        async def second_subagent():
            async with gate.slot("subagent", SUBAGENT_RESERVATION):
                events.append("subagent-two")

        first = asyncio.create_task(first_subagent())
        while gate.active_subagents != 1:
            await asyncio.sleep(0)
        primary_task = asyncio.create_task(primary())
        while gate.waiting_primary != 1:
            await asyncio.sleep(0)
        second = asyncio.create_task(second_subagent())
        await asyncio.sleep(0)
        release_first.set()
        await asyncio.gather(first, primary_task, second)
        self.assertLess(events.index("primary"), events.index("subagent-two"))

    async def test_cancelled_primary_releases_waiter_count(self):
        gate = self.gate(budget=PRIMARY_RESERVATION + SUBAGENT_RESERVATION - 1)
        async with gate.slot("subagent", SUBAGENT_RESERVATION):
            task = asyncio.create_task(self._take_slot(gate, "primary", PRIMARY_RESERVATION))
            while gate.waiting_primary != 1:
                await asyncio.sleep(0)
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            self.assertEqual(gate.waiting_primary, 0)

    async def test_subagent_concurrency_is_capped(self):
        gate = self.gate(budget=10**9, subagent_limit=2)
        active = 0
        maximum = 0
        entered = asyncio.Event()
        release = asyncio.Event()

        async def subagent():
            nonlocal active, maximum
            async with gate.slot("subagent", SUBAGENT_RESERVATION):
                active += 1
                maximum = max(maximum, active)
                if active == 2:
                    entered.set()
                await release.wait()
                active -= 1

        tasks = [asyncio.create_task(subagent()) for _ in range(3)]
        await entered.wait()
        await asyncio.sleep(0)
        self.assertEqual(gate.active_subagents, 2)
        release.set()
        await asyncio.gather(*tasks)
        self.assertEqual(maximum, 2)

    async def test_the_resolved_fan_out_is_the_most_the_budget_allows(self):
        # Greedy by design: the plan sizes the swarm at the largest batch that fits, and
        # the gate admits exactly that many at once, with no artificial headroom left over.
        self.assertEqual(FAN_OUT, BUDGET // SUBAGENT_RESERVATION)
        self.assertEqual(await self._peak(["subagent"] * (FAN_OUT + 3)), FAN_OUT)
        self.assertEqual(
            await self._peak(["subagent"] * (FAN_OUT + 3), subagent_limit=None), FAN_OUT
        )

    async def test_the_primary_lane_shares_the_budget_with_one_subagent(self):
        # 262,144 + 64,000 stays inside the resolved budget, so the operator's own turn and
        # one child run together; a second child has to wait for a slot to open.
        self.assertLessEqual(PRIMARY_RESERVATION + SUBAGENT_RESERVATION, BUDGET)
        self.assertGreater(PRIMARY_RESERVATION + 2 * SUBAGENT_RESERVATION, BUDGET)
        self.assertEqual(
            await self._peak(["primary", *(["subagent"] * FAN_OUT)]),
            2,
        )

    async def test_an_exclusive_reservation_runs_alone(self):
        self.assertEqual(
            await self._peak(["long", "subagent", "subagent", "primary"]),
            1,
        )

    async def test_exclusivity_follows_the_threshold_not_the_lane_name(self):
        gate = self.gate(exclusive_at=None)
        self.assertFalse(gate.is_exclusive(EXCLUSIVE_AT))
        gate = self.gate(exclusive_at=EXCLUSIVE_AT)
        self.assertTrue(gate.is_exclusive(EXCLUSIVE_AT))
        self.assertFalse(gate.is_exclusive(EXCLUSIVE_AT - 1))

    async def test_model_concurrency_caps_total_requests(self):
        self.assertEqual(
            await self._peak(["subagent"] * 4, limit=2, subagent_limit=4, budget=10**9),
            2,
        )

    async def test_a_finished_subagent_returns_its_reservation(self):
        gate = self.gate()
        entered = asyncio.Event()
        release = asyncio.Event()

        async def primary():
            async with gate.slot("primary", PRIMARY_RESERVATION):
                entered.set()
                await release.wait()

        running = asyncio.create_task(primary())
        await entered.wait()
        # The resolved budget leaves room for one child beside the operator's own turn.
        await self._take_slot(gate, "subagent", SUBAGENT_RESERVATION)
        self.assertEqual((gate.active, gate.reserved), (1, PRIMARY_RESERVATION))
        release.set()
        await running
        self.assertEqual((gate.active, gate.reserved), (0, 0))

    async def _peak(self, lanes, **kwargs):
        reservations = {name: lane.reservation for name, lane in LANES.items()}
        gate = self.gate(**kwargs)
        live = 0
        peak = 0
        release = asyncio.Event()

        async def one(lane):
            nonlocal live, peak
            async with gate.slot(lane, reservations[lane]):
                live += 1
                peak = max(peak, live)
                await release.wait()
                live -= 1

        tasks = [asyncio.create_task(one(lane)) for lane in lanes]
        while gate.active == 0:
            await asyncio.sleep(0)
        await asyncio.sleep(0.01)
        release.set()
        await asyncio.gather(*tasks)
        return peak

    @staticmethod
    async def _take_slot(gate, lane, reservation):
        async with gate.slot(lane, reservation):
            return


class IngressTests(unittest.IsolatedAsyncioTestCase):
    async def test_ingress_rejects_beyond_active_and_queue_bound(self):
        ingress = PROXY.IngressGate(active=1, queued=1)
        release = asyncio.Event()

        async def occupy():
            async with ingress.slot():
                await release.wait()

        active = asyncio.create_task(occupy())
        while ingress.pending != 1:
            await asyncio.sleep(0)
        queued = asyncio.create_task(occupy())
        while ingress.pending != 2:
            await asyncio.sleep(0)
        with self.assertRaises(PROXY.web.HTTPServiceUnavailable):
            async with ingress.slot():
                pass
        release.set()
        await asyncio.gather(active, queued)

    def test_ingress_bounds_the_primary_plus_the_whole_swarm(self):
        # Kimi's own background-slot setting is what bounds this, so a request that is
        # still waiting for a fair-use permit holds an ingress place rather than a slot.
        self.assertEqual(
            PROXY.ingress.limit, 1 + FAN_OUT + PROXY.MAX_QUEUED
        )


class RateLedgerTests(unittest.TestCase):
    """A rolling window books the worst case up front and settles to what was measured."""

    def setUp(self):
        self.now = 1000.0
        self.ledger = PROXY.RateLedger(RATE_CAPACITY, RATE_UNIT, clock=lambda: self.now)

    def advance(self, seconds: float) -> None:
        self.now += seconds

    def test_reservation_books_the_full_lane_allowance(self):
        booking = self.ledger.reserve(SUBAGENT.output_clamp)
        self.assertIsNotNone(booking)
        self.assertEqual(self.ledger.committed, SUBAGENT.output_clamp)
        self.assertEqual(self.ledger.available(), RATE_CAPACITY - SUBAGENT.output_clamp)

    def test_admission_stops_at_capacity(self):
        self.assertIsNotNone(self.ledger.reserve(120000))
        # A booking that would push the window past capacity is refused, but an
        # exact fill is allowed.
        self.assertIsNone(self.ledger.reserve(RATE_CAPACITY - 120000 + 1))
        self.assertIsNotNone(self.ledger.reserve(RATE_CAPACITY - 120000))
        self.assertEqual(self.ledger.committed, RATE_CAPACITY)
        self.assertIsNone(self.ledger.reserve(1))
        self.assertGreater(self.ledger.wait_seconds(), 0)

    def test_settling_to_measured_usage_reopens_capacity_immediately(self):
        booking = self.ledger.reserve(SUBAGENT.output_clamp)
        self.ledger.settle(booking, 1200)
        self.assertEqual(self.ledger.committed, 1200)

    def test_a_failed_attempt_gives_the_whole_booking_back(self):
        booking = self.ledger.reserve(SUBAGENT.output_clamp)
        self.ledger.settle(booking, 0)
        self.assertEqual(self.ledger.committed, 0)

    def test_unknown_usage_keeps_the_pessimistic_booking(self):
        booking = self.ledger.reserve(SUBAGENT.output_clamp)
        self.ledger.settle(booking, None)
        self.assertEqual(self.ledger.committed, SUBAGENT.output_clamp)

    def test_booking_is_settled_once(self):
        booking = self.ledger.reserve(SUBAGENT.output_clamp)
        self.ledger.settle(booking, 100)
        self.ledger.settle(booking, 99999)
        self.assertEqual(self.ledger.committed, 100)

    def test_settling_an_expired_booking_does_not_double_count(self):
        booking = self.ledger.reserve(SUBAGENT.output_clamp)
        self.advance(61)
        self.assertEqual(self.ledger.projected(), 0)
        self.ledger.settle(booking, 5000)
        self.assertEqual(self.ledger.projected(), 5000)

    def test_window_rolls_after_sixty_seconds(self):
        self.assertIsNotNone(self.ledger.reserve(RATE_CAPACITY))
        self.assertIsNone(self.ledger.reserve(1))
        self.advance(60.5)
        self.assertIsNotNone(self.ledger.reserve(RATE_CAPACITY))

    def test_gateway_remaining_quota_wins(self):
        self.ledger.note_server({"x-ratelimit-remaining": "2000", "x-ratelimit-reset": "12"})
        self.assertEqual(self.ledger.available(), 2000)
        self.assertIsNone(self.ledger.reserve(2001))
        self.assertIsNotNone(self.ledger.reserve(2000))

    def test_gateway_quota_is_forgotten_once_its_window_passes(self):
        self.ledger.note_server({"x-ratelimit-remaining": "10", "x-ratelimit-reset": "5"})
        self.advance(6)
        self.assertEqual(self.ledger.available(), RATE_CAPACITY)

    def test_unparsable_quota_headers_are_ignored(self):
        self.ledger.note_server({"x-ratelimit-remaining": "soon"})
        self.assertIsNone(self.ledger.server_remaining)
        self.assertEqual(self.ledger.available(), RATE_CAPACITY)

    def test_two_equal_bookings_are_independent(self):
        first = self.ledger.reserve(SUBAGENT.output_clamp)
        second = self.ledger.reserve(SUBAGENT.output_clamp)
        self.ledger.settle(first, 0)
        self.assertEqual(self.ledger.committed, SUBAGENT.output_clamp)
        self.ledger.settle(second, 0)
        self.assertEqual(self.ledger.committed, 0)

    def test_an_unknown_metered_unit_is_refused(self):
        with self.assertRaisesRegex(RuntimeError, "unknown rate unit"):
            PROXY.RateLedger(100, "characters")


class MeteredUnitTests(unittest.TestCase):
    """Any unit a provider might meter is charged from the same request."""

    def cost(self):
        return PROXY.Cost(input=1000, output=500)

    def test_each_unit_charges_what_the_name_says(self):
        cases = {
            "requests": 1,
            "input_tokens": 1000,
            "total_tokens": 1500,
            "output_tokens": 500,
        }
        for unit, amount in cases.items():
            with self.subTest(unit=unit):
                self.assertEqual(self.cost().charge(unit), amount)
                ledger = PROXY.RateLedger(10_000, unit)
                self.assertEqual(ledger.charge(self.cost()), amount)

    def test_a_request_ledger_counts_requests(self):
        ledger = PROXY.RateLedger(2, "requests")
        self.assertIsNotNone(ledger.reserve(ledger.charge(self.cost())))
        self.assertIsNotNone(ledger.reserve(ledger.charge(self.cost())))
        self.assertIsNone(ledger.reserve(ledger.charge(self.cost())))

    def test_credit_prefers_measured_usage(self):
        outcome = PROXY.Attempt(output_tokens=11, prompt_tokens=22)
        self.assertEqual(self.cost().credit("output_tokens", outcome), 11)
        self.assertEqual(self.cost().credit("input_tokens", outcome), 22)
        self.assertEqual(self.cost().credit("total_tokens", outcome), 33)
        self.assertEqual(self.cost().credit("requests", outcome), 1)

    def test_an_unmeasurable_unit_keeps_the_pessimistic_charge(self):
        outcome = PROXY.Attempt(output_tokens=None, prompt_tokens=None, estimated_output=None)
        for unit in ("output_tokens", "input_tokens", "total_tokens"):
            with self.subTest(unit=unit):
                self.assertIsNone(self.cost().credit(unit, outcome))

    def test_output_falls_back_to_the_character_estimate(self):
        outcome = PROXY.Attempt(output_tokens=None, estimated_output=4)
        self.assertEqual(self.cost().credit("output_tokens", outcome), 4)

    def test_a_gateway_header_only_moves_the_ledger_it_describes(self):
        headers = {
            "x-ratelimit-remaining": "5",
            "x-ratelimit-reset": "10",
            "x-ratelimit-requests-remaining": "7",
            "x-ratelimit-requests-reset": "10",
        }
        for unit, want in (("output_tokens", 5), ("requests", 7)):
            with self.subTest(unit=unit):
                ledger = PROXY.RateLedger(1000, unit)
                ledger.note_server(headers)
                self.assertEqual(ledger.server_remaining, want)
        for unit in ("input_tokens", "total_tokens"):
            with self.subTest(unit=unit):
                ledger = PROXY.RateLedger(1000, unit)
                ledger.note_server(headers)
                self.assertIsNone(ledger.server_remaining)

    def test_a_booked_unit_appears_in_the_health_snapshot(self):
        self.assertEqual(self.snapshot_unit(), RATE_UNIT)

    def snapshot_unit(self):
        return PROXY.RateLedger(10, RATE_UNIT).snapshot()["unit"]


class EnforcementBookingTests(unittest.TestCase):
    """Every counter a lane is charged to is booked together, or not at all."""

    def lane(self, *counter_ids):
        import dataclasses

        return dataclasses.replace(SUBAGENT, counters=list(counter_ids))

    def test_one_request_books_once_per_ledger(self):
        enforcement = PROXY.Enforcement()
        enforcement.ledgers = {
            "a": PROXY.RateLedger(1000, "output_tokens"),
            "b": PROXY.RateLedger(10, "requests"),
        }
        lane = self.lane("a", "b")
        cost = PROXY.Cost(input=10, output=600)
        bookings = enforcement.book(lane, cost)
        self.assertEqual(sorted(bookings), ["a", "b"])
        self.assertEqual(enforcement.ledgers["a"].committed, 600)
        self.assertEqual(enforcement.ledgers["b"].committed, 1)

    def test_a_lane_that_cannot_fill_both_counters_fills_neither(self):
        enforcement = PROXY.Enforcement()
        enforcement.ledgers = {
            "a": PROXY.RateLedger(1000, "output_tokens"),
            "b": PROXY.RateLedger(1, "requests"),
        }
        lane = self.lane("a", "b")
        cost = PROXY.Cost(input=10, output=600)
        self.assertIsNotNone(enforcement.book(lane, cost))
        self.assertIsNone(enforcement.book(lane, cost))
        # The refused attempt left no trace on the ledger it did reach first.
        self.assertEqual(enforcement.ledgers["a"].committed, 600)

    def test_settling_refunds_every_ledger_when_nothing_reached_the_client(self):
        enforcement = PROXY.Enforcement()
        enforcement.ledgers = {"a": PROXY.RateLedger(1000, "output_tokens")}
        lane = self.lane("a")
        cost = PROXY.Cost(input=10, output=600)
        bookings = enforcement.book(lane, cost)
        enforcement.settle(lane, bookings, cost, None)
        self.assertEqual(enforcement.ledgers["a"].committed, 0)


class PolicyReloadTests(unittest.TestCase):
    """Drift in either mounted file stops traffic instead of being ignored."""

    def setUp(self):
        reset_policy_state()
        self.plan_path = write_plan(mutated())
        self.config_path = render_config(mutated())
        patcher = patch.multiple(
            PROXY, POLICY_PATH=self.plan_path, KIMI_CONFIG_PATH=self.config_path
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(reset_policy_state)

    def touch(self, path: str, text: str) -> None:
        target = Path(path)
        target.write_text(text, encoding="utf-8")
        # Guarantee a different mtime+size stamp so the cache cannot return stale data.
        future = int(target.stat().st_mtime_ns) + 1_000_000_000
        os.utime(target, ns=(future, future))

    def test_valid_policy_is_served_and_cached(self):
        first = PROXY.current_policy()
        self.assertIs(PROXY.current_policy(), first)

    def test_becoming_invalid_fails_closed_then_recovers(self):
        self.assertEqual(PROXY.current_policy().lanes["subagent"].alias, SUBAGENT.alias)

        def break_it(document):
            document["secondary_model"]["force"] = False
            return document

        self.touch(self.config_path, CONFIG_MERGE.emit(break_it(tomllib.loads(
            Path(self.config_path).read_text()
        ))))
        with self.assertRaises(PROXY.PolicyDrift):
            PROXY.current_policy()

        def fix_it(document):
            document["secondary_model"]["force"] = True
            return document

        self.touch(self.config_path, CONFIG_MERGE.emit(fix_it(tomllib.loads(
            Path(self.config_path).read_text()
        ))))
        self.assertEqual(PROXY.current_policy().lanes["subagent"].alias, SUBAGENT.alias)

    def test_reservations_follow_the_live_plan(self):
        plan = mutated()
        plan["lanes"]["subagent"]["input_tokens"] -= 1000
        self.touch(self.plan_path, json.dumps(plan))
        self.touch(self.config_path, render_config(plan).read_text() if False else Path(
            render_config(plan)
        ).read_text())
        policy = PROXY.current_policy()
        self.assertEqual(policy.lanes["subagent"].reservation, SUBAGENT.reservation - 1000)

    def test_a_missing_input_file_fails_closed(self):
        PROXY.current_policy()
        os.unlink(self.plan_path)
        with self.assertRaisesRegex(PROXY.PolicyDrift, "unreadable"):
            PROXY.current_policy()


class ChatRouteTests(unittest.IsolatedAsyncioTestCase):
    """The whole chat handler, so status constructions are exercised for real."""

    def setUp(self):
        reset_policy_state()
        clean_counters()
        self.addCleanup(clean_counters)
        self.sent = []

        async def capture(_request, lane, body, _started, _inbound, _cost):
            self.sent.append((lane, json.loads(body)))
            return PROXY.web.Response(text="ok")

        patcher = patch.object(PROXY, "forward_chat", capture)
        patcher.start()
        self.addCleanup(patcher.stop)

    def mount(self, plan=None, mutate_config=None):
        context = live(plan or mutated(), mutate_config)
        context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)
        reset_policy_state()

    def request(self, body: bytes, lane="subagent", headers=None):
        merged = {"Authorization": f"Bearer {INTERNAL_TOKEN}"}
        merged.update(headers or {})
        return SimpleNamespace(
            method="POST",
            content_type="application/json",
            headers=merged,
            match_info={"lane": lane},
            query_string="",
            rel_url=f"/{lane}/v1/chat/completions",
            transport=SimpleNamespace(is_closing=lambda: False),
            app={},
            read=lambda: asyncio.sleep(0, result=body),
        )

    async def test_legal_request_is_forwarded_with_the_lane_model(self):
        self.mount()
        body = json.dumps({"model": "anything", "messages": [{"content": "hi"}]}).encode()
        response = await PROXY.chat(self.request(body))
        self.assertEqual(response.status, 200)
        lane, sent = self.sent[0]
        self.assertEqual(lane.name, "subagent")
        self.assertEqual(sent["model"], lane.model)
        self.assertEqual(sent["max_completion_tokens"], lane.output_clamp)

    async def test_gross_oversize_input_is_rejected_before_the_upstream(self):
        self.mount()
        lane = PROXY.BASELINE_POLICY.lanes["subagent"]
        guard = lane.max_input * PROXY.INPUT_GUARD_PERCENT // 100
        body = json.dumps({"messages": [{"content": "x" * (guard * 8)}]}).encode()
        with self.assertRaises(PROXY.web.HTTPRequestEntityTooLarge):
            await PROXY.chat(self.request(body))
        self.assertEqual(self.sent, [])

    async def test_unverifiable_policy_stops_traffic(self):
        def break_it(document):
            document["secondary_model"]["force"] = False

        self.mount(mutate_config=break_it)
        body = json.dumps({"messages": [{"content": "hi"}]}).encode()
        with self.assertRaises(PROXY.web.HTTPServiceUnavailable) as refused:
            await PROXY.chat(self.request(body))
        self.assertEqual(refused.exception.headers.get("Retry-After"), "5")
        self.assertEqual(self.sent, [])

    async def test_unauthenticated_and_compressed_requests_are_refused(self):
        self.mount()
        body = json.dumps({"messages": []}).encode()
        without_token = self.request(body, headers={"Authorization": "Bearer nope"})
        with self.assertRaises(PROXY.web.HTTPUnauthorized):
            await PROXY.chat(without_token)
        compressed = self.request(body, headers={"Content-Encoding": "gzip"})
        with self.assertRaises(PROXY.web.HTTPUnsupportedMediaType):
            await PROXY.chat(compressed)
        query = self.request(body)
        query.query_string = "user=someone-else"
        with self.assertRaises(PROXY.web.HTTPBadRequest):
            await PROXY.chat(query)
        self.assertEqual(self.sent, [])

    async def test_a_model_only_lane_cannot_be_reached_on_another_lane(self):
        # Routing is the only authority over which model a request reaches; the body's
        # alias is discarded, so a subagent cannot borrow the long lane's window.
        self.mount()
        body = json.dumps({"model": LONG.alias, "messages": []}).encode()
        await PROXY.chat(self.request(body, lane="subagent"))
        _lane, sent = self.sent[0]
        self.assertEqual(sent["model"], SUBAGENT.model)


class ForwardChatTests(unittest.IsolatedAsyncioTestCase):
    """Backoff must never occupy a fair-use permit or a rate booking."""

    def setUp(self):
        reset_policy_state()
        clean_counters()
        self.addCleanup(clean_counters)
        self.policy = PROXY.current_policy()
        self.lane = self.policy.lanes["subagent"]
        self.gate = PROXY.enforcement.gates[CONTEXT_ID]
        self.ledger = PROXY.enforcement.ledgers[RATE_ID]
        patcher = patch.object(PROXY, "USAGE_SUPPORTED", True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def request(self, closing=False):
        transport = SimpleNamespace(is_closing=lambda: closing)
        return SimpleNamespace(
            method="POST",
            content_type="application/json",
            transport=transport,
            app={"client": SimpleNamespace()},
            headers={},
        )

    def cost(self):
        return PROXY.Cost(input=100, output=self.lane.output_clamp)

    async def test_retry_sleeps_with_no_permit_and_no_booking(self):
        observations = []
        calls = {"count": 0}
        first_response = SimpleNamespace()

        async def fake_attempt(_request, _session, lane, _body, _attempt):
            async with PROXY.admission(lane, PROXY.enforcement):
                calls["count"] += 1
                observations.append(("attempt", self.gate.active, self.ledger.committed))
                if calls["count"] >= 2:
                    return PROXY.Attempt(
                        response=first_response, output_tokens=7, prompt_tokens=11
                    )
                return PROXY.Attempt(retry_after=0.0)

        real_sleep = asyncio.sleep

        async def watchful_sleep(delay, *_args, **_kwargs):
            observations.append(("sleep", self.gate.active, self.ledger.committed))
            return await real_sleep(0)

        with (
            patch.object(PROXY, "stream_attempt", fake_attempt),
            patch.object(PROXY.asyncio, "sleep", watchful_sleep),
        ):
            result = await PROXY.forward_chat(
                self.request(),
                self.lane,
                b"{}",
                time.monotonic(),
                b"{}",
                self.cost(),
            )

        self.assertIs(result, first_response)
        attempts = [item for item in observations if item[0] == "attempt"]
        waits = [item for item in observations if item[0] == "sleep"]
        self.assertEqual(len(attempts), 2)
        self.assertEqual(len(waits), 1)

        # Each attempt ran while holding exactly one permit and one full booking.
        for _tag, active, committed in attempts:
            self.assertEqual(active, 1)
            self.assertEqual(committed, self.lane.output_clamp)

        # The backoff sleep itself held neither.
        for _tag, active, committed in waits:
            self.assertEqual(active, 0)
            self.assertEqual(committed, 0)

        self.assertEqual(self.ledger.committed, 7)

    async def test_request_deadline_stops_retrying(self):
        async def never_finish(*_args, **_kwargs):
            return PROXY.Attempt(retry_after=1.0)

        started = time.monotonic() - PROXY.MAX_REQUEST_SECONDS - 1
        before = PROXY.stats.deadline_stops
        with patch.object(PROXY, "stream_attempt", never_finish):
            with self.assertRaises(PROXY.web.HTTPBadGateway):
                await PROXY.forward_chat(
                    self.request(), self.lane, b"{}", started, b"{}", self.cost()
                )
        self.assertEqual(self.ledger.committed, 0)
        self.assertEqual(PROXY.stats.deadline_stops, before + 1)

    async def test_a_gone_client_stops_without_an_attempt(self):
        async def never_called(*_args, **_kwargs):  # pragma: no cover - must not run
            raise AssertionError("a disconnected client must not reach the provider")

        with patch.object(PROXY, "stream_attempt", never_called):
            with self.assertRaises(asyncio.CancelledError):
                await PROXY.forward_chat(
                    self.request(closing=True),
                    self.lane,
                    b"{}",
                    time.monotonic(),
                    b"{}",
                    self.cost(),
                )
        self.assertEqual((self.gate.active, self.ledger.committed), (0, 0))

    async def test_measured_output_settles_the_booking(self):
        response = SimpleNamespace()

        async def completed(*_args, **_kwargs):
            return PROXY.Attempt(response=response, output_tokens=1500, prompt_tokens=9000)

        with patch.object(PROXY, "stream_attempt", completed):
            result = await PROXY.forward_chat(
                self.request(),
                self.lane,
                b"{}",
                time.monotonic(),
                b"{}",
                self.cost(),
            )
        self.assertIs(result, response)
        self.assertEqual(self.ledger.committed, 1500)
        self.assertEqual(self.gate.active, 0)

    async def test_unmeasurable_usage_keeps_the_pessimistic_booking(self):
        response = SimpleNamespace()
        before = PROXY.stats.usage_unknown

        async def unreported(*_args, **_kwargs):
            return PROXY.Attempt(response=response)

        with patch.object(PROXY, "stream_attempt", unreported):
            await PROXY.forward_chat(
                self.request(),
                self.lane,
                b"{}",
                time.monotonic(),
                b"{}",
                self.cost(),
            )
        self.assertEqual(self.ledger.committed, self.lane.output_clamp)
        self.assertEqual(PROXY.stats.usage_unknown, before + 1)

    async def test_gateway_rejection_of_usage_is_retried_without_injection(self):
        calls = []

        async def reject_then_succeed(_request, _session, lane, body, attempt):
            calls.append(body)
            if len(calls) == 1:
                return PROXY.Attempt(retry_after=0.0, rejected_usage=True)
            return PROXY.Attempt(response=SimpleNamespace(), output_tokens=10, prompt_tokens=20)

        inbound = b'{"messages":[],"stream":true}'
        with (
            patch.object(PROXY, "REQUEST_USAGE", True),
            patch.object(PROXY, "stream_attempt", reject_then_succeed),
        ):
            result = await PROXY.forward_chat(
                self.request(),
                self.lane,
                PROXY.rewrite_model(inbound, Request(), self.lane),
                time.monotonic(),
                inbound,
                self.cost(),
            )
            # The self-heal has to persist for the life of the process, not just
            # this request; the patch restores the original value on exit.
            self.assertFalse(PROXY.USAGE_SUPPORTED)

        self.assertIsNotNone(result)
        self.assertEqual(len(calls), 2)
        self.assertIn("stream_options", json.loads(calls[0]))
        self.assertNotIn("stream_options", json.loads(calls[1]))


class InputGuardTests(unittest.TestCase):
    def test_base64_media_is_charged_as_media_not_as_text(self):
        payload = {"messages": [{"content": "data:image/png;base64," + "A" * 4_000_000}]}
        estimate, media = PROXY.estimate_input_tokens(json.dumps(payload).encode())
        self.assertEqual(media, 1)
        self.assertLess(estimate, 10000)

    def test_guard_rejects_only_gross_oversize_requests(self):
        guard = SUBAGENT.max_input * PROXY.INPUT_GUARD_PERCENT // 100
        oversized = json.dumps({"messages": [{"content": "x" * (guard * 4 + 4000)}]}).encode()
        estimate, _ = PROXY.estimate_input_tokens(oversized)
        self.assertGreater(estimate, guard)

    def test_a_full_but_legal_subagent_request_passes(self):
        guard = SUBAGENT.max_input * PROXY.INPUT_GUARD_PERCENT // 100
        legal = json.dumps({"messages": [{"content": "x" * (SUBAGENT.max_input * 4)}]}).encode()
        estimate, _ = PROXY.estimate_input_tokens(legal)
        self.assertLessEqual(estimate, guard)


class StreamOptionTests(unittest.TestCase):
    def setUp(self):
        patcher = patch.object(PROXY, "USAGE_SUPPORTED", True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def rewrite(self, payload: dict, lane=SUBAGENT):
        return json.loads(PROXY.rewrite_model(json.dumps(payload).encode(), Request(), lane))

    def test_usage_is_requested_only_for_streams(self):
        self.assertEqual(
            self.rewrite({"messages": [], "stream": True})["stream_options"],
            {"include_usage": True},
        )
        self.assertNotIn("stream_options", self.rewrite({"messages": []}))
        self.assertNotIn("stream_options", self.rewrite({"messages": [], "stream": False}))

    def test_client_stream_options_are_not_overwritten(self):
        body = self.rewrite(
            {"messages": [], "stream": True, "stream_options": {"include_usage": False}}
        )
        self.assertEqual(body["stream_options"], {"include_usage": False})

    def test_injection_stops_after_the_gateway_rejects_it(self):
        with patch.object(PROXY, "USAGE_SUPPORTED", False):
            self.assertNotIn("stream_options", self.rewrite({"messages": [], "stream": True}))

    def test_injection_stops_when_the_operator_turns_it_off(self):
        with patch.object(PROXY, "REQUEST_USAGE", False):
            self.assertNotIn("stream_options", self.rewrite({"messages": [], "stream": True}))

    def test_only_a_bad_request_can_disable_usage(self):
        self.assertTrue(
            PROXY.body_rejects_stream_usage(400, b'{"error":"Unsupported stream_options"}')
        )
        self.assertFalse(PROXY.body_rejects_stream_usage(429, b"Unsupported stream_options"))
        self.assertFalse(PROXY.body_rejects_stream_usage(400, b'{"error":"bad model"}'))


class UsageScannerTests(unittest.TestCase):
    @staticmethod
    def chunk(text: str) -> bytes:
        return text.encode()

    def test_usage_survives_a_split_mid_event(self):
        scanner = PROXY.UsageScanner()
        payload = (
            'data: {"choices":[{"delta":{"content":"hello"}}]}\n\n'
            'data: {"choices":[{"delta":{"content":" world"}}],'
            '"usage":{"prompt_tokens":1234,"completion_tokens":57}}\n\n'
            "data: [DONE]\n\n"
        )
        blob = self.chunk(payload)
        for index in range(0, len(blob), 7):
            scanner.feed(blob[index : index + 7])
        scanner.finish()
        self.assertEqual(scanner.completion_tokens, 57)
        self.assertEqual(scanner.prompt_tokens, 1234)
        self.assertEqual(scanner.text_chars, len("hello world"))
        self.assertIsNone(scanner.estimated_output())

    def test_reasoning_only_stream_is_still_measured(self):
        scanner = PROXY.UsageScanner()
        scanner.feed(
            self.chunk(
                'data: {"choices":[{"delta":{"reasoning_content":"thinking hard"}}]}\n\n'
            )
        )
        scanner.finish()
        self.assertEqual(scanner.estimated_output(), 4)  # 13 characters, no usage reported

    def test_events_without_usage_are_not_parsed_as_json(self):
        scanner = PROXY.UsageScanner()
        scanner.feed(self.chunk("data: not-json-at-all\n\n"))
        scanner.finish()
        self.assertIsNone(scanner.completion_tokens)

    def test_buffer_cap_degrades_instead_of_growing(self):
        scanner = PROXY.UsageScanner(cap=64)
        scanner.feed(self.chunk("x" * 5000))
        self.assertTrue(scanner.overflowed)
        scanner.feed(self.chunk('data: {"usage":{"completion_tokens":9}}\n\n'))
        self.assertIsNone(scanner.completion_tokens)
        self.assertIsNone(scanner.estimated_output())

    def test_json_completion_response_is_scanned(self):
        scanner = PROXY.UsageScanner()
        scanner.feed(
            self.chunk('data: {"choices":[{"message":{"content":"answer"}}],'
                       '"usage":{"completion_tokens":2}}\n\n')
        )
        scanner.finish()
        self.assertEqual(scanner.completion_tokens, 2)


class HealthTests(unittest.IsolatedAsyncioTestCase):
    """What the operator sees is the plan the proxy is actually enforcing."""

    def setUp(self):
        reset_policy_state()
        clean_counters()
        self.addCleanup(clean_counters)

    async def report(self, plan=None, mutate_config=None):
        with live(plan or mutated(), mutate_config):
            reset_policy_state()
            response = await PROXY.health(None)
        return response.status, json.loads(response.body)

    async def test_health_describes_the_resolved_envelope(self):
        status, body = await self.report()
        self.assertEqual(status, 200)
        self.assertTrue(body["policy_enforced"])
        self.assertEqual(body["credentials"], SECRET_NAMES)
        self.assertEqual(body["subagent_limit"], FAN_OUT)
        self.assertEqual(sorted(body["lanes"]), sorted(LANES))
        self.assertEqual(
            body["providers"], sorted({item["provider_name"] for item in PLAN["lanes"].values()})
        )
        self.assertEqual(body["counters"][CONTEXT_ID]["context_budget"], BUDGET)
        self.assertEqual(body["counters"][CONTEXT_ID]["exclusive_at"], EXCLUSIVE_AT)
        self.assertEqual(body["rates"][RATE_ID]["capacity"], RATE_CAPACITY)
        self.assertEqual(body["rates"][RATE_ID]["unit"], RATE_UNIT)
        self.assertFalse(body["lanes"]["primary"]["exclusive"])
        self.assertTrue(body["lanes"]["long"]["exclusive"])
        self.assertFalse(body["lanes"]["subagent"]["exclusive"])

    async def test_health_reports_no_credential_value(self):
        # The operator sees where each lane is served from, which is the plan's own
        # endpoint, and never the provider key that the proxy holds on the agent's behalf.
        _status, body = await self.report()
        for name, lane in body["lanes"].items():
            provider = PLAN["providers"][PLAN["lanes"][name]["provider"]]
            self.assertEqual(lane["endpoint"], provider["base_url"])
        self.assertNotIn(PROVIDER_KEY, json.dumps(body))

    async def test_drift_is_reported_as_an_error(self):
        def break_it(document):
            document["secondary_model"]["force"] = False

        status, body = await self.report(mutate_config=break_it)
        self.assertEqual(status, 503)
        self.assertEqual(body["status"], "policy-drift")
        self.assertIn("force must be true", body["policy_error"])

    async def test_models_endpoint_lists_only_the_lane_model(self):
        with live(mutated()):
            reset_policy_state()
            request = Request(headers={"Authorization": f"Bearer {INTERNAL_TOKEN}"})
            request.match_info = {"lane": "subagent"}
            response = await PROXY.models(request)
        self.assertEqual(
            json.loads(response.body)["data"], [{"id": SUBAGENT.model, "object": "model"}]
        )


class PlanIsTheSourceOfTruthTests(unittest.TestCase):
    """The proxy holds no model facts of its own, so the definitions decide everything."""

    def test_every_advertised_number_comes_from_the_plan(self):
        with live(mutated()):
            policy = PROXY.load_runtime_policy()
        for name, lane in policy.lanes.items():
            entry = PLAN["lanes"][name]
            with self.subTest(lane=name):
                self.assertEqual(lane.alias, entry["alias"])
                self.assertEqual(lane.model, entry["model"])
                self.assertEqual(lane.context, entry["context_tokens"])
                self.assertEqual(lane.max_input, entry["input_tokens"])
                self.assertEqual(lane.output_clamp, entry["output_clamp_tokens"])
                self.assertEqual(lane.reservation, entry["reservation"])

    def test_a_renamed_model_moves_the_envelope_without_touching_the_proxy(self):
        plan = mutated()
        for entry in plan["lanes"].values():
            entry["model"] = "Some Other Model"
            entry["alias"] = f"{entry['alias']}-x"
        plan["counters"][CONTEXT_ID]["subject"] = "Some Other Model"
        policy = loaded_policy(plan)
        self.assertEqual({lane.model for lane in policy.lanes.values()}, {"Some Other Model"})
        self.assertEqual(policy.lanes["subagent"].alias, f"{SUBAGENT.alias}-x")

    def test_a_provider_that_publishes_no_rate_rule_produces_no_ledger(self):
        plan = mutated()
        del plan["counters"][RATE_ID]
        for entry in plan["lanes"].values():
            entry["counters"] = [CONTEXT_ID]
        policy = loaded_policy(plan)
        enforcement = PROXY.Enforcement()
        enforcement.sync(policy)
        self.assertEqual(sorted(enforcement.ledgers), [])
        self.assertEqual(sorted(enforcement.gates), [CONTEXT_ID])

    def test_a_provider_that_publishes_only_a_concurrency_limit_becomes_a_gate(self):
        plan = mutated()
        count_id = "count:nrp/model"
        plan["counters"] = {
            count_id: {
                "id": count_id,
                "family": "count",
                "scope": "model",
                "subject": "fixture",
                "provider": "nrp",
                "lanes": sorted(LANES),
                "max": 3,
            }
        }
        for entry in plan["lanes"].values():
            entry["counters"] = [count_id]
        policy = loaded_policy(plan)
        enforcement = PROXY.Enforcement()
        enforcement.sync(policy)
        gate = enforcement.gates[count_id]
        self.assertEqual(gate.limit, 3)
        self.assertIsNone(gate.budget)
        self.assertIsNone(gate.exclusive_at)
        self.assertFalse(gate.is_exclusive(10**9))
        PROXY.validate_policy(policy)

    def test_a_different_fan_out_produces_a_different_swarm(self):
        # The gate has no idea what a subagent is worth; it only enforces the numbers the
        # plan carries, so a provider that allows a bigger swarm gets a bigger swarm here.
        plan = mutated()
        bigger = FAN_OUT + 3
        plan["counters"][CONTEXT_ID]["budget"] = bigger * SUBAGENT_RESERVATION
        plan["counters"][CONTEXT_ID]["ceiling"] = bigger * SUBAGENT_RESERVATION
        plan["limits"]["subagent_concurrency"] = bigger
        policy = loaded_policy(plan)
        PROXY.validate_policy(policy)

        enforcement = PROXY.Enforcement()
        enforcement.sync(policy)
        self.assertEqual(enforcement.gates[CONTEXT_ID].subagent_limit, bigger)
        self.assertGreater(bigger, FAN_OUT)


class PromptArchiveTests(unittest.IsolatedAsyncioTestCase):
    """One file and one stdout line per new user prompt, through the real chat handler."""

    HOST_DIR = "/host/prompt-log"

    def setUp(self):
        reset_policy_state()
        clean_counters()
        self.addCleanup(clean_counters)

        storage = tempfile.TemporaryDirectory()
        self.addCleanup(storage.cleanup)
        self.directory = Path(storage.name) / "prompt-log"

        for name, value in (
            ("PROMPT_LOG_DIR", self.directory),
            ("PROMPT_LOG_HOST_DIR", Path(self.HOST_DIR)),
        ):
            patcher = patch.object(PROXY, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

        # The archive remembers what it has already written, and that memory is the
        # module's, not the test's.
        PROXY._archived.clear()
        self.addCleanup(PROXY._archived.clear)

        self.captured = io.StringIO()
        patcher = patch.object(sys, "stdout", self.captured)
        patcher.start()
        self.addCleanup(patcher.stop)

        async def deliver(_request, _lane, _body, _started, _inbound, _cost):
            return PROXY.web.Response(text="ok")

        patcher = patch.object(PROXY, "forward_chat", deliver)
        patcher.start()
        self.addCleanup(patcher.stop)

    def request(self, messages) -> SimpleNamespace:
        body = json.dumps({"model": "anything", "messages": messages}).encode()
        return SimpleNamespace(
            method="POST",
            content_type="application/json",
            headers={"Authorization": f"Bearer {INTERNAL_TOKEN}"},
            match_info={"lane": "subagent"},
            query_string="",
            rel_url="/subagent/v1/chat/completions",
            transport=SimpleNamespace(is_closing=lambda: False),
            app={},
            read=lambda: asyncio.sleep(0, result=body),
        )

    async def send(self, messages) -> list[Path]:
        """Serve one chat request and report what the archive holds afterwards."""
        await PROXY.chat(self.request(messages))
        return sorted(self.directory.iterdir()) if self.directory.exists() else []

    @property
    def output(self) -> str:
        return self.captured.getvalue()

    async def test_a_new_user_prompt_becomes_one_file_and_one_line(self):
        prompt = "Add a caching layer to the session lookup"
        files = await self.send(
            [
                {"role": "system", "content": "You are an agent."},
                {"role": "user", "content": prompt},
            ]
        )

        self.assertEqual(len(files), 1)
        dump = files[0].read_text()
        self.assertTrue(dump.startswith("POST "))
        self.assertIn("/v1/chat/completions", dump)
        self.assertIn(prompt, dump)
        self.assertIn('\n  "messages": [', dump, "the body is pretty-printed, not compact")

        line = self.output.strip()
        self.assertIn("prompt_log lane=subagent", line)
        self.assertRegex(line, r"time=\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")
        self.assertIn(f"file={self.HOST_DIR}/{files[0].name}", line, "the host path is printed")
        self.assertIn(prompt, line)

    async def test_the_stdout_summary_is_truncated_and_stays_on_one_line(self):
        prompt = "the first line\n" + "x" * 200
        await self.send([{"role": "user", "content": prompt}])

        summary = self.output.strip().split("prompt='", 1)[1].removesuffix("'")
        self.assertEqual(
            len(summary),
            PROXY.PROMPT_SUMMARY_CHARS + 1,
            "65 characters of prompt, then an ellipsis",
        )
        self.assertTrue(summary.endswith("…"))
        self.assertNotIn("\n", summary, "a prompt with newlines cannot break the line")

    async def test_a_harness_reminder_is_not_a_prompt(self):
        files = await self.send(
            [
                {"role": "user", "content": "Fix the flaky test"},
                {"role": "assistant", "content": "Reading the failure now."},
                {
                    "role": "user",
                    "content": "<system-reminder>Today's date is 2026-09-21.</system-reminder>",
                },
            ]
        )

        self.assertEqual(files, [])
        self.assertEqual(self.output.strip(), "")

    async def test_the_rest_of_a_turn_adds_nothing_and_the_next_prompt_does(self):
        prompt = "Refactor the auth module"
        opening = [{"role": "user", "content": prompt}]
        self.assertEqual(len(await self.send(opening)), 1)

        # Later steps of the same turn re-send the whole conversation, tool traffic included.
        grown = opening + [
            {"role": "assistant", "content": "Reading."},
            {"role": "tool", "tool_call_id": "1", "content": "the file's contents"},
        ]
        self.assertEqual(len(await self.send(grown)), 1, "a step is not a new prompt")

        next_prompt = grown + [{"role": "user", "content": "Now add tests"}]
        self.assertEqual(len(await self.send(next_prompt)), 2)

    async def test_a_content_parts_prompt_is_archived_by_its_text(self):
        files = await self.send(
            [
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
                        {"type": "text", "text": "What is in this screenshot?"},
                    ],
                }
            ]
        )

        self.assertEqual(len(files), 1)
        self.assertIn("What is in this screenshot?", self.output)
        self.assertIn("data:image/png;base64,AAAA", files[0].read_text(), "the file holds the body")

    async def test_the_archive_holds_neither_credential_nor_cache_salt(self):
        files = await self.send([{"role": "user", "content": "Any prompt at all"}])

        dump = files[0].read_text()
        self.assertNotIn(CACHE_SALT, dump)
        self.assertNotIn(INTERNAL_TOKEN, dump)
        self.assertNotIn(PROVIDER_KEY, dump)
        self.assertIn("Authorization: <REDACTED>", dump)
        self.assertIn('"cache_salt": "<REDACTED>"', dump)

    async def test_the_archive_is_empty_at_both_ends_of_a_run(self):
        await self.send([{"role": "user", "content": "First prompt"}])
        self.assertEqual(len(list(self.directory.iterdir())), 1)

        await PROXY.close_prompt_archive(None)
        self.assertTrue(self.directory.is_dir(), "the mount point outlives its contents")
        self.assertEqual(list(self.directory.iterdir()), [])

        # A container that was killed never got to purge, so the next one does.
        (self.directory / "prompt-stale.txt").write_text("stale")
        await PROXY.open_prompt_archive(None)
        self.assertEqual(list(self.directory.iterdir()), [])

    async def test_a_failing_archive_still_serves_the_request(self):
        obstruction = self.directory.parent / "not-a-directory"
        obstruction.write_text("a file where a parent directory should be")

        with patch.object(PROXY, "PROMPT_LOG_DIR", obstruction / "prompt-log"):
            response = await PROXY.chat(self.request([{"role": "user", "content": "Serve me"}]))

        self.assertEqual(response.status, 200)
        self.assertIn("prompt_log lane=subagent error=", self.output)


if __name__ == "__main__":
    unittest.main()
