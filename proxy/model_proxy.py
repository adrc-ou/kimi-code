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

The proxy also keeps an archive of the prompts it forwards. Every new user prompt becomes one
file under a host-visible directory, plus one stdout line pointing at it, and the directory is
emptied when the container stops. See ``log_prompt``.

Everything the proxy records goes to two places at once. A rotating file under ``LOG_DIR``
carries every event with its full detail - the upstream status, the error body, the quota
headers, and an id shared by every line of one client request - while stdout carries only the
terse line for the events named in ``LIVE_EVENTS``, which is what reaches the terminal the
stack was launched from. The file is the superset, so nothing is lost by the filtered view;
see ``event`` and ``setup_logging``.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import shutil
import time
import tomllib
from collections import OrderedDict, deque
from collections.abc import AsyncIterator, Sequence
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime
from email.utils import parsedate_to_datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

from aiohttp import (
    ClientConnectionError,
    ClientSession,
    ClientTimeout,
    ServerDisconnectedError,
    web,
)

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
# Zero, the default, means no per-request wall clock at all: a request runs until it completes,
# until its stream stalls past SOCK_READ_TIMEOUT, or until the client cancels it, and the UI owns
# that last one. This is also what the provider advises for scripts. An operator may still name a
# positive number to reinstate a cap; nothing else in the file treats 0 as a valid limit.
MAX_REQUEST_SECONDS = float(os.environ.get("MODEL_PROXY_MAX_REQUEST_SECONDS", "0"))

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


# ============================================================
# Two-tier logging: everything to a file, a chosen subset to the terminal
# ============================================================
#
# Every event is written to a rotating file under LOG_DIR carrying the detail that makes a
# stuck request explainable - the upstream status, its error body, the quota headers, how long
# the attempt ran, and an id shared by every line of one client request. Only the events named
# by LIVE_EVENTS are also written to stdout, and stdout is what ``./start.sh`` streams into the
# operator's terminal alongside every other container in the stack.
#
# The two renderings are deliberately different. The terminal line stays exactly as terse as it
# has always been, so adding diagnostic detail to the file cannot bury the live signal that the
# terse set was chosen to preserve. Nothing is lost by that split, because the file is the
# superset: whatever the terminal shows, a grep of the file finds, plus what the upstream
# actually said.
#
# ``MODEL_PROXY_LIVE_EVENTS`` re-draws the line without a code change: a comma-separated list of
# event names, or ``all`` to mirror the file verbatim. Promote a name while reproducing a fault
# and drop it again afterwards.

#: The archive directory inside this container, and the same directory's path on the host,
#: which is the one an operator can open without going through Docker. The container's root
#: filesystem is read-only, so the file exists only because compose binds this directory.
LOG_DIR = Path(os.environ.get("MODEL_PROXY_LOG_DIR", "/var/log/model-proxy"))
LOG_HOST_DIR = os.environ.get("MODEL_PROXY_LOG_HOST_DIR") or str(LOG_DIR)
LOG_FILE_NAME = "model-proxy.log"
LOG_MAX_BYTES = int(os.environ.get("MODEL_PROXY_LOG_MAX_BYTES", str(8 * 1024 * 1024)))
LOG_BACKUPS = int(os.environ.get("MODEL_PROXY_LOG_BACKUPS", "4"))

#: How many bytes of failure dumps to keep in total, oldest dropped first. Bounded by size
#: rather than by count because the sessions that produce several dumps are precisely the ones
#: whose bodies are megabytes: a count bound would still let them grow without limit.
LOG_DUMP_BUDGET_BYTES = int(
    os.environ.get("MODEL_PROXY_LOG_DUMP_BUDGET_BYTES", str(100 * 1024 * 1024))
)

#: An inline image is carried as base64 inside the request body, so its bytes ride along on
#: every later step of the session that produced them. Measured on a real archived request:
#: 3,721,208 of 4,025,030 body bytes were images, while all text plus every tool schema came
#: to ~304 KB. Kimi's own byte budget does not bound this, because `region` and
#: `full_resolution` reads are exempt from it and a pasted attachment is not a read at all.
#: The proxy is the last point that sees the body and the only one this harness owns, so
#: eviction happens here: once an assistant turn has answered an image, the bytes are replaced
#: in place by a handle the agent can re-read with. See ``evict_answered_images``.
#: Off unless the operator opted in. ``./start.sh`` asks on every interactive launch and writes
#: the answer here; a proxy started some other way must not begin rewriting model input on an
#: assumption nobody confirmed.
IMAGE_EVICTION_ENABLED = os.environ.get("MODEL_PROXY_IMAGE_EVICTION", "0").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}

#: Smaller images stay. Icons and thumbnails are cheap enough that rewriting them trades a
#: trivial number of bytes for a handle the model then has to reason about.
IMAGE_EVICTION_MIN_BYTES = int(
    os.environ.get("MODEL_PROXY_IMAGE_EVICTION_MIN_BYTES", "32768")
)

#: How much of the digest to show. Enough to be unique across a session's images at the
#: collision rate that matters here, short enough to be convenient to pass back.
IMAGE_HANDLE_PREFIX = 12

#: Both spellings are live in this stack, so both are read. See ``_image_urls_of``.
IMAGE_PART_FIELD_NAMES = ("image_url", "imageUrl")

#: A failure dump is a whole request, which for a multimodal body is megabytes, so this
#: ceiling is what keeps a few of them from outgrowing the host directory they land in. A
#: request over the cap is still described in the log; only its replay file is skipped.
LOG_DUMP_MAX_BYTES = int(
    os.environ.get("MODEL_PROXY_LOG_DUMP_MAX_BYTES", str(64 * 1024 * 1024))
)

#: How much of an upstream error body one event may carry. A gateway's whole complaint fits;
#: the bound matters because that body arrives from the network rather than from this plan.
LOG_ERROR_EXCERPT_BYTES = 4096

#: Events whose terse line also reaches stdout. This is the set the proxy has always printed,
#: carried forward unchanged so the launch terminal reads the same as it did before the file
#: existed. Everything below is also in the file whether or not it is listed here.
DEFAULT_LIVE_EVENTS = frozenset(
    {
        "input_guard",
        "lane_config",
        "log_file",
        "log_unavailable",
        "policy_drift",
        "policy_drift_clear",
        "prompt_log",
        "prompt_log_purge",
        "prompt_log_purged",
        "rate_wait",
        "request_complete",
        "request_deadline",
        "response_limit",
        "startup_refused",
        "stream_stall",
        "upstream_retry",
        "usage_reporting_unsupported",
    }
)


def _configured_live_events() -> tuple[bool, frozenset[str]]:
    """Return ``(mirror_everything, event names)`` from ``MODEL_PROXY_LIVE_EVENTS``."""
    configured = os.environ.get("MODEL_PROXY_LIVE_EVENTS", "").strip().lower()
    if not configured:
        return False, DEFAULT_LIVE_EVENTS
    if configured in {"all", "*"}:
        return True, DEFAULT_LIVE_EVENTS
    return False, frozenset(name.strip() for name in configured.split(",") if name.strip())


LIVE_ALL, LIVE_EVENTS = _configured_live_events()

#: Set once per client request and read by every event that request raises, so lines from the
#: concurrent sessions sharing this one proxy can be told apart after the fact.
REQUEST_ID: ContextVar[str] = ContextVar("request_id", default="")

