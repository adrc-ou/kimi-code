#!/usr/bin/env python3
"""Strict OpenAI-compatible NRP policy proxy.

NRP's Fair Use Policy has three rules, and this module enforces all three:

1. 200,000 output tokens per minute per API token and model. This is the only
   limit the gateway enforces itself. ``OutputRateLedger`` books each request's
   full lane output at admission and settles it to measured usage, so the proxy
   paces itself instead of waiting to be rejected with HTTP 429.
2. Any single request utilizing at least 35% of the model's context length is
   limited to one concurrent request. ``FairUseGate`` treats such lanes as
   exclusive.
3. Otherwise all concurrent requests combined must stay inside 35% of the model's
   context length, within the per-model request-count ceiling. ``FairUseGate``
   admits on summed per-lane reservations, so lane behaviour is derived from the
   rendered Kimi configuration rather than hard-coded per lane.

A request's lane reservation is ``max_input_size`` plus the lane output clamp,
which is the worst-case context that request can occupy while in flight.

Fair-use permits and rate bookings are always released before a backoff sleep, so
waiting on NRP never consumes a permit. The enforced Kimi configuration is
re-read on the request path: drift fails a lane closed instead of being served
under stale assumptions.
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

LANES = ("primary", "long", "subagent")
PROVIDER_BY_LANE = {lane: f"nrp-{lane}" for lane in LANES}
RATE_WINDOW_SECONDS = 60.0


def secret(file_variable: str, value_variable: str, *, min_length: int = 32) -> str:
    path = os.environ.get(file_variable, "")
    value = (Path(path).read_text() if path else os.environ.get(value_variable, "")).strip()
    if len(value) < min_length:
        raise RuntimeError(
            f"{file_variable} must reference a secret of at least {min_length} characters"
        )
    return value


UPSTREAM = os.environ["NRP_UPSTREAM_ORIGIN"].rstrip("/")
UPSTREAM_MODEL = os.environ["NRP_UPSTREAM_MODEL"]
# Provider-issued keys have no harness-defined length; only our generated
# internal credentials and private cache salt require at least 32 characters.
API_KEY = secret("NRP_API_KEY_FILE", "NRP_API_KEY", min_length=1)
INTERNAL_BEARER_TOKEN = secret("NRP_INTERNAL_TOKEN_FILE", "NRP_INTERNAL_TOKEN")
CACHE_SALT = secret("NRP_CACHE_SALT_FILE", "NRP_CACHE_SALT")
KIMI_CONFIG_PATH = os.environ.get("KIMI_CONFIG_PATH", "/policy/kimi-config.toml")
SUBAGENT_LIMIT = int(os.environ.get("NRP_SUBAGENT_MAX_CONCURRENCY", "5"))
MODEL_CONTEXT = int(os.environ.get("NRP_MODEL_CONTEXT", "1000000"))
FAIR_USE_PERCENT = int(os.environ.get("NRP_FAIR_USE_PERCENT", "35"))
PARALLEL_CONTEXT_BUDGET = int(os.environ.get("NRP_PARALLEL_CONTEXT_BUDGET", "320000"))
MODEL_MAX_CONCURRENCY = int(os.environ.get("NRP_MODEL_MAX_CONCURRENCY", "16"))
MAX_REQUEST_BYTES = int(os.environ.get("NRP_MAX_REQUEST_BYTES", str(256 * 1024**2)))
MAX_RESPONSE_BYTES = int(os.environ.get("NRP_MAX_RESPONSE_BYTES", str(512 * 1024**2)))
MAX_ERROR_BYTES = int(os.environ.get("NRP_MAX_ERROR_BYTES", str(64 * 1024)))
MAX_QUEUED = int(os.environ.get("NRP_MAX_QUEUED", "32"))
MAX_OUTPUT_TOKENS = {
    "primary": int(os.environ.get("NRP_PRIMARY_MAX_OUTPUT_TOKENS", "65536")),
    "long": int(os.environ.get("NRP_LONG_MAX_OUTPUT_TOKENS", "65536")),
    "subagent": int(os.environ.get("NRP_SUBAGENT_MAX_OUTPUT_TOKENS", "8192")),
}

# Rule 1: the gateway's only automatic limit, kept below capacity so that output
# estimates landing high do not turn into 429s.
OUTPUT_TOKENS_PER_MINUTE = int(os.environ.get("NRP_OUTPUT_TOKENS_PER_MINUTE", "200000"))
OUTPUT_RATE_HEADROOM = int(os.environ.get("NRP_OUTPUT_RATE_HEADROOM_PERCENT", "90"))

# A stalled upstream read must release its permit rather than wedge the lane.
SOCK_READ_TIMEOUT = float(os.environ.get("NRP_UPSTREAM_SOCK_READ_TIMEOUT", "300"))
MAX_REQUEST_SECONDS = float(os.environ.get("NRP_MAX_REQUEST_SECONDS", "3600"))

# Coarse per-lane input ceiling, as a percentage of the lane input cap. It exists
# to catch configuration drift and abuse, not to meter legitimate traffic.
INPUT_GUARD_PERCENT = int(os.environ.get("NRP_INPUT_GUARD_PERCENT", "150"))
MEDIA_TOKEN_ESTIMATE = int(os.environ.get("NRP_MEDIA_TOKEN_ESTIMATE", "1600"))

# Whether the gateway still accepts usage reporting in streamed responses.
REQUEST_USAGE = os.environ.get("NRP_REQUEST_USAGE", "1").strip().lower() in {
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
# Enforced Kimi configuration
# ============================================================


class PolicyDrift(RuntimeError):
    """The rendered Kimi configuration no longer satisfies fair-use policy."""


@dataclass(frozen=True)
class LanePolicy:
    alias: str
    context: int
    max_input: int
    output_clamp: int
    reserved: int

    @property
    def reservation(self) -> int:
        """Worst-case context this lane occupies while in flight."""
        return self.max_input + self.output_clamp


@dataclass(frozen=True)
class RuntimePolicy:
    lanes: dict[str, LanePolicy]
    forced_alias: str

    def reservation(self, lane: str) -> int:
        return self.lanes[lane].reservation

    def output_clamp(self, lane: str) -> int:
        return self.lanes[lane].output_clamp


def fair_use_ceiling() -> int:
    return MODEL_CONTEXT * FAIR_USE_PERCENT // 100


def _lane_model(config: dict, lane: str) -> tuple[str, dict]:
    provider = PROVIDER_BY_LANE[lane]
    matches = [
        (alias, model)
        for alias, model in config.get("models", {}).items()
        if isinstance(model, dict) and model.get("provider") == provider
    ]
    if len(matches) != 1:
        raise PolicyDrift(f"expected exactly one model bound to provider {provider!r}")
    alias, model = matches[0]
    for key in ("max_context_size", "max_input_size"):
        if not isinstance(model.get(key), int) or model[key] <= 0:
            raise PolicyDrift(f"{alias}.{key} must be a positive integer")
    return alias, model


def load_runtime_policy(config_path: str | None = None) -> RuntimePolicy:
    """Read and validate the rendered Kimi configuration that defines each lane."""
    with open(config_path or KIMI_CONFIG_PATH, "rb") as source:
        config = tomllib.load(source)

    secondary = config["secondary_model"]
    if secondary.get("force") is not True:
        raise PolicyDrift("Kimi secondary_model.force must be true")
    forced_alias = secondary["default_model"]

    reserve = int(config.get("loop_control", {}).get("reserved_context_size", 0))
    if reserve <= 0:
        raise PolicyDrift("Kimi loop_control.reserved_context_size must be positive")

    lanes: dict[str, LanePolicy] = {}
    for lane in LANES:
        alias, model = _lane_model(config, lane)
        context = int(model["max_context_size"])
        max_input = int(model["max_input_size"])
        clamp = MAX_OUTPUT_TOKENS[lane]
        if max_input + reserve > context:
            raise PolicyDrift(f"{alias}: max_input_size + reserved_context_size exceeds window")
        if max_input + clamp > context:
            raise PolicyDrift(f"{alias}: max_input_size + {clamp} output clamp exceeds window")
        lanes[lane] = LanePolicy(alias, context, max_input, clamp, reserve)

    if lanes["subagent"].alias != forced_alias:
        provider = PROVIDER_BY_LANE["subagent"]
        raise PolicyDrift(f"forced secondary model {forced_alias!r} is not the {provider} lane")

    return RuntimePolicy(lanes=lanes, forced_alias=forced_alias)


def validate_policy(policy: RuntimePolicy | None = None) -> None:
    """Fail startup unless every configured lane can coexist inside fair use."""
    policy = policy or BASELINE_POLICY
    numeric_values = {
        "NRP_MODEL_CONTEXT": MODEL_CONTEXT,
        "NRP_FAIR_USE_PERCENT": FAIR_USE_PERCENT,
        "NRP_PARALLEL_CONTEXT_BUDGET": PARALLEL_CONTEXT_BUDGET,
        "NRP_MODEL_MAX_CONCURRENCY": MODEL_MAX_CONCURRENCY,
        "NRP_SUBAGENT_MAX_CONCURRENCY": SUBAGENT_LIMIT,
        "NRP_OUTPUT_TOKENS_PER_MINUTE": OUTPUT_TOKENS_PER_MINUTE,
        "NRP_OUTPUT_RATE_HEADROOM_PERCENT": OUTPUT_RATE_HEADROOM,
        "NRP_UPSTREAM_SOCK_READ_TIMEOUT": SOCK_READ_TIMEOUT,
        "NRP_MAX_REQUEST_SECONDS": MAX_REQUEST_SECONDS,
        "NRP_INPUT_GUARD_PERCENT": INPUT_GUARD_PERCENT,
        "NRP_MEDIA_TOKEN_ESTIMATE": MEDIA_TOKEN_ESTIMATE,
        "NRP_MAX_REQUEST_BYTES": MAX_REQUEST_BYTES,
        "NRP_MAX_RESPONSE_BYTES": MAX_RESPONSE_BYTES,
        "NRP_MAX_QUEUED": MAX_QUEUED,
    }
    for name, value in numeric_values.items():
        if value <= 0:
            raise RuntimeError(f"{name} must be positive; got {value}")
    if FAIR_USE_PERCENT > 100:
        raise RuntimeError(f"NRP_FAIR_USE_PERCENT must not exceed 100; got {FAIR_USE_PERCENT}")
    if not 1 <= OUTPUT_RATE_HEADROOM <= 100:
        raise RuntimeError("NRP_OUTPUT_RATE_HEADROOM_PERCENT must be between 1 and 100")
    if INPUT_GUARD_PERCENT < 100:
        raise RuntimeError("NRP_INPUT_GUARD_PERCENT must be at least 100")

    ceiling = fair_use_ceiling()
    if PARALLEL_CONTEXT_BUDGET > ceiling:
        raise RuntimeError("NRP_PARALLEL_CONTEXT_BUDGET exceeds the configured fair-use limit")
    if SUBAGENT_LIMIT > MODEL_MAX_CONCURRENCY:
        raise RuntimeError("Configured subagent concurrency exceeds NRP model concurrency")

    for lane, lane_policy in policy.lanes.items():
        if lane_policy.reservation > MODEL_CONTEXT:
            raise RuntimeError(f"{lane} lane cannot fit inside the served context window")
        if lane_policy.reservation < ceiling and lane_policy.reservation > PARALLEL_CONTEXT_BUDGET:
            raise RuntimeError(
                f"{lane} lane reservation {lane_policy.reservation} can never be admitted "
                f"under parallel budget {PARALLEL_CONTEXT_BUDGET}"
            )

    subagent_reservation = policy.reservation("subagent")
    if SUBAGENT_LIMIT * subagent_reservation > PARALLEL_CONTEXT_BUDGET:
        raise RuntimeError("Subagent configuration exceeds parallel context budget")


_policy_cache: tuple[int, int, RuntimePolicy] | None = None
_policy_error: str | None = None
_policy_error_logged = False


def current_policy() -> RuntimePolicy:
    """Return the enforced policy, re-reading it whenever the file has changed.

    Fails closed: a configuration that no longer satisfies fair use stops the
    affected traffic rather than being served under the last known-good policy.
    """
    global _policy_cache, _policy_error, _policy_error_logged

    stat = os.stat(KIMI_CONFIG_PATH)
    stamp = (int(stat.st_mtime_ns), stat.st_size)
    if _policy_cache is not None and _policy_cache[0:2] == stamp:
        return _policy_cache[2]

    try:
        policy = load_runtime_policy()
    except (OSError, KeyError, ValueError, PolicyDrift, TypeError) as exc:
        detail = str(exc)
        if detail != _policy_error or not _policy_error_logged:
            print(f"policy_drift error={detail}", flush=True)
            _policy_error_logged = True
        _policy_error = detail
        raise PolicyDrift(detail) from exc

    try:
        validate_policy(policy)
    except RuntimeError as exc:
        detail = str(exc)
        if detail != _policy_error or not _policy_error_logged:
            print(f"policy_drift error={detail}", flush=True)
            _policy_error_logged = True
        _policy_error = detail
        raise PolicyDrift(detail) from exc

    if _policy_error is not None:
        print("policy_drift_clear", flush=True)
    _policy_error = None
    _policy_error_logged = False
    _policy_cache = (stamp[0], stamp[1], policy)
    return policy


BASELINE_POLICY = load_runtime_policy()


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


def rewrite_model(body: bytes, request: web.Request, lane: str = "primary") -> bytes:
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
        payload[name] = min(value, MAX_OUTPUT_TOKENS[lane])
    if not limits:
        payload["max_completion_tokens"] = MAX_OUTPUT_TOKENS[lane]
    # Rule 1 is metered in output tokens, so measured usage beats estimation.
    if REQUEST_USAGE and USAGE_SUPPORTED and payload.get("stream") is True:
        payload.setdefault("stream_options", {"include_usage": True})
    payload["model"] = UPSTREAM_MODEL
    payload["cache_salt"] = CACHE_SALT
    return json.dumps(payload, separators=(",", ":"), allow_nan=False).encode()


def body_rejects_stream_usage(status: int, body: bytes) -> bool:
    return status == 400 and b"stream_options" in body


# ============================================================
# Rule 1: output tokens per minute
# ============================================================


@dataclass(eq=False)
class Booking:
    """One entry in the rolling output-token window.

    Identity comparison is deliberate: two bookings for the same amount and expiry
    must not be interchangeable when one of them is settled.
    """

    expires_at: float
    amount: int
    settled: bool = False
    pruned: bool = False


class OutputRateLedger:
    """Rolling-window cap on output tokens, which is all NRP enforces itself.

    A request books its lane's full output allowance before it starts, because
    its eventual size is genuinely unknown, and is then settled to measured
    usage. The gateway's own remaining-quota header wins whenever it reports
    less headroom than we do.
    """

    def __init__(
        self,
        capacity: int,
        *,
        window: float = RATE_WINDOW_SECONDS,
        clock=time.monotonic,
    ) -> None:
        self.capacity = capacity
        self.window = window
        self.clock = clock
        self.bookings: deque[Booking] = deque()
        self.committed = 0
        self.server_remaining: int | None = None
        self.server_expires_at: float = 0.0

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
        remaining = headers.get("x-ratelimit-remaining")
        if remaining is None:
            return
        try:
            value = int(float(remaining))
        except ValueError:
            return
        reset = headers.get("x-ratelimit-reset")
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
            "committed": self.projected(),
            "available": self.available(),
            "server_remaining": self.server_remaining,
        }


def output_rate_capacity() -> int:
    return OUTPUT_TOKENS_PER_MINUTE * OUTPUT_RATE_HEADROOM // 100


# ============================================================
# Rules 2 and 3: concurrent context and request count
# ============================================================


class FairUseGate:
    """Admission for fair-use context and request-count limits.

    Lanes are not special-cased. A lane whose worst-case reservation reaches the
    fair-use ceiling may run alone (rule 2); every other lane is admitted while
    the sum of live reservations stays inside the parallel budget (rule 3). At
    the shipped numbers that reproduces strict primary/subagent separation,
    because a primary reservation plus one subagent reservation exceeds it.
    """

    def __init__(
        self,
        subagent_limit: int,
        *,
        model_limit: int = MODEL_MAX_CONCURRENCY,
        budget: int = PARALLEL_CONTEXT_BUDGET,
        ceiling: int | None = None,
    ) -> None:
        self.condition = asyncio.Condition()
        self.subagent_limit = subagent_limit
        self.model_limit = model_limit
        self.budget = budget
        self.ceiling = fair_use_ceiling() if ceiling is None else ceiling
        self.reserved = 0
        self.active = 0
        self.active_primary = 0
        self.active_subagents = 0
        self.waiting_primary = 0

    def is_exclusive(self, reservation: int) -> bool:
        return reservation >= self.ceiling

    def _admits(self, lane: str, reservation: int) -> bool:
        if self.active >= self.model_limit:
            return False
        if lane == "subagent" and self.active_subagents >= self.subagent_limit:
            return False
        if self.is_exclusive(reservation):
            return self.active == 0
        # Subagents yield to a primary that is already queued, so an idle
        # operator cannot be starved by a continuous fan-out of children.
        if lane != "primary" and self.waiting_primary:
            return False
        return self.reserved + reservation <= self.budget

    @contextlib.asynccontextmanager
    async def slot(self, lane: str, reservation: int) -> AsyncIterator[None]:
        primary_like = lane in {"primary", "long"}
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
            "parallel_context_budget": self.budget,
            "fair_use_context_ceiling": self.ceiling,
            "subagent_limit": self.subagent_limit,
            "model_limit": self.model_limit,
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


gate = FairUseGate(SUBAGENT_LIMIT)
rate_ledger = OutputRateLedger(output_rate_capacity())
ingress = IngressGate(1 + SUBAGENT_LIMIT, MAX_QUEUED)
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


def filtered_request_headers(request: web.Request) -> dict[str, str]:
    blocked = (
        HOP_BY_HOP_HEADERS
        | connection_headers(request.headers)
        | {"host", "authorization", "content-length", "content-encoding", "accept-encoding"}
    )
    result = {name: value for name, value in request.headers.items() if name.lower() not in blocked}
    result["Authorization"] = f"Bearer {API_KEY}"
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
            {"status": "policy-drift", "policy_error": str(exc), **gate.snapshot()},
            status=503,
        )

    return web.json_response(
        {
            "status": "ok",
            "policy_enforced": True,
            **gate.snapshot(),
            **{"rate_" + key: value for key, value in rate_ledger.snapshot().items()},
            "fair_use_percent": FAIR_USE_PERCENT,
            "model_context": MODEL_CONTEXT,
            "output_tokens_per_minute": OUTPUT_TOKENS_PER_MINUTE,
            "lanes": {
                lane: {
                    "alias": item.alias,
                    "context": item.context,
                    "max_input": item.max_input,
                    "output_clamp": item.output_clamp,
                    "reservation": item.reservation,
                    "exclusive": gate.is_exclusive(item.reservation),
                }
                for lane, item in policy.lanes.items()
            },
            "forced_secondary_model": policy.forced_alias,
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
    return web.json_response(
        {"object": "list", "data": [{"id": UPSTREAM_MODEL, "object": "model"}]}
    )


async def chat(request: web.Request) -> web.StreamResponse:
    started = time.monotonic()
    authorize_client(request)

    if request.query_string:
        raise web.HTTPBadRequest(text="query strings are not supported")

    if request.headers.get("Content-Encoding", "identity").lower() != "identity":
        raise web.HTTPUnsupportedMediaType(
            text="compressed request bodies are not supported"
        )

    lane = request.match_info["lane"]

    try:
        policy = current_policy()
    except PolicyDrift as exc:
        stats.policy_errors += 1
        raise web.HTTPServiceUnavailable(
            text=f"model policy is not currently enforced: {exc}",
            headers={"Retry-After": "5"},
        ) from exc

    async with ingress.slot():
        inbound_body = await request.read()

        debug_http(
            "INBOUND REQUEST TO PROXY",
            method=request.method,
            url=str(request.rel_url),
            headers=request.headers,
            body=inbound_body,
        )

        guard = policy.lanes[lane].max_input * INPUT_GUARD_PERCENT // 100
        estimate, media = estimate_input_tokens(inbound_body)
        if estimate > guard:
            stats.guard_rejections += 1
            print(
                f"input_guard lane={lane} estimate={estimate} guard={guard} "
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
            policy,
            inbound_body,
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
    lane: str,
    body: bytes,
    policy: RuntimePolicy,
    attempt: int,
) -> Attempt:
    """Make one attempt, holding the lane's fair-use permit for its whole life."""
    async with gate.slot(lane, policy.reservation(lane)):
        outbound_headers = filtered_request_headers(request)

        debug_http(
            f"OUTBOUND REQUEST TO UPSTREAM - ATTEMPT {attempt}",
            method="POST",
            url=f"{UPSTREAM}/v1/chat/completions",
            headers=outbound_headers,
            body=body,
            redact_cache_salt=True,
        )

        response = await session.post(
            f"{UPSTREAM}/v1/chat/completions",
            data=body,
            headers=outbound_headers,
            allow_redirects=False,
        )

        try:
            rate_ledger.note_server(response.headers)

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
                            f"response_limit lane={lane} input_bytes={len(body)} "
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
                    f"stream_stall lane={lane} attempt={attempt} bytes={output_bytes}",
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


