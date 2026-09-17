#!/usr/bin/env python3
"""Strict OpenAI-compatible provider-policy proxy.

The enforcement envelope is not configured here. It is resolved at launch from the model
definitions in ``./models`` and the provider rules in ``./providers``, written to
``model-policy.json``, and mounted into this container. This module reads that plan and
refuses traffic it cannot fit inside it, which is why no model window, context percentage,
or provider name appears below.

A plan carries three counter families, and each counter becomes one live object:

* ``context`` - worst-case in-flight tokens for one subject. Admission compares the sum of
  live reservations with the provider's aggregate budget, and a single request at or above
  the provider's exclusivity threshold runs alone. A fair-use rule such as "all concurrent
  requests combined stay inside 35% of the context window" lives here.
* ``count`` - a plain concurrency ceiling on a model, credential, or provider subject.
* ``rate`` - a rolling-window budget in one metered unit. ``RateLedger`` books each request's
  full expected charge in that unit before it starts and settles it against measured usage, so
  the proxy paces itself instead of waiting to be rejected with HTTP 429.

A request holds a slot in every counter its lane names. Counters are always taken in
sorted counter-id order, so no two requests can pick them up in opposite orders and
deadlock. Fair-use permits and rate bookings are always released before a backoff sleep,
so waiting on a provider never consumes capacity.

Both the plan and the rendered Kimi configuration that plan was compiled into are re-read
on the request path: drift stops the affected traffic instead of being served under stale
assumptions.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import os
import re
import secrets
import time
import tomllib
from collections import deque
from collections.abc import AsyncIterator
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from pathlib import Path

from aiohttp import (
    ClientConnectionError,
    ClientSession,
    ClientTimeout,
    ServerDisconnectedError,
    web,
)

DEBUG_HTTP = False

RATE_WINDOW_SECONDS = 60.0

#: Units a rate ledger may meter, mirroring the plan's ``unit`` field.
LEDGER_UNITS = frozenset({"output_tokens", "input_tokens", "total_tokens", "requests"})


def secret(file_variable: str, value_variable: str, *, min_length: int = 32) -> str:
    path = os.environ.get(file_variable, "")
    value = (Path(path).read_text() if path else os.environ.get(value_variable, "")).strip()
    if len(value) < min_length:
        raise RuntimeError(
            f"{file_variable} must reference a secret of at least {min_length} characters"
        )
    return value


#: The plan schema this proxy understands; ``tools/policy.py`` publishes the matching constant.
PLAN_SCHEMA_VERSION = 1

#: The resolved plan this proxy enforces, and the Kimi configuration that same plan was
#: rendered into. Both are read on the request path, so a mismatch fails closed.
POLICY_PATH = os.environ.get("MODEL_PROXY_POLICY", "/policy/model-policy.json")
KIMI_CONFIG_PATH = os.environ.get("KIMI_CONFIG_PATH", "/policy/kimi-config.toml")
#: Provider credentials are mounted one file per credential, named by the plan.
SECRETS_DIR = os.environ.get("MODEL_PROXY_SECRETS_DIR", "/run/secrets")
INTERNAL_BEARER_TOKEN = secret("MODEL_PROXY_INTERNAL_TOKEN_FILE", "MODEL_PROXY_INTERNAL_TOKEN")
CACHE_SALT = secret("MODEL_PROXY_CACHE_SALT_FILE", "MODEL_PROXY_CACHE_SALT")

MAX_REQUEST_BYTES = int(os.environ.get("MODEL_PROXY_MAX_REQUEST_BYTES", str(256 * 1024**2)))
MAX_RESPONSE_BYTES = int(os.environ.get("MODEL_PROXY_MAX_RESPONSE_BYTES", str(512 * 1024**2)))
MAX_ERROR_BYTES = int(os.environ.get("MODEL_PROXY_MAX_ERROR_BYTES", str(64 * 1024)))
MAX_QUEUED = int(os.environ.get("MODEL_PROXY_MAX_QUEUED", "32"))

# A stalled upstream read must release its permit rather than wedge the lane.
SOCK_READ_TIMEOUT = float(os.environ.get("MODEL_PROXY_SOCK_READ_TIMEOUT", "300"))
MAX_REQUEST_SECONDS = float(os.environ.get("MODEL_PROXY_MAX_REQUEST_SECONDS", "3600"))

# Coarse per-lane input ceiling, as a percentage of the lane input cap. It exists
# to catch configuration drift and abuse, not to meter legitimate traffic.
INPUT_GUARD_PERCENT = int(os.environ.get("MODEL_PROXY_INPUT_GUARD_PERCENT", "150"))
MEDIA_TOKEN_ESTIMATE = int(os.environ.get("MODEL_PROXY_MEDIA_TOKEN_ESTIMATE", "1600"))

# Whether the gateway still accepts usage reporting in streamed responses.
REQUEST_USAGE = os.environ.get("MODEL_PROXY_REQUEST_USAGE", "1").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
USAGE_SUPPORTED = True

RETRYABLE = {429, 500, 502, 503, 504}

SENSITIVE_HEADERS = {
    "authorization",
    "proxy-authorization",
    "x-api-key",
    "cookie",
    "set-cookie",
}

DATA_URL = re.compile(rb"data:[\w.+-]+/[\w.+-]+;base64,[A-Za-z0-9+/=]*")


def debug_headers(headers) -> dict[str, str]:
    return {
        name: "<REDACTED>" if name.lower() in SENSITIVE_HEADERS else value
        for name, value in headers.items()
    }


def debug_body(body: bytes, *, redact_cache_salt: bool = False) -> str:
    if not body:
        return "<empty>"

    try:
        payload = json.loads(body)

        if redact_cache_salt and isinstance(payload, dict) and "cache_salt" in payload:
            payload["cache_salt"] = "<REDACTED>"

        return json.dumps(payload, indent=2, ensure_ascii=False)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return body.decode("utf-8", errors="replace")


def debug_http(
    title: str,
    *,
    method: str | None = None,
    url: str | None = None,
    status: int | None = None,
    headers=None,
    body: bytes | None = None,
    redact_cache_salt: bool = False,
) -> None:
    if not DEBUG_HTTP:
        return

    print("\n" + "=" * 80, flush=True)
    print(title, flush=True)

    if method is not None:
        print(f"METHOD: {method}", flush=True)

    if url is not None:
        print(f"URL: {url}", flush=True)

    if status is not None:
        print(f"STATUS: {status}", flush=True)

    if headers is not None:
        print("HEADERS:", flush=True)
        for name, value in debug_headers(headers).items():
            print(f"  {name}: {value}", flush=True)

    if body is not None:
        print("BODY:", flush=True)
        print(
            debug_body(body, redact_cache_salt=redact_cache_salt),
            flush=True,
        )

    print("=" * 80 + "\n", flush=True)


# ============================================================
# The resolved plan, and the Kimi configuration rendered from it
# ============================================================


class PolicyDrift(RuntimeError):
    """The mounted plan, or the Kimi configuration rendered from it, is not enforceable."""


@dataclass(frozen=True)
class LanePolicy:
    """One serving lane: what it costs, where its traffic goes, who admits it."""

    name: str
    alias: str
    provider_name: str
    model: str
    base_url: str
    secret_name: str
    context: int
    max_input: int
    output_clamp: int
    reserved: int
    counters: tuple[str, ...]

    @property
    def reservation(self) -> int:
        """Worst-case context this lane occupies while in flight."""
        return self.max_input + self.output_clamp


@dataclass(frozen=True)
class RuntimePolicy:
    lanes: dict[str, LanePolicy]
    counters: dict[str, dict]
    limits: dict
    reserved: int
    providers: tuple[str, ...]


def _positive(entry: dict, key: str, where: str) -> int:
    value = entry.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise PolicyDrift(f"{where}.{key} must be a positive integer")
    return value


def load_plan(path: str | None = None) -> dict:
    """Read the plan the launcher resolved from ``./models`` and ``./providers``."""
    location = path or POLICY_PATH
    try:
        with open(location, encoding="utf-8") as source:
            plan = json.load(source)
    except (OSError, ValueError) as exc:
        raise PolicyDrift(f"cannot read model policy {location}: {exc}") from exc
    if not isinstance(plan, dict) or plan.get("schema_version") != PLAN_SCHEMA_VERSION:
        raise PolicyDrift(f"model policy must be schema {PLAN_SCHEMA_VERSION}")
    if not isinstance(plan.get("lanes"), dict) or not plan["lanes"]:
        raise PolicyDrift("model policy declares no lanes")
    if not isinstance(plan.get("providers"), dict) or not plan["providers"]:
        raise PolicyDrift("model policy declares no providers")
    return plan


def upstream_credential(secret_name: str) -> str:
    """Provider key for one credential, read from the file the launcher mounted for it.

    Names come from the plan, which was generated from operator-owned definitions, and are
    constrained here so a definition can never reach outside the secrets directory.
    """
    if not re.fullmatch(r"[a-z0-9_]{1,64}", secret_name):
        raise PolicyDrift(f"invalid credential name {secret_name!r}")
    try:
        value = (Path(SECRETS_DIR) / secret_name).read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise PolicyDrift(f"credential {secret_name} is not mounted: {exc}") from exc
    if not value:
        raise PolicyDrift(f"credential {secret_name} is empty")
    return value


def _lane_from_plan(name: str, entry: dict, providers: dict, reserved: int) -> LanePolicy:
    provider = providers.get(entry.get("provider"))
    if not isinstance(provider, dict):
        raise PolicyDrift(
            f"lane {name} names provider {entry.get('provider')!r}, which the plan omits"
        )
    credential = (provider.get("credentials") or {}).get(entry.get("credential"))
    if not isinstance(credential, dict):
        raise PolicyDrift(
            f"lane {name} names credential {entry.get('credential')!r}, which the plan omits"
        )
    base_url = str(provider.get("base_url", "")).rstrip("/")
    if not base_url.startswith(("http://", "https://")):
        raise PolicyDrift(f"provider {entry['provider']} publishes no usable endpoint")
    return LanePolicy(
        name=name,
        alias=str(entry["alias"]),
        provider_name=str(entry["provider_name"]),
        model=str(entry["model"]),
        base_url=base_url,
        secret_name=str(credential["secret_name"]),
        context=_positive(entry, "context_tokens", f"lane {name}"),
        max_input=_positive(entry, "input_tokens", f"lane {name}"),
        output_clamp=_positive(entry, "output_clamp_tokens", f"lane {name}"),
        reserved=reserved,
        counters=tuple(sorted(entry.get("counters") or ())),
    )


def enforce_kimi_configuration(policy: RuntimePolicy, config_path: str | None = None) -> None:
    """Prove the live Kimi configuration is still the one this plan produced.

    The plan is authoritative because it is what the operator's definitions resolved to. The
    rendered configuration is cross-checked rather than trusted: an alias Kimi does not offer,
    a lane sized differently than the plan priced it, or a lost subagent binding is drift, and
    drift stops traffic instead of sending requests the plan cannot account for.
    """
    try:
        with open(config_path or KIMI_CONFIG_PATH, "rb") as source:
            config = tomllib.load(source)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise PolicyDrift(f"cannot read the rendered Kimi configuration: {exc}") from exc

    reserve = int((config.get("loop_control") or {}).get("reserved_context_size", 0))
    if reserve != policy.reserved:
        raise PolicyDrift(
            f"Kimi reserves {reserve} output tokens per step but the plan budgeted "
            f"{policy.reserved}"
        )

    models = config.get("models") or {}
    for name, lane in policy.lanes.items():
        model = models.get(lane.alias)
        if not isinstance(model, dict):
            raise PolicyDrift(f"Kimi does not offer {lane.alias!r}, the model for lane {name}")
        if model.get("provider") != lane.provider_name:
            raise PolicyDrift(
                f"Kimi binds {lane.alias!r} to provider {model.get('provider')!r}, "
                f"not the plan's {lane.provider_name!r}"
            )
        for key, want in (
            ("max_context_size", lane.context),
            ("max_input_size", lane.max_input),
        ):
            if model.get(key) != want:
                raise PolicyDrift(
                    f"Kimi sizes {lane.alias}.{key} at {model.get(key)!r}, "
                    f"the plan enforces {want}"
                )

    secondary = config.get("secondary_model") or {}
    if secondary.get("force") is not True:
        raise PolicyDrift("Kimi secondary_model.force must be true")
    subagent = policy.lanes.get("subagent")
    if subagent is not None and secondary.get("default_model") != subagent.alias:
        raise PolicyDrift(
            f"forced secondary model {secondary.get('default_model')!r} is not {subagent.alias!r}"
        )
    primary = policy.lanes.get("primary")
    if primary is not None and config.get("default_model") != primary.alias:
        raise PolicyDrift(
            f"Kimi default model {config.get('default_model')!r} is not {primary.alias!r}"
        )


def load_runtime_policy(config_path: str | None = None) -> RuntimePolicy:
    """Build enforcement state from the plan, then cross-check the live Kimi configuration."""
    plan = load_plan()
    reserved = _positive(plan, "reserved_context_size", "model policy")
    lanes = {
        name: _lane_from_plan(name, entry, plan["providers"], reserved)
        for name, entry in plan["lanes"].items()
    }
    for name in lanes:
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,31}", name):
            raise PolicyDrift(f"lane name {name!r} cannot be used in a route")
    policy = RuntimePolicy(
        lanes=lanes,
        counters={
            key: dict(value)
            for key, value in (plan.get("counters") or {}).items()
            if isinstance(value, dict)
        },
        limits=dict(plan.get("limits") or {}),
        reserved=reserved,
        providers=tuple(sorted({lane.provider_name for lane in lanes.values()})),
    )
    enforce_kimi_configuration(policy, config_path)
    return policy


def counter_limits(counter: dict) -> tuple[int | None, int | None, int | None]:
    """Return (context budget, exclusivity threshold, request limit) a counter imposes."""
    family = counter.get("family")
    if family == "context":
        return counter.get("budget"), counter.get("exclusive_at"), counter.get("max")
    if family == "count":
        return None, None, counter.get("max")
    return None, None, None


def validate_policy(policy: RuntimePolicy | None = None) -> None:
    """Fail startup unless every planned lane and every configured knob is usable.

    Nothing here decides a limit; the plan already carries the numbers the provider rules
    produced. This proves the plan is internally servable, so a definition that could only
    ever starve a lane stops the proxy at startup instead of at the first request.
    """
    policy = policy or BASELINE_POLICY
    numeric_values = {
        "MODEL_PROXY_MAX_REQUEST_BYTES": MAX_REQUEST_BYTES,
        "MODEL_PROXY_MAX_RESPONSE_BYTES": MAX_RESPONSE_BYTES,
        "MODEL_PROXY_MAX_ERROR_BYTES": MAX_ERROR_BYTES,
        "MODEL_PROXY_MAX_QUEUED": MAX_QUEUED,
        "MODEL_PROXY_SOCK_READ_TIMEOUT": SOCK_READ_TIMEOUT,
        "MODEL_PROXY_MAX_REQUEST_SECONDS": MAX_REQUEST_SECONDS,
        "MODEL_PROXY_INPUT_GUARD_PERCENT": INPUT_GUARD_PERCENT,
        "MODEL_PROXY_MEDIA_TOKEN_ESTIMATE": MEDIA_TOKEN_ESTIMATE,
    }
    for name, value in numeric_values.items():
        if value <= 0:
            raise RuntimeError(f"{name} must be positive; got {value}")
    if INPUT_GUARD_PERCENT < 100:
        raise RuntimeError("MODEL_PROXY_INPUT_GUARD_PERCENT must be at least 100")

    for lane_name, lane in policy.lanes.items():
        if lane.max_input + lane.reserved > lane.context:
            raise RuntimeError(
                f"{lane.alias} ({lane_name}): {lane.max_input} input + {lane.reserved} reserved "
                f"does not fit its {lane.context}-token window"
            )
        if lane.max_input + lane.output_clamp > lane.context:
            raise RuntimeError(
                f"{lane.alias} ({lane_name}): {lane.max_input} input + {lane.output_clamp} "
                f"output clamp does not fit its {lane.context}-token window"
            )
        for counter_id in lane.counters:
            counter = policy.counters.get(counter_id)
            if counter is None:
                raise RuntimeError(f"{lane.alias} names unknown counter {counter_id}")
            budget, exclusive_at, _limit = counter_limits(counter)
            if budget is None:
                continue
            aggregate = counter.get("ceiling")
            if isinstance(aggregate, int) and budget > aggregate:
                raise RuntimeError(
                    f"counter {counter_id} budgets {budget} tokens above the "
                    f"{aggregate}-token aggregate ceiling its provider publishes"
                )
            # A reservation that reaches the exclusivity threshold may exceed the budget: the
            # provider's own terms allow a request that large to run, provided nothing else
            # runs beside it. Below that threshold, a lane that cannot fit inside the budget
            # could never be admitted at all.
            if lane.reservation > budget and not (
                exclusive_at is not None and lane.reservation >= exclusive_at
            ):
                raise RuntimeError(
                    f"{lane.alias} reserves {lane.reservation} tokens but counter {counter_id} "
                    f"only budgets {budget}; no request on this lane could ever be admitted"
                )

    subagent = policy.lanes.get("subagent")
    fan_out = policy.limits.get("subagent_concurrency")
    if subagent is not None and isinstance(fan_out, int) and fan_out > 0:
        for counter_id in subagent.counters:
            budget = policy.counters[counter_id].get("budget")
            if budget is not None and fan_out * subagent.reservation > budget:
                raise RuntimeError(
                    f"{fan_out} subagent reservations of {subagent.reservation} exceed the "
                    f"{budget}-token budget on counter {counter_id}"
                )


_policy_cache: tuple[object, object, RuntimePolicy] | None = None
_policy_error: str | None = None
_policy_error_logged = False


def _file_stamp(path: str) -> tuple[int, int]:
    stat = os.stat(path)
    return (int(stat.st_mtime_ns), stat.st_size)


def _refuse(detail: str, cause: BaseException) -> PolicyDrift:
    """Log one line per distinct policy failure and return the error the caller raises."""
    global _policy_error, _policy_error_logged
    if detail != _policy_error or not _policy_error_logged:
        print(f"policy_drift error={detail}", flush=True)
        _policy_error_logged = True
    _policy_error = detail
    error = PolicyDrift(detail)
    error.__cause__ = cause
    return error


def current_policy() -> RuntimePolicy:
    """Return the enforced policy, re-reading either input whenever it has changed.

    Fails closed: a plan or a configuration the proxy cannot honour stops the affected
    traffic rather than being served under the last known-good policy.
    """
    global _policy_cache, _policy_error, _policy_error_logged

    try:
        stamp = (_file_stamp(POLICY_PATH), _file_stamp(KIMI_CONFIG_PATH))
    except OSError as exc:
        raise _refuse(f"policy inputs are unreadable: {exc}", exc) from exc

    if _policy_cache is not None and _policy_cache[0:2] == stamp:
        return _policy_cache[2]

    try:
        policy = load_runtime_policy()
        validate_policy(policy)
    except (KeyError, TypeError, ValueError, PolicyDrift, RuntimeError) as exc:
        raise _refuse(str(exc), exc) from exc

    if _policy_error is not None:
        print("policy_drift_clear", flush=True)
    _policy_error = None
    _policy_error_logged = False
    _policy_cache = (stamp[0], stamp[1], policy)
    # Counter numbers move with the plan; the objects holding live reservations do not.
    enforcement.sync(policy)
    return policy


BASELINE_POLICY = load_runtime_policy()
validate_policy(BASELINE_POLICY)


def estimate_input_tokens(body: bytes) -> tuple[int, int]:
    """Rough input-token count, charging media separately instead of by base64 length.

    Base64 payloads are ~4/3 characters per source byte but only a few hundred
    model tokens, so counting them as text would reject valid multimodal requests.
    """
    media = 0

    def _drop(match: re.Match[bytes]) -> bytes:
        nonlocal media
        media += 1
        return b""

    stripped = DATA_URL.sub(_drop, body)
    return len(stripped) // 4 + media * MEDIA_TOKEN_ESTIMATE, media


def authorize_client(request: web.Request) -> None:
    supplied = request.headers.get("Authorization", "")
    if not hmac.compare_digest(supplied, f"Bearer {INTERNAL_BEARER_TOKEN}"):
        raise web.HTTPUnauthorized()


def _object_no_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def rewrite_model(body: bytes, request: web.Request, lane: LanePolicy) -> bytes:
    """Validate a chat request and rebind it to the model and clamp this lane was sold.

    The client's alias is deliberately discarded: the route a request arrived on is the
    only thing that decides which model it reaches, so a body cannot ask for a lane with a
    bigger window or a longer output than the reservation that was charged for it.
    """
    if request.method != "POST" or request.content_type != "application/json":
        raise web.HTTPUnsupportedMediaType(text="chat requests require application/json")
    try:
        payload = json.loads(
            body,
            object_pairs_hook=_object_no_duplicates,
            parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
        )
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
        raise web.HTTPBadRequest(text="invalid JSON request") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("messages"), list):
        raise web.HTTPBadRequest(text="request must contain a messages array")
    if "model" in payload and not isinstance(payload["model"], str):
        raise web.HTTPBadRequest(text="model must be a string")
    limits = [name for name in ("max_tokens", "max_completion_tokens") if name in payload]
    if len(limits) == 2 and payload[limits[0]] != payload[limits[1]]:
        raise web.HTTPBadRequest(text="conflicting output token limits")
    for name in limits:
        value = payload[name]
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise web.HTTPBadRequest(text=f"{name} must be a positive integer")
        payload[name] = min(value, lane.output_clamp)
    if not limits:
        payload["max_completion_tokens"] = lane.output_clamp
    # Output tokens are what the provider meters itself, so measured usage beats estimation.
    if REQUEST_USAGE and USAGE_SUPPORTED and payload.get("stream") is True:
        payload.setdefault("stream_options", {"include_usage": True})
    payload["model"] = lane.model
    payload["cache_salt"] = CACHE_SALT
    return json.dumps(payload, separators=(",", ":"), allow_nan=False).encode()


def body_rejects_stream_usage(status: int, body: bytes) -> bool:
    return status == 400 and b"stream_options" in body


# ============================================================
# Rate counters: booked usage in a rolling window
# ============================================================


@dataclass(frozen=True)
class Cost:
    """What one request may cost, in every unit a provider might meter it in.

    The plan says which unit each ledger reads; this object is the one place that knows how
    much a request charges in each of them, so adding a metered unit is a change here and in
    the definition taxonomy rather than a search through the admission path.
    """

    input: int
    output: int

    def charge(self, unit: str) -> int:
        if unit == "requests":
            return 1
        if unit == "input_tokens":
            return self.input
        if unit == "total_tokens":
            return self.input + self.output
        return self.output

    def credit(self, unit: str, outcome: Attempt) -> int | None:
        """Measured cost once the response finished.

        ``None`` means nothing was measured in that unit, and the pessimistic charge stands
        for the rest of the window - the safe direction to be wrong in.
        """
        if unit == "requests":
            return 1
        if unit == "input_tokens":
            return outcome.prompt_tokens
        if unit == "total_tokens":
            if outcome.prompt_tokens is None or outcome.output_tokens is None:
                return None
            return outcome.prompt_tokens + outcome.output_tokens
        if outcome.output_tokens is not None:
            return outcome.output_tokens
        return outcome.estimated_output


@dataclass(eq=False)
class Booking:
    """One entry in a rolling rate window, charged in that ledger's unit.

    Identity comparison is deliberate: two bookings for the same amount and expiry
    must not be interchangeable when one of them is settled.
    """

    expires_at: float
    amount: int
    settled: bool = False
    pruned: bool = False


class RateLedger:
    """Rolling-window cap on one metered unit - output tokens, input tokens, total, or requests.

    A request books the largest charge its lane could plausibly incur before it starts,
    because its eventual size is genuinely unknown, and is then settled to measured usage.
    The gateway's own remaining-quota header wins whenever it reports less headroom than we
    do, for the units the header actually describes.
    """

    def __init__(
        self,
        capacity: int,
        unit: str = "output_tokens",
        *,
        window: float = RATE_WINDOW_SECONDS,
        clock=time.monotonic,
    ) -> None:
        if unit not in LEDGER_UNITS:
            raise RuntimeError(f"unknown rate unit {unit!r}")
        self.capacity = capacity
        self.unit = unit
        self.window = window
        self.clock = clock
        self.bookings: deque[Booking] = deque()
        self.committed = 0
        self.server_remaining: int | None = None
        self.server_expires_at: float = 0.0

    def charge(self, cost: Cost) -> int:
        """What one request books against this ledger, before any usage is known."""
        return cost.charge(self.unit)

    def credit(self, cost: Cost, outcome: Attempt) -> int | None:
        """Measured cost once a response is done; ``None`` keeps the pessimistic charge."""
        return cost.credit(self.unit, outcome)

    def _insert(self, booking: Booking) -> None:
        self.bookings.append(booking)
        self.bookings = deque(sorted(self.bookings, key=lambda item: item.expires_at))
        self.committed += booking.amount

    def _prune(self, now: float) -> None:
        while self.bookings and self.bookings[0].expires_at <= now:
            booking = self.bookings.popleft()
            self.committed -= booking.amount
            booking.pruned = True
        if self.server_remaining is not None and self.server_expires_at <= now:
            self.server_remaining = None

    def projected(self) -> int:
        self._prune(self.clock())
        return self.committed

    def available(self) -> int:
        now = self.clock()
        self._prune(now)
        room = self.capacity - self.committed
        if self.server_remaining is not None:
            room = min(room, self.server_remaining)
        return max(0, room)

    def note_server(self, headers) -> None:
        """Let the endpoint's own headroom win, for the unit it actually reports.

        A gateway quota header describes one meter: request-count headers for a request
        ledger, token-count headers for an output-token ledger. A ledger whose unit no
        header describes - an input-token cap on a gateway that meters output - keeps its
        own pessimistic bookings rather than borrowing a number that means something else.
        """
        if self.unit == "requests":
            remaining_key, reset_key = (
                "x-ratelimit-requests-remaining",
                "x-ratelimit-requests-reset",
            )
        elif self.unit == "output_tokens":
            remaining_key, reset_key = "x-ratelimit-remaining", "x-ratelimit-reset"
        else:
            return
        remaining = headers.get(remaining_key)
        if remaining is None:
            return
        try:
            value = int(float(remaining))
        except ValueError:
            return
        reset = headers.get(reset_key)
        try:
            seconds = float(reset) if reset is not None else RATE_WINDOW_SECONDS
        except ValueError:
            seconds = RATE_WINDOW_SECONDS
        now = self.clock()
        self.server_remaining = max(0, value)
        self.server_expires_at = now + min(max(seconds, 1.0), RATE_WINDOW_SECONDS * 2)

    def reserve(self, amount: int) -> Booking | None:
        now = self.clock()
        self._prune(now)
        if self.available() < amount:
            return None
        booking = Booking(expires_at=now + self.window, amount=amount)
        self._insert(booking)
        return booking

    def settle(self, booking: Booking | None, actual: int | None) -> None:
        """Replace a reservation with measured output, or drop it if nothing ran."""
        if booking is None or booking.settled:
            return
        booking.settled = True
        if actual is None:
            return  # Keep the pessimistic booking for the rest of the window.
        now = self.clock()
        self._prune(now)
        if not booking.pruned:
            # Still inside the window: withdraw the pessimistic amount first.
            with contextlib.suppress(ValueError):
                self.bookings.remove(booking)
            self.committed -= booking.amount
        if actual <= 0:
            return
        booking.amount = actual
        booking.expires_at = now + self.window
        self._insert(booking)

    def wait_seconds(self) -> float:
        now = self.clock()
        self._prune(now)
        pending = [item.expires_at - now for item in self.bookings if item.expires_at > now]
        if self.server_remaining is not None:
            pending.append(self.server_expires_at - now)
        if not pending:
            return 1.0
        return min(60.0, max(1.0, min(pending)))

    def snapshot(self) -> dict:
        return {
            "capacity": self.capacity,
            "unit": self.unit,
            "committed": self.projected(),
            "available": self.available(),
            "server_remaining": self.server_remaining,
        }


# ============================================================
# Concurrent context and request count
# ============================================================


class FairUseGate:
    """Admission for one provider counter.

    A counter carries whichever of three constraints the provider publishes for its scope:
    an aggregate in-flight token budget, a threshold at or above which a single request must
    run alone, and a hard request-count ceiling. ``None`` means the provider stated nothing
    of that kind, so the constraint is simply absent - a provider with only a concurrency
    limit gets a counting semaphore, and one with only a token budget gets a bin.

    Lanes are not special-cased. A reservation that reaches the exclusivity threshold runs
    alone because the provider says a request that large may not overlap anything; every
    other lane is admitted while the sum of live reservations stays inside the budget. Which
    lane yields to which is a fairness choice, not a policy rule: subagents give way to a
    primary that is already queued, so an idle operator cannot be starved by a fan-out of
    children.
    """

    def __init__(
        self,
        counter_id: str,
        *,
        budget: int | None = None,
        exclusive_at: int | None = None,
        limit: int | None = None,
        subagent_limit: int | None = None,
    ) -> None:
        self.counter_id = counter_id
        self.condition = asyncio.Condition()
        self.budget = budget
        self.exclusive_at = exclusive_at
        self.limit = limit
        self.subagent_limit = subagent_limit
        self.reserved = 0
        self.active = 0
        self.active_primary = 0
        self.active_subagents = 0
        self.waiting_primary = 0

    def is_exclusive(self, reservation: int) -> bool:
        return self.exclusive_at is not None and reservation >= self.exclusive_at

    def _admits(self, lane: str, reservation: int) -> bool:
        if self.limit is not None and self.active >= self.limit:
            return False
        if self.subagent_limit is not None and self.active_subagents >= self.subagent_limit:
            return False
        if self.is_exclusive(reservation):
            return self.active == 0
        if lane != "primary" and self.waiting_primary:
            return False
        return self.budget is None or self.reserved + reservation <= self.budget

    @contextlib.asynccontextmanager
    async def slot(self, lane: str, reservation: int) -> AsyncIterator[None]:
        primary_like = lane != "subagent"
        async with self.condition:
            if primary_like:
                self.waiting_primary += 1
            try:
                await self.condition.wait_for(lambda: self._admits(lane, reservation))
            finally:
                if primary_like:
                    self.waiting_primary -= 1
                    self.condition.notify_all()
            self.reserved += reservation
            self.active += 1
            if lane == "subagent":
                self.active_subagents += 1
            else:
                self.active_primary += 1
        try:
            yield
        finally:
            async with self.condition:
                self.reserved -= reservation
                self.active -= 1
                if lane == "subagent":
                    self.active_subagents -= 1
                else:
                    self.active_primary -= 1
                self.condition.notify_all()

    def snapshot(self) -> dict:
        return {
            "active": self.active,
            "active_primary": self.active_primary,
            "active_subagents": self.active_subagents,
            "waiting_primary": self.waiting_primary,
            "reserved_context": self.reserved,
            "context_budget": self.budget,
            "exclusive_at": self.exclusive_at,
            "request_limit": self.limit,
            "subagent_limit": self.subagent_limit,
        }


class IngressGate:
    def __init__(self, active: int, queued: int):
        self.semaphore = asyncio.Semaphore(active)
        self.limit = active + queued
        self.pending = 0
        self.lock = asyncio.Lock()

    @contextlib.asynccontextmanager
    async def slot(self):
        async with self.lock:
            if self.pending >= self.limit:
                raise web.HTTPServiceUnavailable(headers={"Retry-After": "1"})
            self.pending += 1
        try:
            async with self.semaphore:
                yield
        finally:
            async with self.lock:
                self.pending -= 1


@dataclass
class Stats:
    rate_waits: int = 0
    guard_rejections: int = 0
    usage_unknown: int = 0
    usage_unsupported: int = 0
    deadline_stops: int = 0
    stream_stalls: int = 0
    policy_errors: int = 0


class Enforcement:
    """The live counter objects for one plan, keyed by the counter ids the plan publishes.

    Objects are reused across a reload and only their numbers move, because a reload happens
    while requests are in flight: a gate that was rebuilt would forget reservations it still
    has to release. Refreshing in place means the next admission is judged by the new policy
    while the old one is still paid back correctly.
    """

    def __init__(self) -> None:
        self.gates: dict[str, FairUseGate] = {}
        self.ledgers: dict[str, RateLedger] = {}

    def sync(self, policy: RuntimePolicy) -> None:
        fan_out = policy.limits.get("subagent_concurrency")
        wanted: set[str] = set()
        for counter_id, counter in sorted(policy.counters.items()):
            family = counter.get("family")
            lanes = counter.get("lanes") or []
            if family in {"context", "count"}:
                budget, exclusive_at, limit = counter_limits(counter)
                subagent_limit = fan_out if "subagent" in lanes else None
                gate = self.gates.get(counter_id)
                if gate is None:
                    self.gates[counter_id] = FairUseGate(
                        counter_id,
                        budget=budget,
                        exclusive_at=exclusive_at,
                        limit=limit,
                        subagent_limit=subagent_limit,
                    )
                else:
                    gate.budget = budget
                    gate.exclusive_at = exclusive_at
                    gate.limit = limit
                    gate.subagent_limit = subagent_limit
                wanted.add(counter_id)
            elif family == "rate":
                capacity = counter.get("capacity")
                if not isinstance(capacity, int) or capacity <= 0:
                    continue
                unit = str(counter.get("unit") or "output_tokens")
                ledger = self.ledgers.get(counter_id)
                if ledger is None:
                    self.ledgers[counter_id] = RateLedger(capacity, unit)
                else:
                    ledger.capacity = capacity
                    ledger.unit = unit
                wanted.add(counter_id)
        for counter_id in set(self.gates) - wanted:
            del self.gates[counter_id]
        for counter_id in set(self.ledgers) - wanted:
            del self.ledgers[counter_id]

    def gates_for(self, lane: LanePolicy) -> tuple[FairUseGate, ...]:
        """Counters this lane must hold, in the one order every request takes them.

        Sorted counter-id order is what keeps two lanes that share counters from deadlocking:
        no request can be waiting on a counter another request has not yet tried to take.
        """
        return tuple(self.gates[key] for key in lane.counters if key in self.gates)

    def ledgers_for(self, lane: LanePolicy) -> tuple[tuple[str, RateLedger], ...]:
        return tuple(
            (key, self.ledgers[key]) for key in lane.counters if key in self.ledgers
        )

    def book(self, lane: LanePolicy, cost: Cost) -> dict[str, Booking] | None:
        """Reserve usage on every rate counter this lane shares, or on none of them.

        Each ledger charges itself in its own unit from the same request, so a provider that
        meters both output tokens and requests sees one request booked once in each.
        """
        bookings: dict[str, Booking] = {}
        for counter_id, ledger in self.ledgers_for(lane):
            booking = ledger.reserve(ledger.charge(cost))
            if booking is None:
                for key, item in bookings.items():
                    self.ledgers[key].settle(item, 0)
                return None
            bookings[counter_id] = booking
        return bookings

    def settle(
        self,
        lane: LanePolicy,
        bookings: dict[str, Booking] | None,
        cost: Cost,
        outcome: Attempt | None,
    ) -> None:
        """Settle every booking in the unit its own ledger meters.

        ``outcome`` is None when the attempt never reached the client, which refunds the
        charge entirely. Where a response reported nothing measurable in a ledger's unit, the
        pessimistic charge stands for the rest of the window - the safe direction to be wrong.
        """
        for counter_id, booking in (bookings or {}).items():
            ledger = self.ledgers[counter_id]
            if outcome is None:
                ledger.settle(booking, 0)
                continue
            actual = ledger.credit(cost, outcome)
            if actual is None:
                stats.usage_unknown += 1
            ledger.settle(booking, actual)

    def wait_seconds(self, lane: LanePolicy) -> float:
        waits = [ledger.wait_seconds() for _key, ledger in self.ledgers_for(lane)]
        return max(waits) if waits else 1.0

    def is_exclusive(self, lane: LanePolicy) -> bool:
        """True when any counter this lane holds would serve it alone."""
        return any(gate.is_exclusive(lane.reservation) for gate in self.gates_for(lane))

    def snapshot(self) -> dict:
        gates = {
            counter_id: gate.snapshot() for counter_id, gate in sorted(self.gates.items())
        }
        ledgers = {
            counter_id: ledger.snapshot()
            for counter_id, ledger in sorted(self.ledgers.items())
        }
        return {"counters": gates, "rates": ledgers}


@contextlib.asynccontextmanager
async def admission(lane: LanePolicy, enforcement: Enforcement) -> AsyncIterator[None]:
    """Hold every counter the plan says this lane's traffic is charged to."""
    reservation = lane.reservation
    async with contextlib.AsyncExitStack() as stack:
        for gate in enforcement.gates_for(lane):
            await stack.enter_async_context(gate.slot(lane.name, reservation))
        yield