#: Why the file sink is unavailable, or None when it is open. Surfaced on /healthz rather than
#: only at startup, because a bind that was dropped mid-life should not read as a quiet proxy.
LOG_UNAVAILABLE: str | None = None

_LOG = logging.getLogger("model_proxy")


def _one_line(value: object) -> str:
    """Render a field value as one space-free token.

    An event is one line by contract: the terminal is a scrollback an operator reads, and the
    file is grepped. Upstream bodies and quoted JSON both carry newlines, so they are escaped
    rather than allowed to split a record in two.
    """
    if isinstance(value, BaseException):
        text = f"{type(value).__name__}: {value}"
    else:
        text = str(value)
    text = text.replace("\\", "\\\\").replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
    return text if " " not in text else f"'{text}'"


def event(name: str, terse: Sequence[str] = (), /, **fields: object) -> None:
    """Record one event in the file, and in the terminal when its kind is live.

    ``terse`` names the fields the terminal line carries, in order; omitting it sends every
    field, which is right for the one-off notices that already read as short as they can. The
    file always carries everything, in the order written here.

    The request id goes to the file only. A live line keeps the exact shape it had before this
    seam existed, because the terminal is read by eye and the file is read by grep, and the id
    that ties one client request's lines together is worth nothing in the former. Setting
    ``MODEL_PROXY_LIVE_EVENTS=all`` mirrors the file verbatim instead, id included.
    """
    request_id = REQUEST_ID.get()
    detail = [name]
    if request_id:
        detail.append(f"rid={request_id}")
    detail += [f"{key}={_one_line(value)}" for key, value in fields.items()]
    full = " ".join(detail)
    _LOG.debug(full)

    if LIVE_ALL:
        print(full, flush=True)
        return
    if name not in LIVE_EVENTS:
        return
    if not fields:
        print(name, flush=True)
        return

    chosen = tuple(terse) or tuple(fields)
    print(
        " ".join(
            [name] + [f"{key}={_one_line(fields[key])}" for key in chosen if key in fields]
        ),
        flush=True,
    )


def setup_logging() -> str | None:
    """Open the file sink and return the reason it could not be opened, or None.

    Never fatal. A proxy that cannot write its log still enforces the plan and still answers
    the terminal; losing the diagnostic file deserves a warning, not a stack that will not
    start. RotatingFileHandler is not thread-safe and one dump is written from a worker
    thread, so the archive path keeps its own plain write and only these records use the
    handler.
    """
    global LOG_UNAVAILABLE

    _LOG.setLevel(logging.DEBUG)
    _LOG.propagate = False
    # A handler whose emit() raises writes a multi-line "Logging error" traceback to stderr,
    # which start.sh puts on the operator's terminal beside the launch output. Losing the file
    # is already reported by LOG_UNAVAILABLE and by /healthz; it must not also cost the
    # terminal its readability for the rest of the launch.
    logging.raiseExceptions = False
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        # Not delay=True. Deferring the open past this try block is what let a directory that
        # exists but cannot be opened — read-only remount, foreign owner, ENOSPC — look
        # healthy here and then fail on every single emit.
        handler = RotatingFileHandler(
            LOG_DIR / LOG_FILE_NAME,
            maxBytes=LOG_MAX_BYTES,
            backupCount=LOG_BACKUPS,
            encoding="utf-8",
        )
    except (OSError, ValueError) as exc:
        LOG_UNAVAILABLE = f"{type(exc).__name__}: {exc}"
        return LOG_UNAVAILABLE

    formatter = logging.Formatter(
        fmt="%(asctime)s.%(msecs)03dZ %(message)s", datefmt="%Y-%m-%dT%H:%M:%S"
    )
    formatter.converter = time.gmtime
    handler.setFormatter(formatter)
    _LOG.addHandler(handler)
    event(
        "log_open",
        directory=LOG_DIR,
        host_directory=LOG_HOST_DIR,
        file=LOG_DIR / LOG_FILE_NAME,
        max_bytes=LOG_MAX_BYTES,
        backups=LOG_BACKUPS,
        live="all" if LIVE_ALL else ",".join(sorted(LIVE_EVENTS)),
    )
    return None


# ============================================================
# Outgoing prompt archive
# ============================================================
#
# Every new user prompt is archived as one file holding the request the proxy is about to
# send upstream, and one stdout line naming where it went. The archive is always on: what
# keeps it readable is that a request only qualifies when it *introduces* a prompt, so the
# many calls a single turn makes - each of which re-sends the whole conversation - cost one
# file between them instead of one file each.
#
# The directory is a host bind, so the operator can open the path stdout names without
# going through Docker. It holds conversation text, which is why its contents are purged on
# shutdown, and why it is mounted into this container alone.
#
# A dump is a request, not a secret store: the provider key, the internal bearer, and the
# cache salt are replaced by their redaction markers on the way to disk, exactly as they
# were on the way to stdout.

#: Where the archive lives inside this container, and the same directory's path on the host,
#: which is the one worth printing. Unset, the host path is the container path, which is
#: correct only when nothing was bind-mounted.
PROMPT_LOG_DIR = Path(os.environ.get("MODEL_PROXY_PROMPT_LOG_DIR", "/prompt-log"))
PROMPT_LOG_HOST_DIR = Path(
    os.environ.get("MODEL_PROXY_PROMPT_LOG_HOST_DIR") or str(PROMPT_LOG_DIR)
)

#: How much of the prompt the stdout line carries. Enough to recognise the turn, short
#: enough that the line stays one line.
PROMPT_SUMMARY_CHARS = 65

#: The harness injects its own notes as user-role messages, so a user message built like
#: that is not the operator speaking and does not open a file.
REMINDER_TAG = "<system-reminder>"

#: Keys of the prompts already archived, oldest first. The bound is generous rather than
#: tight: it exists so a long-lived proxy cannot accumulate one string per prompt forever,
#: and any window wider than the number of turns in a session keeps retries and multi-step
#: turns collapsed.
ARCHIVED_PROMPTS = 64

_archived: OrderedDict[str, None] = OrderedDict()


def message_text(message) -> str:
    """The plain text of one chat message, whether its content is a string or content parts."""
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            part["text"]
            for part in content
            if isinstance(part, dict) and isinstance(part.get("text"), str)
        )
    return ""


def prompt_of(body: bytes) -> tuple[str, int] | None:
    """The user prompt this request introduces, with that message's position beside it.

    ``None`` means the request introduces nothing: the newest user message is a harness
    reminder, or there is no user message at all, or the body is not a chat request.
    Looking only at the *newest* user message is what makes this a new-prompt detector;
    scanning the whole array would qualify every call of every turn, because every call
    re-sends the prompts that turn already contains.
    """
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None

    if not isinstance(payload, dict) or not isinstance(payload.get("messages"), list):
        return None

    messages = payload["messages"]
    for position, message in enumerate(reversed(messages), start=1):
        if not (isinstance(message, dict) and message.get("role") == "user"):
            continue
        text = message_text(message)
        if not text or REMINDER_TAG in text:
            return None
        return (text, len(messages) - position + 1)

    return None


