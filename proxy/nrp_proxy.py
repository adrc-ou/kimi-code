#!/usr/bin/env python3
"""Strict OpenAI-compatible NRP policy proxy."""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import os
import secrets
import time
import tomllib
from collections.abc import AsyncIterator
from email.utils import parsedate_to_datetime
from pathlib import Path

from aiohttp import (
    ClientConnectionError,
    ClientSession,
    ClientTimeout,
    ServerDisconnectedError,
    web,
)


def secret(file_variable: str, value_variable: str) -> str:
    path = os.environ.get(file_variable, "")
    value = Path(path).read_text().strip() if path else os.environ.get(value_variable, "")
    if len(value) < 32:
        raise RuntimeError(f"{file_variable} must reference a secret of at least 32 characters")
    return value


UPSTREAM = os.environ["NRP_UPSTREAM_ORIGIN"].rstrip("/")
UPSTREAM_MODEL = os.environ["NRP_UPSTREAM_MODEL"]
API_KEY = secret("NRP_API_KEY_FILE", "NRP_API_KEY")
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
RETRYABLE = {429, 500, 502, 503, 504}


def load_kimi_policy() -> int:
    with open(KIMI_CONFIG_PATH, "rb") as source:
        config = tomllib.load(source)
    secondary = config["secondary_model"]
    if secondary.get("force") is not True:
        raise RuntimeError("Kimi secondary_model.force must be true")
    alias = secondary["default_model"]
    model = config["models"][alias]
    if model["provider"] != "nrp-subagent":
        raise RuntimeError(f"Forced secondary model {alias!r} does not use nrp-subagent")
    context = int(model["max_context_size"])
    reserve = int(config["loop_control"]["reserved_context_size"])
    if int(model["max_input_size"]) + reserve > context:
        raise RuntimeError("Subagent max_input_size + reserve exceeds max_context_size")
    return context


SUBAGENT_CONTEXT = load_kimi_policy()


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
    payload["model"] = UPSTREAM_MODEL
    payload["cache_salt"] = CACHE_SALT
    return json.dumps(payload, separators=(",", ":"), allow_nan=False).encode()


def validate_policy() -> None:
    numeric_values = {
        "NRP_MODEL_CONTEXT": MODEL_CONTEXT,
        "NRP_FAIR_USE_PERCENT": FAIR_USE_PERCENT,
        "NRP_PARALLEL_CONTEXT_BUDGET": PARALLEL_CONTEXT_BUDGET,
        "NRP_MODEL_MAX_CONCURRENCY": MODEL_MAX_CONCURRENCY,
        "NRP_SUBAGENT_MAX_CONCURRENCY": SUBAGENT_LIMIT,
        "subagent max_context_size": SUBAGENT_CONTEXT,
        "NRP_MAX_REQUEST_BYTES": MAX_REQUEST_BYTES,
        "NRP_MAX_RESPONSE_BYTES": MAX_RESPONSE_BYTES,
        "NRP_MAX_QUEUED": MAX_QUEUED,
    }
    for name, value in numeric_values.items():
        if value <= 0:
            raise RuntimeError(f"{name} must be positive; got {value}")
    if FAIR_USE_PERCENT > 100:
        raise RuntimeError(f"NRP_FAIR_USE_PERCENT must not exceed 100; got {FAIR_USE_PERCENT}")
    hard_budget = MODEL_CONTEXT * FAIR_USE_PERCENT // 100
    if PARALLEL_CONTEXT_BUDGET > hard_budget:
        raise RuntimeError("NRP_PARALLEL_CONTEXT_BUDGET exceeds the configured fair-use limit")
    if SUBAGENT_LIMIT > MODEL_MAX_CONCURRENCY:
        raise RuntimeError("Configured subagent concurrency exceeds NRP model concurrency")
    if SUBAGENT_LIMIT * SUBAGENT_CONTEXT > PARALLEL_CONTEXT_BUDGET:
        raise RuntimeError("Subagent configuration exceeds parallel context budget")


class FairUseGate:
    def __init__(self, subagent_limit: int):
        self.condition = asyncio.Condition()
        self.subagent_limit = subagent_limit
        self.active_primary = 0
        self.active_subagents = 0
        self.waiting_primary = 0

    @contextlib.asynccontextmanager
    async def slot(self, lane: str) -> AsyncIterator[None]:
        if lane == "long":
            lane = "primary"
        if lane == "primary":
            async with self.condition:
                self.waiting_primary += 1
                try:
                    await self.condition.wait_for(
                        lambda: not self.active_primary and not self.active_subagents
                    )
                    self.active_primary = 1
                finally:
                    self.waiting_primary -= 1
            try:
                yield
            finally:
                async with self.condition:
                    self.active_primary = 0
                    self.condition.notify_all()
            return
        if lane == "subagent":
            async with self.condition:
                await self.condition.wait_for(
                    lambda: not self.active_primary
                    and not self.waiting_primary
                    and self.active_subagents < self.subagent_limit
                )
                self.active_subagents += 1
            try:
                yield
            finally:
                async with self.condition:
                    self.active_subagents -= 1
                    self.condition.notify_all()
            return
        raise ValueError(f"Unknown lane: {lane}")


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