enforcement = Enforcement()
enforcement.sync(BASELINE_POLICY)
ingress = IngressGate(
    1 + int(BASELINE_POLICY.limits.get("subagent_concurrency") or 1), MAX_QUEUED
)
stats = Stats()
HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}


def connection_headers(headers) -> set[str]:
    return {
        item.strip().lower() for item in headers.get("Connection", "").split(",") if item.strip()
    }


def filtered_request_headers(request: web.Request, api_key: str) -> dict[str, str]:
    blocked = (
        HOP_BY_HOP_HEADERS
        | connection_headers(request.headers)
        | {"host", "authorization", "content-length", "content-encoding", "accept-encoding"}
    )
    result = {name: value for name, value in request.headers.items() if name.lower() not in blocked}
    # The client's bearer is the harness's internal token; the provider key belongs to the
    # lane's credential and never leaves this process.
    result["Authorization"] = f"Bearer {api_key}"
    result["Content-Type"] = "application/json"
    # Usage is read out of the response stream, so it must not arrive compressed.
    result["Accept-Encoding"] = "identity"
    return result


def filtered_response_headers(headers) -> dict[str, str]:
    blocked = HOP_BY_HOP_HEADERS | connection_headers(headers) | {"content-length"}
    return {name: value for name, value in headers.items() if name.lower() not in blocked}