def prompt_summary(text: str) -> str:
    """One greppable line carrying the opening of the prompt."""
    flat = " ".join(text.split())
    if len(flat) > PROMPT_SUMMARY_CHARS:
        return flat[:PROMPT_SUMMARY_CHARS] + "…"
    return flat


def request_dump(method: str, url: str, headers, body: bytes) -> str:
    """The outbound request as text: request line, headers, then the body pretty-printed."""
    lines = [f"{method} {url} HTTP/1.1"]
    lines += [f"{name}: {value}" for name, value in debug_headers(headers).items()]
    lines += ["", debug_body(body, redact_cache_salt=True), ""]
    return "\n".join(lines)


def new_prompt(key: str) -> bool:
    """Whether this prompt has not been archived yet, remembering it if it has not."""
    if key in _archived:
        _archived.move_to_end(key)
        return False
    _archived[key] = None
    while len(_archived) > ARCHIVED_PROMPTS:
        _archived.popitem(last=False)
    return True


def write_dump(
    method: str,
    url: str,
    headers: dict[str, str],
    body: bytes,
    name: str,
    directory: Path | None = None,
) -> None:
    """Render and store one request; the caller decides what a failure is worth.

    ``directory`` defaults to the prompt archive. A failure dump goes to the log directory
    instead, which outlives the container, so a fault can still be examined after the stack
    that produced it has stopped.
    """
    destination = directory or PROMPT_LOG_DIR
    destination.mkdir(parents=True, exist_ok=True)
    (destination / name).write_text(
        request_dump(method, url, headers, body), encoding="utf-8"
    )


async def log_prompt(request: web.Request, lane: LanePolicy, body: bytes) -> None:
    """Archive one qualifying outgoing prompt; never let the archive cost a request."""
    found = prompt_of(body)
    if found is None:
        return

    text, depth = found
    key = hashlib.sha256(f"{depth}\n{text}".encode()).hexdigest()
    if not new_prompt(key):
        return

    # The request line and headers describe a call that carries no credential of ours, so
    # they are rebuilt from the inbound request the same way the attempt builds them, with
    # the provider key standing in as its own redaction marker.
    headers = filtered_request_headers(request, "<REDACTED>")
    now = datetime.now().astimezone()
    name = f"prompt-{now.strftime('%Y%m%dT%H%M%S')}-{now.microsecond:06d}-{key[:8]}.txt"

    try:
        # A multimodal body can be hundreds of megabytes, and pretty-printing it before
        # writing it out is far too much work to attempt on the loop that paces every lane.
        await asyncio.to_thread(
            write_dump,
            request.method,
            f"{lane.base_url}/v1/chat/completions",
            headers,
            body,
            name,
        )
    except (OSError, ValueError) as exc:
        # A ValueError here can only come from encoding text the provider accepted and the
        # local filesystem would not take; either way the prompt is worth a log line and
        # nothing more.
        event("prompt_log", lane=lane.name, error=exc)
        return

    event(
        "prompt_log",
        ("lane", "time", "chars", "file", "prompt"),
        lane=lane.name,
        time=now.isoformat(timespec="microseconds"),
        chars=len(text),
        file=PROMPT_LOG_HOST_DIR / name,
        prompt=prompt_summary(text),
        depth=depth,
        key=key[:8],
    )


def purge_prompt_log() -> int:
    """Empty the archive directory, leaving the mount point itself in place."""
    try:
        entries = sorted(PROMPT_LOG_DIR.iterdir())
    except OSError:
        # No directory at all is the normal state of a stack that never archived anything.
        return 0

    removed = 0
    for entry in entries:
        try:
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry)
            else:
                entry.unlink()
            removed += 1
        except OSError as exc:
            event("prompt_log_purge", path=entry, error=exc)

    return removed


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
    #: The lane Kimi's ``default_model`` is expected to name. Chosen at launch, and the only
    #: lane whose alias the drift check accepts for it. The plan always carries one; this
    #: default only matches the harness's shipped default lane for a hand-made plan.
    agent_lane: str = "long"


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


def verify_credentials(policy: RuntimePolicy) -> None:
    """Read every credential the plan names once, before the proxy starts listening.

    Without this, a missing or empty mount first surfaces on the request that needs that
    lane, which makes a failed launch look like a transient upstream error mid-session.
    Values are read and discarded: nothing here may print one, and the request path keeps
    reading per request so a remount is picked up without a restart.
    """
    for secret_name in sorted({lane.secret_name for lane in policy.lanes.values()}):
        upstream_credential(secret_name)


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
    agent = policy.lanes.get(policy.agent_lane)
    if agent is None:
        raise PolicyDrift(
            f"model policy designates agent lane {policy.agent_lane!r}, which it does not publish"
        )
    if config.get("default_model") != agent.alias:
        raise PolicyDrift(
            f"Kimi default model {config.get('default_model')!r} is not the designated "
            f"agent lane model {agent.alias!r}"
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
        agent_lane=str(plan.get("agent_lane") or "long"),
    )
    enforce_kimi_configuration(policy, config_path)
    return policy