def book_output(booking: Booking | None, outcome: Attempt) -> None:
    """Settle a rate booking against measured output.

    With neither measured usage nor an estimate the pessimistic booking stands for
    the rest of the window, which is the safe direction to be wrong in.
    """
    actual = outcome.output_tokens
    if actual is None:
        actual = outcome.estimated_output
    if actual is None:
        stats.usage_unknown += 1
        rate_ledger.settle(booking, None)
        return
    rate_ledger.settle(booking, actual)


async def forward_chat(
    request: web.Request,
    lane: str,
    body: bytes,
    started: float,
    policy: RuntimePolicy,
    inbound_body: bytes,
) -> web.StreamResponse:
    """Retry an upstream request until it finishes or the client goes away.

    Every sleep happens with no fair-use permit held and no rate booking outstanding,
    so backpressure from NRP never occupies capacity that other requests could use.
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
                f"request_deadline lane={lane} attempts={attempt_number} "
                f"elapsed_seconds={elapsed:.1f}",
                flush=True,
            )
            raise web.HTTPBadGateway(
                text="upstream did not complete within the policy request limit"
            )

        booking = rate_ledger.reserve(policy.output_clamp(lane))
        if booking is None:
            stats.rate_waits += 1
            delay = rate_ledger.wait_seconds()
            print(
                f"rate_wait lane={lane} attempt={attempt_number} delay={delay:.1f}s "
                f"committed={rate_ledger.committed}",
                flush=True,
            )
            await asyncio.sleep(delay)
            continue

        try:
            outcome = await stream_attempt(
                request, session, lane, body, policy, attempt_number
            )
        except (TimeoutError, ClientConnectionError, ServerDisconnectedError) as exc:
            rate_ledger.settle(booking, 0)
            delay = retry_delay(_HeaderBag({}), attempt_number)
            print(
                f"upstream_retry lane={lane} attempt={attempt_number} "
                f"error={type(exc).__name__} delay={delay:.1f}s",
                flush=True,
            )
        else:
            if outcome.streamed:
                book_output(booking, outcome)
                print(
                    f"request_complete lane={lane} input_bytes={len(body)} "
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

            rate_ledger.settle(booking, 0)
            delay = outcome.retry_after if outcome.retry_after is not None else 1.0
            print(
                f"upstream_retry lane={lane} attempt={attempt_number} "
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

    app.router.add_get("/healthz", health)
    app.router.add_post(
        "/{lane:primary|long|subagent}/v1/chat/completions",
        chat,
    )
    app.router.add_get(
        "/{lane:primary|long|subagent}/v1/models",
        models,
    )
    return app


def main() -> None:
    validate_policy()
    web.run_app(create_app(), host="0.0.0.0", port=8080, access_log=None)  # noqa: S104


if __name__ == "__main__":
    main()