def clamp_retry_delay(seconds: float) -> float:
    return min(300.0, max(1.0, seconds))


def retry_delay(response, attempt: int) -> float:
    retry_after = response.headers.get("Retry-After")
    if retry_after:
        try:
            return clamp_retry_delay(float(retry_after))
        except ValueError:
            try:
                return clamp_retry_delay(
                    parsedate_to_datetime(retry_after).timestamp() - time.time()
                )
            except (TypeError, ValueError, OverflowError):
                pass
    reset = response.headers.get("x-ratelimit-reset")
    if reset:
        try:
            value = float(reset)
            if value > 100_000_000_000:
                value /= 1000
            return clamp_retry_delay(value - time.time() if value > 10_000_000 else value)
        except ValueError:
            pass
    return min(60.0, 2 ** min(max(attempt - 1, 0), 6)) + secrets.SystemRandom().uniform(0.0, 1.0)


# ============================================================
# Measured usage out of the response stream
# ============================================================


class UsageScanner:
    """Read token usage and generated length from an OpenAI-compatible stream.

    Chunks split anywhere, so events are only parsed once their terminating blank
    line has arrived. The buffer is capped: a parser that cannot keep up degrades
    to a reservation kept for the whole window, never to unbounded memory.
    """

    def __init__(self, *, cap: int = 262144) -> None:
        self.cap = cap
        self.buffer = bytearray()
        self.completion_tokens: int | None = None
        self.prompt_tokens: int | None = None
        self.text_chars = 0
        self.overflowed = False

    def feed(self, chunk: bytes) -> None:
        if self.overflowed:
            return
        self.buffer += chunk
        while True:
            raw, separator, rest = bytes(self.buffer).partition(b"\n\n")
            if not separator:
                break
            self.buffer = bytearray(rest)
            self._event(raw)
        if len(self.buffer) > self.cap:
            self.buffer.clear()
            self.overflowed = True

    def finish(self) -> None:
        if not self.overflowed and self.buffer:
            self._event(bytes(self.buffer))
        self.buffer.clear()

    def _event(self, raw: bytes) -> None:
        for line in raw.split(b"\n"):
            line = line.strip()
            if not line.startswith(b"data:"):
                continue
            data = line[5:].strip()
            if not data or data == b"[DONE]":
                continue
            if b'"usage"' not in data and b'"content"' not in data:
                if b'"reasoning_content"' not in data:
                    continue
            try:
                event = json.loads(data)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if not isinstance(event, dict):
                continue
            usage = event.get("usage")
            if isinstance(usage, dict):
                completion = usage.get("completion_tokens")
                prompt = usage.get("prompt_tokens")
                if isinstance(completion, int) and not isinstance(completion, bool):
                    self.completion_tokens = completion
                if isinstance(prompt, int) and not isinstance(prompt, bool):
                    self.prompt_tokens = prompt
            for choice in event.get("choices") or []:
                if not isinstance(choice, dict):
                    continue
                delta = choice.get("delta")
                source = delta if isinstance(delta, dict) else choice.get("message")
                if not isinstance(source, dict):
                    continue
                for key in ("content", "reasoning_content"):
                    value = source.get(key)
                    if isinstance(value, str):
                        self.text_chars += len(value)

    def estimated_output(self) -> int | None:
        """Character-derived fallback for when the gateway reports no usage."""
        if self.completion_tokens is not None:
            return None
        if not self.text_chars and self.overflowed:
            return None
        return (self.text_chars + 3) // 4