def counter_limits(counter: dict) -> tuple[int | None, int | None, int | None, int | None]:
    """Return (context budget, ceiling, exclusivity threshold, request limit) a counter imposes.

    The budget is the ceiling reduced by the declared margin, so it is what the gate steers
    traffic towards; the ceiling is the provider's own published number, and it is the larger
    figure a lone request is still allowed to occupy. Only the second bounds whether a request
    too big to share the pool may run at all.
    """
    family = counter.get("family")
    if family == "context":
        return (
            counter.get("budget"),
            counter.get("ceiling"),
            counter.get("exclusive_at"),
            counter.get("max"),
        )
    if family == "count":
        return None, None, None, counter.get("max")
    return None, None, None, None


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
        "MODEL_PROXY_INPUT_GUARD_PERCENT": INPUT_GUARD_PERCENT,
        "MODEL_PROXY_MEDIA_TOKEN_ESTIMATE": MEDIA_TOKEN_ESTIMATE,
        "MODEL_PROXY_LOG_MAX_BYTES": LOG_MAX_BYTES,
        "MODEL_PROXY_LOG_DUMP_MAX_BYTES": LOG_DUMP_MAX_BYTES,
        "MODEL_PROXY_LOG_DUMP_BUDGET_BYTES": LOG_DUMP_BUDGET_BYTES,
        "MODEL_PROXY_IMAGE_EVICTION_MIN_BYTES": IMAGE_EVICTION_MIN_BYTES,
    }
    for name, value in numeric_values.items():
        if value <= 0:
            raise RuntimeError(f"{name} must be positive; got {value}")
    # The one knob for which "no limit" is a legitimate setting, so it is validated apart from
    # the map above rather than loosening the rule for everything in it. A negative is still a
    # typo, and a typo here would mean every request trips its deadline the instant it starts.
    if MAX_REQUEST_SECONDS < 0:
        raise RuntimeError(
            f"MODEL_PROXY_MAX_REQUEST_SECONDS must be 0 for unlimited or a positive number;"
            f" got {MAX_REQUEST_SECONDS}"
        )
    if INPUT_GUARD_PERCENT < 100:
        raise RuntimeError("MODEL_PROXY_INPUT_GUARD_PERCENT must be at least 100")
    # No rotation history is a legitimate setting - one file, truncated at the cap - so this
    # is bounded apart from the positive map above rather than folded into it.
    if LOG_BACKUPS < 0:
        raise RuntimeError(f"MODEL_PROXY_LOG_BACKUPS must not be negative; got {LOG_BACKUPS}")
    # The budget has to hold at least one whole dump, or the act of pruning would evict the
    # dump just written and leave nothing behind but the event that claims it was saved.
    if LOG_DUMP_BUDGET_BYTES < LOG_DUMP_MAX_BYTES:
        raise RuntimeError(
            f"MODEL_PROXY_LOG_DUMP_BUDGET_BYTES={LOG_DUMP_BUDGET_BYTES} cannot hold one dump of "
            f"MODEL_PROXY_LOG_DUMP_MAX_BYTES={LOG_DUMP_MAX_BYTES}"
        )

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
            budget, ceiling, exclusive_at, _limit = counter_limits(counter)
            if budget is None:
                continue
            if ceiling is not None and budget > ceiling:
                raise RuntimeError(
                    f"counter {counter_id} budgets {budget} tokens above the "
                    f"{ceiling}-token aggregate ceiling its provider publishes"
                )
            # No price may fall between the two. A reservation over the budget is served alone
            # only while it fits the ceiling, and above it only once the threshold is reached,
            # so a threshold set above its own ceiling would leave the prices between them with
            # no admission rule at all. Providers publish one fraction for both, so agreeing is
            # the normal case; a provider that ever split them this way is a contradiction the
            # harness cannot serve and must refuse to start on.
            if (
                ceiling is not None
                and exclusive_at is not None
                and exclusive_at > ceiling
            ):
                raise RuntimeError(
                    f"counter {counter_id} sets exclusivity at {exclusive_at} tokens, above the "
                    f"{ceiling}-token aggregate ceiling its provider publishes"
                )
            # A reservation the budget cannot hold is not automatically unservable: the
            # provider's aggregate rule bounds the requests it has *in combination*, so a
            # reservation that fits the published ceiling on its own is served alone, and a
            # reservation at or above the exclusivity threshold is served alone by the
            # provider's own terms. What cannot be served at all is a reservation above the
            # ceiling that the threshold nonetheless does not licence to run alone; the two
            # numbers coincide for NRP, so only contradictory provider terms can produce it.
            if lane.reservation > budget and not (
                lane.reservation <= ceiling
                or (exclusive_at is not None and lane.reservation >= exclusive_at)
            ):
                raise RuntimeError(
                    f"{lane.alias} reserves {lane.reservation} tokens but counter {counter_id} "
                    f"budgets {budget} of a {ceiling}-token ceiling without a threshold that "
                    f"large; no request on this lane could ever be admitted"
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
        event("policy_drift", error=detail)
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
        event("policy_drift_clear")
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


def priced_reservation(lane: LanePolicy, estimate: int) -> int:
    """What one request is charged for its fair-use permit, from what it is actually carrying.

    A lane's own reservation is the worst case its input cap and output clamp allow, which is the
    right number for proving at startup that the lane can be served at all and the wrong one for
    admitting a request that uses a tenth of its window. NRP's aggregate and exclusivity rules are
    both written about the context a request utilizes, so charging the worst case makes a short
    request on a wide lane look large enough to run alone and hold the whole allowance while it
    does not need it.

    The estimate is the same figure the input guard already measured, and the lane reservation
    stays the ceiling: a price may be fairer than the worst case, never bigger than it. That
    ordering is what keeps every startup check that was proved against the static reservation
    true of the charged one.
    """
    return min(lane.reservation, max(estimate, 1) + lane.output_clamp)


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


@dataclass
class Eviction:
    """What one body's eviction did, for the record and for nothing else."""

    images: int = 0
    bytes_freed: int = 0
    handles: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return self.images > 0


def _looks_like_mime(value: str) -> bool:
    """Whether a data URL prefix actually carries a media type.

    Deliberately strict: this value is interpolated into the note the model reads, and the
    alternative — trusting whatever precedes the comma — lets a malformed URL paste payload
    bytes back into the text written to save space.
    """
    return bool(re.fullmatch(r"image/[a-z0-9.+-]{1,32}", value.lower()))


def _part_text(part: object) -> str:
    if not isinstance(part, dict) or part.get("type") != "text":
        return ""
    text = part.get("text")
    return text if isinstance(text, str) else ""


def _answers_after(messages: list, index: int) -> bool:
    """Whether some later assistant turn said something about this image.

    This is the whole eviction rule, and it is monotone by construction: history only grows,
    so once an image has been answered it stays answered. Each image is therefore rewritten at
    most once, which keeps the request prefix stable afterwards instead of shifting a
    keep-newest-N window on every step and invalidating the provider's cache each time.

    An image with no following assistant text is left untouched, which is what protects the
    attachment the user just pasted and the read the model has not yet answered.
    """
    for later in messages[index + 1 :]:
        if not isinstance(later, dict) or later.get("role") != "assistant":
            continue
        content = later.get("content")
        if isinstance(content, str) and content.strip():
            return True
        if isinstance(content, list) and any(_part_text(part).strip() for part in content):
            return True
    return False


def _image_urls_of(part: dict) -> list[str]:
    """Every inline url this image part carries, under either spelling.

    A part holding both field names with different images is malformed rather than rare, and
    replacing it after hashing only the first would destroy the second without a trace.
    """
    urls = []
    for name in IMAGE_PART_FIELD_NAMES:
        value = part.get(name)
        if isinstance(value, dict) and isinstance(value.get("url"), str):
            urls.append(value["url"])
    return urls


def _eviction_note(url: str) -> tuple[str, int, str] | None:
    """The replacement text for one inline image, the bytes it carried, and its handle."""
    if not url.startswith("data:"):
        # A remote reference is already small; the provider fetches it, we do not.
        return None
    head, comma, payload = url.partition(",")
    if not comma:
        return None
    size = len(payload)
    if size < IMAGE_EVICTION_MIN_BYTES:
        return None
    # Only trust a mime type that looks like one. `data:,<40000 chars>` has no separator, and
    # taking everything after `data:` as the type would paste the payload straight back into
    # the note we are writing to save space.
    declared = head[5:].split(";", 1)[0]
    mime = declared if _looks_like_mime(declared) else "unknown"
    # The digest is of the base64 exactly as it travelled, so the tool that resolves it can
    # re-encode a file on disk and compare, without needing to have seen this request.
    digest = hashlib.sha256(payload.encode()).hexdigest()[:IMAGE_HANDLE_PREFIX]
    handle = f"sha256:{digest}"
    # The digest is of the base64 exactly as it travelled, so the tool that resolves it can
    # re-encode a file on disk and compare, without needing to have seen this request.
    digest = hashlib.sha256(payload.encode()).hexdigest()[:IMAGE_HANDLE_PREFIX]
    handle = f"sha256:{digest}"
    # Named the way the client can actually find it. An MCP tool is surfaced as
    # `mcp__<server>__<tool>`, and an instruction to "call analyze_image" is a handle the model
    # has no way to act on if it never sees a tool by that bare name — which is exactly the
    # situation that produces the pointless re-read loop this note exists to prevent.
    # "may still be available", not "is": only images that live under the session's media
    # directories are guaranteed to resolve, and a crop the agent wrote to scratch space is
    # gone once that space is. Asserting availability the tool cannot promise is how an agent
    # ends up uselessly re-requesting bytes that no longer exist anywhere.
    note = (
        f"[image removed from context after analysis: {size} base64 chars, {mime}, "
        f'handle "{handle}". It may still be on this machine: call the analyze_image tool '
        f'(named mcp__images__analyze_image wherever tools are namespaced) with '
        f'handle="{handle}" and prompt=<what you want from it> to see it again, '
        "then continue the task you were working on.]"
    )
    return note, size, handle


def evict_answered_images(payload: dict) -> Eviction:
    """Replace already-answered inline images in a request payload with re-read handles.

    Mutates the parsed body in place. Content parts are swapped text-for-image; no message is
    added, removed, reordered, or re-roled, so message count, ordering, and every
    ``tool_call_id`` pairing survive untouched. Returning the count lets the caller decide
    whether the body actually needs re-serialising.
    """
    report = Eviction()
    messages = payload.get("messages")
    if not isinstance(messages, list) or not IMAGE_EVICTION_ENABLED:
        return report

    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        if not _answers_after(messages, index):
            continue

        for position, part in enumerate(content):
            if not isinstance(part, dict) or part.get("type") != "image_url":
                continue
            notes = []
            for url in _image_urls_of(part):
                note = _eviction_note(url)
                if note is not None:
                    notes.append(note)
            if not notes:
                continue
            text = "\n".join(entry[0] for entry in notes)
            existing = _part_text(part)
            # Built fresh rather than copied: a copied part would carry `detail` or
            # `cache_control` over from the image block onto a text block, where they are not
            # legal and a strict validator rejects them.
            replacement: dict[str, object] = {"type": "text"}
            if existing:
                replacement["text"] = f"{existing}\n{text}"
            else:
                replacement["text"] = text
            content[position] = replacement
            for _text, size, handle in notes:
                report.images += 1
                report.bytes_freed += size
                report.handles += (handle,)

    return report


def evict_body_images(body: bytes) -> tuple[bytes, Eviction]:
    """Evict from a raw request body, returning the bytes to forward.

    The substring guard first: a body with no image part is not parsed a second time and is
    forwarded exactly as it arrived. A body that parses but evicts nothing also comes back
    untouched, because re-serialising it would perturb key order and byte count for no gain.
    Anything that fails to parse is passed through for ``rewrite_model`` to reject on the
    request path, where the error is the caller's business.
    """
    if not IMAGE_EVICTION_ENABLED:
        return body, Eviction()
    if not any(name.encode() in body for name in IMAGE_PART_FIELD_NAMES):
        return body, Eviction()
    try:
        # Same duplicate-key rule as rewrite_model: parsing leniently here would let a body
        # that rewrite_model must reject slip through once eviction had made it valid JSON
        # again, so the two paths would disagree about the same bytes.
        payload = json.loads(body, object_pairs_hook=_object_no_duplicates)
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        return body, Eviction()
    if not isinstance(payload, dict):
        return body, Eviction()

    report = evict_answered_images(payload)
    if not report:
        return body, report
    try:
        return json.dumps(payload, separators=(",", ":"), allow_nan=False).encode(), report
    except (TypeError, ValueError):
        return body, Eviction()


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

    A counter carries whichever of four constraints the provider publishes for its scope: an
    aggregate in-flight token budget, the published ceiling that budget was taken from, a
    threshold at or above which a single request must run alone, and a hard request-count
    ceiling. ``None`` means the provider stated nothing of that kind, so the constraint is
    simply absent - a provider with only a concurrency limit gets a counting semaphore, and one
    with only a token budget gets a bin.

    Lanes are not special-cased. A reservation that runs alone - because the provider says a
    request that large may not overlap anything, or because it is larger than the entire budget
    and so could not overlap anything even in principle - waits for an idle counter; everything
    else is admitted while the sum of live reservations stays inside the budget. A reservation
    is what one request was priced at rather than the largest its lane could ever be, so it is
    the request that decides whether it runs alone, not the route it arrived on.
    Which lane yields to which is a fairness choice, not a policy rule: subagents give way to a
    primary that is already queued, so an idle operator cannot be starved by a fan-out of
    children.
    """

    def __init__(
        self,
        counter_id: str,
        *,
        budget: int | None = None,
        ceiling: int | None = None,
        exclusive_at: int | None = None,
        limit: int | None = None,
        subagent_limit: int | None = None,
    ) -> None:
        self.counter_id = counter_id
        self.condition = asyncio.Condition()
        self.budget = budget
        self.ceiling = ceiling
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

    def runs_alone(self, reservation: int) -> bool:
        """Whether a reservation this size can only ever be served with the counter empty.

        Two independent reasons reach the same answer. The provider may say a request at or
        above its own threshold runs alone, or the request may simply be larger than the whole
        aggregate budget, in which case sharing is arithmetic rather than policy: nothing that
        ever finishes can make room for it, so admitting it beside anything would wait forever.
        Both cases stay compliant, because a provider's aggregate limit bounds requests held in
        combination, and a lone request within the published ceiling breaks no such bound.

        The second case is the common one in practice. The budget is the provider's fraction
        reduced by the declared margin, so any lane wide enough to reach the threshold also
        carries a band of requests that overshoot the margin while fitting the provider's own
        number, and per-request pricing puts real traffic in that band. Treating it as ordinary
        shared traffic instead of solitude strands those requests permanently, which is why
        :func:`validate_policy` requires the threshold to sit at or below the ceiling: the two
        rules together leave no price that can neither share nor run alone.
        """
        if self.is_exclusive(reservation):
            return True
        if self.budget is None or reservation <= self.budget:
            return False
        return self.ceiling is None or reservation <= self.ceiling

    def _admits(self, lane: str, reservation: int) -> bool:
        if self.limit is not None and self.active >= self.limit:
            return False
        if self.subagent_limit is not None and self.active_subagents >= self.subagent_limit:
            return False
        if self.runs_alone(reservation):
            return self.active == 0
        # Only children yield, and only to somebody else's turn: a primary-like request that
        # counted its own queue entry as a reason to wait could never be admitted at all.
        if lane == "subagent" and self.waiting_primary:
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
            "context_ceiling": self.ceiling,
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
    images_evicted: int = 0
    image_bytes_freed: int = 0


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
                budget, ceiling, exclusive_at, limit = counter_limits(counter)
                subagent_limit = fan_out if "subagent" in lanes else None
                gate = self.gates.get(counter_id)
                if gate is None:
                    self.gates[counter_id] = FairUseGate(
                        counter_id,
                        budget=budget,
                        ceiling=ceiling,
                        exclusive_at=exclusive_at,
                        limit=limit,
                        subagent_limit=subagent_limit,
                    )
                else:
                    gate.budget = budget
                    gate.ceiling = ceiling
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

    def price_runs_alone(self, lane: LanePolicy, reservation: int) -> bool:
        """True when a request priced this high may only be served against an empty counter.

        Judged on the price, not the lane: this is the question whose answer explains why a
        request had to wait for an idle counter at all, and a wide lane still serves short
        requests concurrently.
        """
        return any(gate.runs_alone(reservation) for gate in self.gates_for(lane))

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
async def admission(
    lane: LanePolicy, enforcement: Enforcement, reservation: int | None = None
) -> AsyncIterator[None]:
    """Hold every counter the plan says this lane's traffic is charged to.

    ``reservation`` is what this one request costs; omitting it charges the lane's worst case.
    Each gate adds and later releases the same figure it was handed, so the value is captured
    once here rather than recomputed, and an unbalanced ledger is impossible by construction.
    """
    cost = lane.reservation if reservation is None else reservation
    async with contextlib.AsyncExitStack() as stack:
        for gate in enforcement.gates_for(lane):
            await stack.enter_async_context(gate.slot(lane.name, cost))
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
            # Which lane the main agent was launched on: the alias Kimi's default_model must
            # carry, and therefore the lane a session's own traffic is metered against.
            "agent_lane": policy.agent_lane,
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
            "log": {
                # The host path is deliberately absent: this endpoint is reachable from the
                # agent container, and the log directory can hold request bodies. The operator
                # learns the path from the `log_file` line at launch, which goes to stdout.
                "container_directory": str(LOG_DIR),
                "unavailable": LOG_UNAVAILABLE,
                "max_bytes": LOG_MAX_BYTES,
                "backups": LOG_BACKUPS,
                "live_events": "all" if LIVE_ALL else sorted(LIVE_EVENTS),
            },
            "input_guard_percent": INPUT_GUARD_PERCENT,
            "sock_read_timeout_seconds": SOCK_READ_TIMEOUT,
            # Null, not a sentinel number: an operator reading this should be able to tell that
            # no wall clock is configured rather than decode a made-up one.
            "max_request_seconds": MAX_REQUEST_SECONDS or None,
            "usage_reporting_supported": USAGE_SUPPORTED,
            "stats": {
                "rate_waits": stats.rate_waits,
                "guard_rejections": stats.guard_rejections,
                "usage_unknown": stats.usage_unknown,
                "usage_unsupported": stats.usage_unsupported,
                "deadline_stops": stats.deadline_stops,
                "stream_stalls": stats.stream_stalls,
                "policy_errors": stats.policy_errors,
                "images_evicted": stats.images_evicted,
                "image_bytes_freed": stats.image_bytes_freed,
            },
            "image_eviction": {
                "enabled": IMAGE_EVICTION_ENABLED,
                "min_base64_bytes": IMAGE_EVICTION_MIN_BYTES,
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
    # One id for everything this request causes, so that lines from the sessions sharing this
    # proxy can be separated afterwards. Each aiohttp handler runs in its own task, and a
    # ContextVar set here is visible only within that task's copy of the context.
    REQUEST_ID.set(secrets.token_hex(4))

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
        # The record that the request arrived at all, which is the first question a stalled
        # session asks and was previously only answerable by watching the gate counters move.
        event("request_received", lane=lane_name, input_bytes=len(inbound_body))

        # Eviction precedes pricing on purpose: the guard and the permit must describe the
        # request actually sent upstream. A body trimmed from 5 MB to 1.4 MB that is still
        # priced as 5 MB would be charged for bytes the provider never sees, and could be
        # forced to run alone for a size it no longer has.
        outbound_body, eviction = evict_body_images(inbound_body)
        if eviction:
            stats.images_evicted += eviction.images
            stats.image_bytes_freed += eviction.bytes_freed
            event(
                "images_evicted",
                lane=lane_name,
                images=eviction.images,
                bytes_freed=eviction.bytes_freed,
                body_bytes_before=len(inbound_body),
                body_bytes_after=len(outbound_body),
                handles=list(eviction.handles),
            )

        guard = lane.max_input * INPUT_GUARD_PERCENT // 100
        estimate, media = estimate_input_tokens(outbound_body)
        if estimate > guard:
            stats.guard_rejections += 1
            # input_bytes stays the live field and keeps meaning "what the client sent", as it
            # did before eviction existed; the post-eviction size is alongside it, because after
            # eviction those are different numbers and only one of them tripped the guard.
            event(
                "input_guard",
                ("lane", "estimate", "guard", "media_items", "input_bytes"),
                lane=lane_name,
                estimate=estimate,
                guard=guard,
                media_items=media,
                input_bytes=len(inbound_body),
                evicted_bytes=len(outbound_body),
            )
            # max_size and actual_size are token counts here, not bytes: the guard is
            # a context allowance, and text overrides the byte-worded default.
            raise web.HTTPRequestEntityTooLarge(
                guard,
                estimate,
                text="request input exceeds the enforced lane context allowance",
            )

        outbound_body = rewrite_model(outbound_body, request, lane)

        await log_prompt(request, lane, outbound_body)

        return await forward_chat(
            request,
            lane,
            outbound_body,
            started,
            # Re-derived from the body actually sent, not the one received: rebuilding from the
            # inbound copy would reinstate every image this request just evicted.
            outbound_body,
            # The guard's estimate is the best input figure available before the request
            # runs; a ledger that meters input charges itself from it and settles to the
            # prompt tokens the response reports.
            Cost(input=estimate, output=lane.output_clamp),
            priced_reservation(lane, estimate),
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

    ``status`` and ``error_excerpt`` carry what the gateway actually said. They matter most on
    the paths that hand the client nothing - a retryable status, a usage rejection - where the
    response is otherwise closed and forgotten, and where the reason a request never completes
    is therefore otherwise unrecoverable.
    """

    response: web.StreamResponse | None = None
    retry_after: float | None = None
    rejected_usage: bool = False
    output_tokens: int | None = None
    prompt_tokens: int | None = None
    estimated_output: int | None = None
    stalled: bool = False
    status: int | None = None
    error_excerpt: str | None = None

    @property
    def streamed(self) -> bool:
        return self.response is not None


def _excerpt(body: bytes, limit: int = LOG_ERROR_EXCERPT_BYTES) -> str:
    """An upstream error body as one bounded, single-line token.

    The bound is not about disk: it is that one record stays one line, and an upstream body is
    written by something this plan does not control.
    """
    if not body:
        return "<empty>"
    return _one_line(body[:limit].decode("utf-8", errors="replace"))


def _quota_headers(headers) -> dict[str, str]:
    """The endpoint's own retry and headroom headers, when it sent any.

    Their absence is evidence in its own right. A provider that documents a quota header on
    every response and did not send one here did not produce this answer, which is how a
    gateway rejection gets told apart from an intermediary's.
    """
    wanted = ("retry-after", "x-ratelimit-remaining", "x-ratelimit-reset")
    return {name: headers[name] for name in wanted if name in headers}


async def stream_attempt(
    request: web.Request,
    session: ClientSession,
    lane: LanePolicy,
    body: bytes,
    attempt: int,
    reservation: int,
) -> Attempt:
    """Make one attempt, holding every fair-use permit this lane is charged to."""
    url = f"{lane.base_url}/v1/chat/completions"
    async with admission(lane, enforcement, reservation):
        outbound_headers = filtered_request_headers(
            request, upstream_credential(lane.secret_name)
        )

        event(
            "upstream_dispatch",
            lane=lane.name,
            attempt=attempt,
            input_bytes=len(body),
            priced_reservation=reservation,
            lane_reservation=lane.reservation,
            runs_alone=enforcement.price_runs_alone(lane, reservation),
        )

        sent = time.monotonic()
        response = await session.post(
            url,
            data=body,
            headers=outbound_headers,
            allow_redirects=False,
        )
        header_ms = round((time.monotonic() - sent) * 1000)

        try:
            for _counter_id, ledger in enforcement.ledgers_for(lane):
                ledger.note_server(response.headers)

            if response.status in RETRYABLE or response.status == 400:
                error_body = await response.content.read(MAX_ERROR_BYTES + 1)

                if response.status in RETRYABLE:
                    response.close()
                    # The whole answer goes to the file and only its existence to the terminal:
                    # this is the line the operator already watches, and the reason behind it is
                    # precisely what a retry loop used to discard.
                    event(
                        "upstream_rejected",
                        lane=lane.name,
                        attempt=attempt,
                        status=response.status,
                        header_ms=header_ms,
                        input_bytes=len(body),
                        priced_reservation=reservation,
                        headers=_quota_headers(response.headers),
                        body=_excerpt(error_body),
                    )
                    return Attempt(
                        retry_after=retry_delay(response, attempt),
                        status=response.status,
                        error_excerpt=_excerpt(error_body),
                    )

                if body_rejects_stream_usage(response.status, error_body):
                    response.close()
                    event(
                        "usage_rejected",
                        lane=lane.name,
                        attempt=attempt,
                        status=response.status,
                        body=_excerpt(error_body),
                    )
                    return Attempt(
                        retry_after=0.0,
                        rejected_usage=True,
                        status=response.status,
                        error_excerpt=_excerpt(error_body),
                    )

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
            # A status the client will read as a refusal is copied into the file while it is
            # forwarded, because the forwarded copy is the only record of it that survives.
            peek = bytearray()

            try:
                async for chunk in response.content.iter_any():
                    output_bytes += len(chunk)
                    scanner.feed(chunk)

                    if response.status >= 400 and len(peek) < LOG_ERROR_EXCERPT_BYTES:
                        peek += chunk[: LOG_ERROR_EXCERPT_BYTES - len(peek)]

                    if output_bytes > MAX_RESPONSE_BYTES:
                        response.close()
                        scanner.finish()
                        event(
                            "response_limit",
                            ("lane", "input_bytes", "output_bytes"),
                            lane=lane.name,
                            input_bytes=len(body),
                            output_bytes=output_bytes,
                            status=response.status,
                            attempt=attempt,
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
                event(
                    "stream_stall",
                    ("lane", "attempt", "bytes"),
                    lane=lane.name,
                    attempt=attempt,
                    bytes=output_bytes,
                    status=response.status,
                    header_ms=header_ms,
                    elapsed_ms=round((time.monotonic() - sent) * 1000),
                )
                if request.transport is not None:
                    request.transport.close()
                return Attempt(
                    response=downstream,
                    output_tokens=scanner.completion_tokens,
                    prompt_tokens=scanner.prompt_tokens,
                    estimated_output=scanner.estimated_output(),
                    stalled=True,
                    status=response.status,
                )

            scanner.finish()
            await downstream.write_eof()

            if response.status >= 400:
                event(
                    "upstream_error",
                    lane=lane.name,
                    attempt=attempt,
                    status=response.status,
                    header_ms=header_ms,
                    input_bytes=len(body),
                    output_bytes=output_bytes,
                    headers=_quota_headers(response.headers),
                    body=_excerpt(bytes(peek)),
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
        status=downstream.status,
    )


def book_output(
    lane: LanePolicy, bookings: dict[str, Booking] | None, cost: Cost, outcome: Attempt
) -> None:
    """Settle this attempt's rate bookings against what it actually produced."""
    enforcement.settle(lane, bookings, cost, outcome)


#: Attempts after which the outbound body is archived, so a request the endpoint keeps
#: refusing can be replayed byte for byte rather than reconstructed from a transcript.
FAILURE_DUMP_ATTEMPT = 4


def prune_failure_dumps() -> int:
    """Drop whole-request dumps, oldest first, until they fit the budget.

    The dump just written is always newest, so it is the last one this evicts — a request
    large enough to exceed the budget on its own sheds everything older rather than itself.
    """
    try:
        paths = list(LOG_DIR.glob("failed-*.txt"))
    except OSError:
        return 0
    dumps: list[tuple[Path, int, int]] = []
    for path in paths:
        try:
            info = path.stat()
        except OSError:
            continue
        dumps.append((path, info.st_size, info.st_mtime_ns))
    dumps.sort(key=lambda item: item[2])
    total = sum(size for _path, size, _mtime in dumps)
    freed = 0
    for path, size, _mtime in dumps:
        if total <= LOG_DUMP_BUDGET_BYTES:
            break
        try:
            path.unlink()
        except OSError:
            continue
        total -= size
        freed += size
    return freed


def purge_failure_dumps() -> int:
    """Remove every whole-request dump. The event log outlives them.

    A dump is prompt text, and prompt text is what this stack promises not to keep past the
    launch that produced it. The events themselves are a different kind of record — numbers
    and short status lines, not conversation — and those are what survive for a post-mortem.
    """
    try:
        paths = list(LOG_DIR.glob("failed-*.txt"))
    except OSError:
        return 0
    removed = 0
    for path in paths:
        with contextlib.suppress(OSError):
            path.unlink()
            removed += 1
    return removed


async def dump_failure_body(
    request: web.Request, lane: LanePolicy, body: bytes, attempt: int
) -> None:
    """Archive the request a retry loop is stuck on, once per client request.

    The prompt archive keeps only requests that introduce a new user prompt, which is what
    makes it readable, and is exactly why it holds nothing for a turn that dies 143 steps in.
    Credentials go through the same redaction as every other dump, and the render runs off the
    loop for the same reason the prompt archive's does: a multimodal body is megabytes.
    """
    request_id = REQUEST_ID.get()
    if not request_id:
        return
    if len(body) > LOG_DUMP_MAX_BYTES:
        event(
            "failure_dump_skipped",
            lane=lane.name,
            attempt=attempt,
            input_bytes=len(body),
            limit=LOG_DUMP_MAX_BYTES,
        )
        return
    # Named by the request, not the attempt: every attempt of a stuck request carries the same
    # body, and a file each would turn one fault into a hundred megabytes.
    path = LOG_DIR / f"failed-{request_id}.txt"
    if path.exists():
        return
    try:
        await asyncio.to_thread(
            write_dump,
            request.method,
            f"{lane.base_url}/v1/chat/completions",
            {},
            body,
            path.name,
            LOG_DIR,
        )
    except (OSError, ValueError) as exc:
        event("failure_dump_failed", lane=lane.name, error=exc)
        return
    freed = prune_failure_dumps()
    event(
        "failure_dumped",
        file=path.name,
        host_file=f"{LOG_HOST_DIR}/{path.name}",
        attempt=attempt,
        bytes=path.stat().st_size if path.exists() else 0,
        budget_bytes=LOG_DUMP_BUDGET_BYTES,
        freed_bytes=freed,
    )


async def forward_chat(
    request: web.Request,
    lane: LanePolicy,
    body: bytes,
    started: float,
    inbound_body: bytes,
    cost: Cost,
    reservation: int | None = None,
) -> web.StreamResponse:
    """Retry an upstream request until it finishes or the client goes away.

    Every sleep happens with no fair-use permit held and no rate booking outstanding,
    so provider backpressure never occupies capacity that other requests could use.

    ``reservation`` is what the caller priced this request at, and every attempt holds exactly
    that. Omitting it charges the lane's worst case, which is the correct reading of a caller
    that has not measured anything.
    """
    global USAGE_SUPPORTED

    cost_of_permit = lane.reservation if reservation is None else reservation
    attempt_number = 0
    session: ClientSession = request.app["client"]

    while True:
        attempt_number += 1

        if request.transport is None or request.transport.is_closing():
            # Silent until now. This is the only record that a client gave up on a request the
            # proxy was still willing to retry, and without it an abandoned request and a
            # request the proxy never received look identical from the outside.
            event(
                "client_cancelled",
                lane=lane.name,
                attempts=attempt_number - 1,
                elapsed_seconds=round(time.monotonic() - started, 3),
            )
            raise asyncio.CancelledError

        elapsed = time.monotonic() - started
        if MAX_REQUEST_SECONDS and elapsed > MAX_REQUEST_SECONDS:
            stats.deadline_stops += 1
            event(
                "request_deadline",
                ("lane", "attempts", "elapsed_seconds"),
                lane=lane.name,
                attempts=attempt_number,
                elapsed_seconds=f"{elapsed:.1f}",
            )
            raise web.HTTPBadGateway(
                text="upstream did not complete within the policy request limit"
            )

        bookings = enforcement.book(lane, cost)
        if bookings is None:
            stats.rate_waits += 1
            delay = enforcement.wait_seconds(lane)
            event(
                "rate_wait",
                ("lane", "attempt", "delay"),
                lane=lane.name,
                attempt=attempt_number,
                delay=f"{delay:.1f}s",
            )
            await asyncio.sleep(delay)
            continue

        try:
            outcome = await stream_attempt(
                request, session, lane, body, attempt_number, cost_of_permit
            )
        except (TimeoutError, ClientConnectionError, ServerDisconnectedError) as exc:
            enforcement.settle(lane, bookings, cost, None)
            delay = retry_delay(_HeaderBag({}), attempt_number)
            event(
                "upstream_retry",
                ("lane", "attempt", "error", "delay"),
                lane=lane.name,
                attempt=attempt_number,
                error=type(exc).__name__,
                delay=f"{delay:.1f}s",
                elapsed_seconds=round(elapsed, 1),
                input_bytes=len(body),
                priced_reservation=cost_of_permit,
            )
        else:
            if outcome.streamed:
                book_output(lane, bookings, cost, outcome)
                # The priced permit beside the pre-request estimate, so the gap between what
                # a permit cost and what the request turned out to use is readable from the
                # log rather than something an operator has to trust the estimator about.
                event(
                    "request_complete",
                    (
                        "lane",
                        "input_bytes",
                        "attempts",
                        "prompt_tokens",
                        "output_tokens",
                        "estimated_output",
                        "priced_reservation",
                        "lane_reservation",
                        "estimated_input",
                        "stalled",
                        "elapsed_seconds",
                    ),
                    lane=lane.name,
                    input_bytes=len(body),
                    attempts=attempt_number,
                    status=outcome.status,
                    prompt_tokens=outcome.prompt_tokens,
                    output_tokens=outcome.output_tokens,
                    estimated_output=outcome.estimated_output,
                    priced_reservation=cost_of_permit,
                    lane_reservation=lane.reservation,
                    estimated_input=cost.input,
                    stalled=outcome.stalled,
                    elapsed_seconds=f"{time.monotonic() - started:.3f}",
                )
                return outcome.response

            if outcome.rejected_usage and USAGE_SUPPORTED:
                USAGE_SUPPORTED = False
                stats.usage_unsupported += 1
                body = rewrite_model(inbound_body, request, lane)
                event("usage_reporting_unsupported", note="retrying without stream_options")

            enforcement.settle(lane, bookings, cost, None)
            delay = outcome.retry_after if outcome.retry_after is not None else 1.0
            event(
                "upstream_retry",
                ("lane", "attempt", "delay"),
                lane=lane.name,
                attempt=attempt_number,
                delay=f"{delay:.1f}s",
                status=outcome.status,
                elapsed_seconds=round(elapsed, 1),
                input_bytes=len(body),
                priced_reservation=cost_of_permit,
                body=outcome.error_excerpt,
            )
            if attempt_number >= FAILURE_DUMP_ATTEMPT:
                await dump_failure_body(request, lane, body, attempt_number)

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


async def open_prompt_archive(app: web.Application) -> None:
    """Start from an empty archive: a container that was killed never got the chance.

    Whole-request dumps are conversation text too, so they are cleared on the same terms.
    """
    stale = purge_prompt_log()
    event(
        "prompt_log",
        ("directory", "summary_chars", "stale_entries"),
        directory=PROMPT_LOG_HOST_DIR,
        summary_chars=PROMPT_SUMMARY_CHARS,
        stale_entries=stale,
        stale_dumps=purge_failure_dumps(),
    )


async def close_prompt_archive(app: web.Application) -> None:
    """Prompt text does not outlive the stack that produced it.

    That covers both kinds of prompt text: the archive, and the whole-request dumps a stalled
    retry loop writes beside the log. The event log itself deliberately survives, because
    numbers and short status lines are not conversation, and a fault that only surfaces after
    the stack is down has nowhere else to be read.
    """
    event(
        "prompt_log_purged",
        ("entries",),
        entries=purge_prompt_log(),
        dumps=purge_failure_dumps(),
    )


def create_app() -> web.Application:
    app = web.Application(client_max_size=MAX_REQUEST_BYTES)
    app.on_startup.append(create_client)
    app.on_startup.append(open_prompt_archive)
    app.on_cleanup.append(close_client)
    app.on_cleanup.append(close_prompt_archive)

    # Only lanes the operator's definitions produced are routable; a lane the plan does
    # not publish answers 404 rather than being admitted and failing policy validation.
    lanes = "{lane:" + "|".join(sorted(BASELINE_POLICY.lanes)) + "}"
    app.router.add_get("/healthz", health)
    app.router.add_post(f"/{lanes}/v1/chat/completions", chat)
    app.router.add_get(f"/{lanes}/v1/models", models)
    return app


def main() -> None:
    unavailable = setup_logging()
    validate_policy()
    try:
        verify_credentials(BASELINE_POLICY)
    except PolicyDrift as exc:
        # One clean line rather than a traceback: start.sh surfaces the container's stderr,
        # and the fact names something the operator can mount, not a crash to debug.
        event("startup_refused", error=exc)
        raise SystemExit(f"model-proxy will not start: {exc}") from None
    for name, lane in sorted(BASELINE_POLICY.lanes.items()):
        event(
            "lane_config",
            lane=name,
            alias=lane.alias,
            provider=lane.provider_name,
            context=lane.context,
            input=lane.max_input,
            output=lane.output_clamp,
            reservation=lane.reservation,
            counters=",".join(lane.counters),
        )
    # Where to read the whole record, named once on the terminal so the filtered live view is
    # never a dead end. A bind the operator has not mounted says so here rather than silently
    # logging to a path that does not exist.
    if unavailable:
        event("log_unavailable", directory=LOG_DIR, error=unavailable)
    else:
        event(
            "log_file",
            file=f"{LOG_HOST_DIR}/{LOG_FILE_NAME}",
            live="all" if LIVE_ALL else "default",
        )
    web.run_app(create_app(), host="0.0.0.0", port=8080, access_log=None)  # noqa: S104


if __name__ == "__main__":
    main()