gate = FairUseGate(SUBAGENT_LIMIT)
ingress = IngressGate(1 + SUBAGENT_LIMIT, MAX_QUEUED)
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
        | {"host", "authorization", "content-length", "content-encoding"}
    )
    result = {name: value for name, value in request.headers.items() if name.lower() not in blocked}
    result["Authorization"] = f"Bearer {API_KEY}"
    result["Content-Type"] = "application/json"
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


async def health(_request: web.Request) -> web.Response:
    hard_budget = MODEL_CONTEXT * FAIR_USE_PERCENT // 100
    return web.json_response(
        {
            "status": "ok",
            "active_primary": gate.active_primary,
            "active_subagents": gate.active_subagents,
            "waiting_primary": gate.waiting_primary,
            "subagent_limit": gate.subagent_limit,
            "parallel_context_budget": PARALLEL_CONTEXT_BUDGET,
            "fair_use_context_budget": hard_budget,
            "hard_35_percent_budget": hard_budget,
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
        raise web.HTTPUnsupportedMediaType(text="compressed request bodies are not supported")
    lane = request.match_info["lane"]
    async with ingress.slot():
        body = rewrite_model(await request.read(), request, lane)
        return await forward_chat(request, lane, body, started)


async def forward_chat(
    request: web.Request, lane: str, body: bytes, started: float
) -> web.StreamResponse:
    attempt = 0
    while True:
        attempt += 1
        if request.transport is None or request.transport.is_closing():
            raise asyncio.CancelledError
        try:
            async with gate.slot(lane):
                session: ClientSession = request.app["client"]
                response = await session.post(
                    f"{UPSTREAM}/v1/chat/completions",
                    data=body,
                    headers=filtered_request_headers(request),
                    allow_redirects=False,
                )
                if response.status in RETRYABLE:
                    delay = retry_delay(response, attempt)
                    await response.content.read(MAX_ERROR_BYTES + 1)
                    response.close()
                else:
                    if (
                        response.content_length is not None
                        and response.content_length > MAX_RESPONSE_BYTES
                    ):
                        response.close()
                        raise web.HTTPBadGateway(text="upstream response exceeds byte limit")
                    downstream = web.StreamResponse(
                        status=response.status, headers=filtered_response_headers(response.headers)
                    )
                    await downstream.prepare(request)
                    output_bytes = 0
                    try:
                        async for chunk in response.content.iter_any():
                            output_bytes += len(chunk)
                            if output_bytes > MAX_RESPONSE_BYTES:
                                response.close()
                                if request.transport is not None:
                                    request.transport.close()
                                print(
                                    f"response_limit lane={lane} input_bytes={len(body)} "
                                    f"output_bytes={output_bytes}",
                                    flush=True,
                                )
                                return downstream
                            await downstream.write(chunk)
                    finally:
                        response.release()
                    await downstream.write_eof()
                    print(
                        f"request_complete lane={lane} input_bytes={len(body)} attempts={attempt} "
                        f"elapsed_seconds={time.monotonic() - started:.3f}",
                        flush=True,
                    )
                    return downstream
        except (TimeoutError, ClientConnectionError, ServerDisconnectedError):
            delay = retry_delay(type("Retry", (), {"headers": {}})(), attempt)
        print(f"upstream_retry lane={lane} attempt={attempt} delay={delay:.1f}s", flush=True)
        await asyncio.sleep(delay)


async def create_client(app: web.Application) -> None:
    app["client"] = ClientSession(
        timeout=ClientTimeout(total=None, connect=60, sock_connect=60, sock_read=None),
        auto_decompress=False,
    )


async def close_client(app: web.Application) -> None:
    await app["client"].close()


def create_app() -> web.Application:
    app = web.Application(client_max_size=MAX_REQUEST_BYTES)
    app.on_startup.append(create_client)
    app.on_cleanup.append(close_client)
    app.router.add_get("/healthz", health)
    for lane in ("primary", "long", "subagent"):
        app.router.add_post(f"/{lane}/v1/chat/completions", chat)
        app.router.add_get(f"/{lane}/v1/models", models)
    return app


def main() -> None:
    validate_policy()
    web.run_app(create_app(), host="0.0.0.0", port=8080, access_log=None)  # noqa: S104


if __name__ == "__main__":
    main()