async def health(_request: web.Request) -> web.Response:
    """Report the policy actually in force, and fail if it is no longer verifiable.

    A 5xx here keeps ``kimi-agent`` from starting against an unverifiable policy;
    a running agent is unaffected, because Compose only reads health at startup.
    """
    try:
        policy = current_policy()
    except PolicyDrift as exc:
        return web.json_response(
            {"status": "policy-drift", "policy_error": str(exc), **enforcement.snapshot()},
            status=503,
        )

    return web.json_response(
        {
            "status": "ok",
            "policy_enforced": True,
            **enforcement.snapshot(),
            "subagent_limit": policy.limits.get("subagent_concurrency"),
            "providers": sorted(policy.providers),
            "credentials": sorted(
                {lane.secret_name for lane in policy.lanes.values()},
            ),
            "lanes": {
                lane: {
                    "alias": item.alias,
                    "provider": item.provider_name,
                    "model": item.model,
                    "endpoint": item.base_url,
                    "credential": item.secret_name,
                    "context": item.context,
                    "max_input": item.max_input,
                    "output_clamp": item.output_clamp,
                    "reservation": item.reservation,
                    "exclusive": enforcement.is_exclusive(item),
                    "counters": list(item.counters),
                }
                for lane, item in policy.lanes.items()
            },
            "input_guard_percent": INPUT_GUARD_PERCENT,
            "sock_read_timeout_seconds": SOCK_READ_TIMEOUT,
            "max_request_seconds": MAX_REQUEST_SECONDS,
            "usage_reporting_supported": USAGE_SUPPORTED,
            "stats": {
                "rate_waits": stats.rate_waits,
                "guard_rejections": stats.guard_rejections,
                "usage_unknown": stats.usage_unknown,
                "usage_unsupported": stats.usage_unsupported,
                "deadline_stops": stats.deadline_stops,
                "stream_stalls": stats.stream_stalls,
                "policy_errors": stats.policy_errors,
            },
        }
    )


async def models(request: web.Request) -> web.Response:
    authorize_client(request)
    lane = current_policy().lanes[request.match_info["lane"]]
    return web.json_response({"object": "list", "data": [{"id": lane.model, "object": "model"}]})


async def chat(request: web.Request) -> web.StreamResponse:
    started = time.monotonic()
    authorize_client(request)

    if request.query_string:
        raise web.HTTPBadRequest(text="query strings are not supported")

    if request.headers.get("Content-Encoding", "identity").lower() != "identity":
        raise web.HTTPUnsupportedMediaType(
            text="compressed request bodies are not supported"
        )

    lane_name = request.match_info["lane"]

    try:
        policy = current_policy()
    except PolicyDrift as exc:
        stats.policy_errors += 1
        raise web.HTTPServiceUnavailable(
            text=f"model policy is not currently enforced: {exc}",
            headers={"Retry-After": "5"},
        ) from exc

    lane = policy.lanes[lane_name]

    async with ingress.slot():
        inbound_body = await request.read()

        debug_http(
            "INBOUND REQUEST TO PROXY",
            method=request.method,
            url=str(request.rel_url),
            headers=request.headers,
            body=inbound_body,
        )

        guard = lane.max_input * INPUT_GUARD_PERCENT // 100
        estimate, media = estimate_input_tokens(inbound_body)
        if estimate > guard:
            stats.guard_rejections += 1
            print(
                f"input_guard lane={lane_name} estimate={estimate} guard={guard} "
                f"media_items={media} input_bytes={len(inbound_body)}",
                flush=True,
            )
            # max_size and actual_size are token counts here, not bytes: the guard is
            # a context allowance, and text overrides the byte-worded default.
            raise web.HTTPRequestEntityTooLarge(
                guard,
                estimate,
                text="request input exceeds the enforced lane context allowance",
            )

        outbound_body = rewrite_model(inbound_body, request, lane)

        return await forward_chat(
            request,
            lane,
            outbound_body,
            started,
            inbound_body,
            # The guard's estimate is the best input figure available before the request
            # runs; a ledger that meters input charges itself from it and settles to the
            # prompt tokens the response reports.
            Cost(input=estimate, output=lane.output_clamp),
        )


class _HeaderBag:
    """Minimal Retry-After source for failures that never produced a response."""

    def __init__(self, headers: dict[str, str]) -> None:
        self.headers = headers


@dataclass
class Attempt:
    """Outcome of one permitted upstream attempt.

    ``response`` is set as soon as anything has reached the client. From that
    point the request cannot be retried, because its status line is already sent,
    so a stall has to end the attempt instead of looping.
    """

    response: web.StreamResponse | None = None
    retry_after: float | None = None
    rejected_usage: bool = False
    output_tokens: int | None = None
    prompt_tokens: int | None = None
    estimated_output: int | None = None
    stalled: bool = False

    @property
    def streamed(self) -> bool:
        return self.response is not None


async def stream_attempt(
    request: web.Request,
    session: ClientSession,
    lane: LanePolicy,
    body: bytes,
    attempt: int,
) -> Attempt:
    """Make one attempt, holding every fair-use permit this lane is charged to."""
    url = f"{lane.base_url}/v1/chat/completions"
    async with admission(lane, enforcement):
        outbound_headers = filtered_request_headers(
            request, upstream_credential(lane.secret_name)
        )

        debug_http(
            f"OUTBOUND REQUEST TO UPSTREAM - ATTEMPT {attempt}",
            method="POST",
            url=url,
            headers=outbound_headers,
            body=body,
            redact_cache_salt=True,
        )

        response = await session.post(
            url,
            data=body,
            headers=outbound_headers,
            allow_redirects=False,
        )

        try:
            for _counter_id, ledger in enforcement.ledgers_for(lane):
                ledger.note_server(response.headers)

            if response.status in RETRYABLE or response.status == 400:
                error_body = await response.content.read(MAX_ERROR_BYTES + 1)

                if response.status in RETRYABLE:
                    debug_http(
                        f"UPSTREAM RESPONSE - ATTEMPT {attempt}",
                        status=response.status,
                        headers=response.headers,
                        body=error_body,
                    )
                    response.close()
                    return Attempt(retry_after=retry_delay(response, attempt))

                if body_rejects_stream_usage(response.status, error_body):
                    response.close()
                    return Attempt(retry_after=0.0, rejected_usage=True)

            if (
                response.content_length is not None
                and response.content_length > MAX_RESPONSE_BYTES
            ):
                response.close()
                raise web.HTTPBadGateway(text="upstream response exceeds byte limit")

            scanner = UsageScanner()
            downstream = web.StreamResponse(
                status=response.status,
                headers=filtered_response_headers(response.headers),
            )
            await downstream.prepare(request)

            output_bytes = 0
            debug_response_body = bytearray() if DEBUG_HTTP else None

            try:
                async for chunk in response.content.iter_any():
                    output_bytes += len(chunk)
                    scanner.feed(chunk)

                    if debug_response_body is not None:
                        debug_response_body.extend(chunk)

                    if output_bytes > MAX_RESPONSE_BYTES:
                        response.close()
                        scanner.finish()
                        print(
                            f"response_limit lane={lane.name} input_bytes={len(body)} "
                            f"output_bytes={output_bytes}",
                            flush=True,
                        )
                        if request.transport is not None:
                            request.transport.close()
                        return _attempt_result(downstream, scanner)

                    await downstream.write(chunk)

            except (TimeoutError, ClientConnectionError, ServerDisconnectedError):
                # Part of this response is already with the client, so the only
                # honest outcome is a dropped connection. The permit is released by
                # the surrounding context manager either way.
                stats.stream_stalls += 1
                scanner.finish()
                response.close()
                print(
                    f"stream_stall lane={lane.name} attempt={attempt} bytes={output_bytes}",
                    flush=True,
                )
                if request.transport is not None:
                    request.transport.close()
                return Attempt(
                    response=downstream,
                    output_tokens=scanner.completion_tokens,
                    prompt_tokens=scanner.prompt_tokens,
                    estimated_output=scanner.estimated_output(),
                    stalled=True,
                )

            scanner.finish()
            await downstream.write_eof()

            if debug_response_body is not None:
                debug_http(
                    f"UPSTREAM RESPONSE - ATTEMPT {attempt}",
                    status=response.status,
                    headers=response.headers,
                    body=bytes(debug_response_body),
                )

            return _attempt_result(downstream, scanner)

        finally:
            response.release()


def _attempt_result(
    downstream: web.StreamResponse, scanner: UsageScanner
) -> Attempt:
    return Attempt(
        response=downstream,
        output_tokens=scanner.completion_tokens,
        prompt_tokens=scanner.prompt_tokens,
        estimated_output=scanner.estimated_output(),
    )


def book_output(
    lane: LanePolicy, bookings: dict[str, Booking] | None, cost: Cost, outcome: Attempt
) -> None:
    """Settle this attempt's rate bookings against what it actually produced."""
    enforcement.settle(lane, bookings, cost, outcome)


async def forward_chat(
    request: web.Request,
    lane: LanePolicy,
    body: bytes,
    started: float,
    inbound_body: bytes,
    cost: Cost,
) -> web.StreamResponse:
    """Retry an upstream request until it finishes or the client goes away.

    Every sleep happens with no fair-use permit held and no rate booking outstanding,
    so provider backpressure never occupies capacity that other requests could use.
    """
    global USAGE_SUPPORTED

    attempt_number = 0
    session: ClientSession = request.app["client"]

    while True:
        attempt_number += 1

        if request.transport is None or request.transport.is_closing():
            raise asyncio.CancelledError

        elapsed = time.monotonic() - started
        if elapsed > MAX_REQUEST_SECONDS:
            stats.deadline_stops += 1
            print(
                f"request_deadline lane={lane.name} attempts={attempt_number} "
                f"elapsed_seconds={elapsed:.1f}",
                flush=True,
            )
            raise web.HTTPBadGateway(
                text="upstream did not complete within the policy request limit"
            )

        bookings = enforcement.book(lane, cost)
        if bookings is None:
            stats.rate_waits += 1
            delay = enforcement.wait_seconds(lane)
            print(
                f"rate_wait lane={lane.name} attempt={attempt_number} delay={delay:.1f}s",
                flush=True,
            )
            await asyncio.sleep(delay)
            continue

        try:
            outcome = await stream_attempt(
                request, session, lane, body, attempt_number
            )
        except (TimeoutError, ClientConnectionError, ServerDisconnectedError) as exc:
            enforcement.settle(lane, bookings, cost, None)
            delay = retry_delay(_HeaderBag({}), attempt_number)
            print(
                f"upstream_retry lane={lane.name} attempt={attempt_number} "
                f"error={type(exc).__name__} delay={delay:.1f}s",
                flush=True,
            )
        else:
            if outcome.streamed:
                book_output(lane, bookings, cost, outcome)
                print(
                    f"request_complete lane={lane.name} input_bytes={len(body)} "
                    f"attempts={attempt_number} prompt_tokens={outcome.prompt_tokens} "
                    f"output_tokens={outcome.output_tokens} "
                    f"estimated_output={outcome.estimated_output} "
                    f"stalled={outcome.stalled} "
                    f"elapsed_seconds={time.monotonic() - started:.3f}",
                    flush=True,
                )
                return outcome.response

            if outcome.rejected_usage and USAGE_SUPPORTED:
                USAGE_SUPPORTED = False
                stats.usage_unsupported += 1
                body = rewrite_model(inbound_body, request, lane)
                print(
                    "usage_reporting_unsupported - retrying without stream_options",
                    flush=True,
                )

            enforcement.settle(lane, bookings, cost, None)
            delay = outcome.retry_after if outcome.retry_after is not None else 1.0
            print(
                f"upstream_retry lane={lane.name} attempt={attempt_number} "
                f"delay={delay:.1f}s",
                flush=True,
            )

        await asyncio.sleep(delay)


async def create_client(app: web.Application) -> None:
    app["client"] = ClientSession(
        timeout=ClientTimeout(
            total=None,
            connect=60,
            sock_connect=60,
            sock_read=SOCK_READ_TIMEOUT,
        ),
        auto_decompress=False,
    )


async def close_client(app: web.Application) -> None:
    await app["client"].close()


def create_app() -> web.Application:
    app = web.Application(client_max_size=MAX_REQUEST_BYTES)
    app.on_startup.append(create_client)
    app.on_cleanup.append(close_client)

    # Only lanes the operator's definitions produced are routable; a lane the plan does
    # not publish answers 404 rather than being admitted and failing policy validation.
    lanes = "{lane:" + "|".join(sorted(BASELINE_POLICY.lanes)) + "}"
    app.router.add_get("/healthz", health)
    app.router.add_post(f"/{lanes}/v1/chat/completions", chat)
    app.router.add_get(f"/{lanes}/v1/models", models)
    return app


def main() -> None:
    validate_policy()
    for name, lane in sorted(BASELINE_POLICY.lanes.items()):
        print(
            f"lane={name} alias={lane.alias} provider={lane.provider_name} "
            f"context={lane.context} input={lane.max_input} output={lane.output_clamp} "
            f"reservation={lane.reservation} counters={','.join(lane.counters)}",
            flush=True,
        )
    web.run_app(create_app(), host="0.0.0.0", port=8080, access_log=None)  # noqa: S104


if __name__ == "__main__":
    main()
