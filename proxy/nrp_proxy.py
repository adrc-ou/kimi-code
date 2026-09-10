#!/usr/bin/env python3

import asyncio
import contextlib
import json
import os
import random
import tomllib
from typing import AsyncIterator

from aiohttp import ClientSession, ClientTimeout, web


UPSTREAM = os.environ["NRP_UPSTREAM_ORIGIN"].rstrip("/")
UPSTREAM_MODEL = os.environ["NRP_UPSTREAM_MODEL"]
API_KEY = os.environ["NRP_API_KEY"]

KIMI_CONFIG_PATH = os.environ.get(
    "KIMI_CONFIG_PATH",
    "/policy/kimi-config.toml",
)

SUBAGENT_LIMIT = int(
    os.environ.get("NRP_SUBAGENT_MAX_CONCURRENCY", "5")
)

MODEL_CONTEXT = int(
    os.environ.get("NRP_MODEL_CONTEXT", "1000000")
)

FAIR_USE_PERCENT = int(
    os.environ.get("NRP_FAIR_USE_PERCENT", "35")
)

PARALLEL_CONTEXT_BUDGET = int(
    os.environ.get("NRP_PARALLEL_CONTEXT_BUDGET", "320000")
)

MODEL_MAX_CONCURRENCY = int(
    os.environ.get("NRP_MODEL_MAX_CONCURRENCY", "16")
)


def load_kimi_policy() -> tuple[int, int]:
    with open(KIMI_CONFIG_PATH, "rb") as f:
        config = tomllib.load(f)

    secondary = config["secondary_model"]

    if secondary.get("force") is not True:
        raise RuntimeError(
            "Kimi secondary_model.force must be true"
        )

    alias = secondary["default_model"]
    model = config["models"][alias]

    if model["provider"] != "nrp-subagent":
        raise RuntimeError(
            f"Forced secondary model {alias!r} does not use nrp-subagent"
        )

    context = int(model["max_context_size"])
    max_input = int(model["max_input_size"])
    reserve = int(
        config["loop_control"]["reserved_context_size"]
    )

    if max_input + reserve > context:
        raise RuntimeError(
            "Subagent max_input_size + reserve exceeds "
            "max_context_size"
        )

    return context

SUBAGENT_CONTEXT = load_kimi_policy()

INTERNAL_BEARER_TOKEN = "proxy-only"

def authorize_client(request: web.Request) -> None:
    expected = f"Bearer {INTERNAL_BEARER_TOKEN}"

    if request.headers.get("Authorization") != expected:
        raise web.HTTPUnauthorized()

def rewrite_model(body: bytes, request: web.Request) -> bytes:
    if request.method not in {"POST", "PUT", "PATCH"}:
        return body

    if request.content_type != "application/json":
        return body

    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return body

    if not isinstance(payload, dict) or "model" not in payload:
        return body

    payload["model"] = UPSTREAM_MODEL

    return json.dumps(
        payload,
        separators=(",", ":"),
    ).encode("utf-8")

def validate_policy() -> None:
    hard_budget = MODEL_CONTEXT * FAIR_USE_PERCENT // 100

    if PARALLEL_CONTEXT_BUDGET > hard_budget:
        raise RuntimeError(
            "NRP_PARALLEL_CONTEXT_BUDGET exceeds the NRP 35% limit: "
            f"{PARALLEL_CONTEXT_BUDGET} > {hard_budget}"
        )

    if SUBAGENT_LIMIT > MODEL_MAX_CONCURRENCY:
        raise RuntimeError(
            "Configured subagent concurrency exceeds NRP model concurrency: "
            f"{SUBAGENT_LIMIT} > {MODEL_MAX_CONCURRENCY}"
        )

    combined = SUBAGENT_LIMIT * SUBAGENT_CONTEXT

    if combined > PARALLEL_CONTEXT_BUDGET:
        raise RuntimeError(
            "Subagent configuration exceeds parallel context budget: "
            f"{SUBAGENT_LIMIT} * {SUBAGENT_CONTEXT} "
            f"= {combined} > {PARALLEL_CONTEXT_BUDGET}"
        )

    print(
        "NRP policy validated: "
        f"hard_budget={hard_budget}, "
        f"parallel_budget={PARALLEL_CONTEXT_BUDGET}, "
        f"subagents={SUBAGENT_LIMIT}, "
        f"context_each={SUBAGENT_CONTEXT}, "
        f"combined={combined}",
        flush=True,
    )


class FairUseGate:
    """
    Two mutually exclusive lanes:

    primary:
        exactly one request;
        cannot overlap any subagent request.

    subagent:
        up to SUBAGENT_LIMIT simultaneous requests;
        cannot overlap a primary request.

    Waiting primary requests get priority once active subagents drain.
    """

    def __init__(self, subagent_limit: int):
        self.condition = asyncio.Condition()

        self.subagent_limit = subagent_limit

        self.active_primary = 0
        self.active_subagents = 0
        self.waiting_primary = 0

    @contextlib.asynccontextmanager
    async def slot(self, lane: str) -> AsyncIterator[None]:
        if lane == "primary":
            async with self.condition:
                self.waiting_primary += 1

                try:
                    await self.condition.wait_for(
                        lambda:
                            self.active_primary == 0
                            and self.active_subagents == 0
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
                    lambda:
                        self.active_primary == 0
                        and self.waiting_primary == 0
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


gate = FairUseGate(SUBAGENT_LIMIT)


HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
}


def filtered_request_headers(request: web.Request) -> dict[str, str]:
    headers = {}

    for name, value in request.headers.items():
        lower = name.lower()

        if lower in HOP_BY_HOP_HEADERS:
            continue

        if lower in {
            "host",
            "authorization",
            "content-length",
        }:
            continue

        headers[name] = value

    headers["Authorization"] = f"Bearer {API_KEY}"

    return headers


def filtered_response_headers(headers) -> dict[str, str]:
    result = {}

    for name, value in headers.items():
        if name.lower() in HOP_BY_HOP_HEADERS:
            continue

        result[name] = value

    return result


def retry_delay(response, attempt: int) -> float:
    retry_after = response.headers.get("Retry-After")

    if retry_after:
        try:
            return max(1.0, float(retry_after))
        except ValueError:
            pass

    reset = response.headers.get("x-ratelimit-reset")

    if reset:
        try:
            return max(1.0, float(reset))
        except ValueError:
            pass

    base = min(60.0, 2 ** min(attempt, 6))

    return base + random.uniform(0.0, 1.0)


async def health(request: web.Request) -> web.Response:
    return web.json_response(
        {
            "status": "ok",
            "active_primary": gate.active_primary,
            "active_subagents": gate.active_subagents,
            "waiting_primary": gate.waiting_primary,
            "subagent_limit": gate.subagent_limit,
            "subagent_context": SUBAGENT_CONTEXT,
            "parallel_context_budget": PARALLEL_CONTEXT_BUDGET,
            "hard_35_percent_budget":
                MODEL_CONTEXT * FAIR_USE_PERCENT // 100,
        }
    )


async def proxy(request: web.Request) -> web.StreamResponse:
    authorize_client(request)
    lane = request.match_info["lane"]

    if lane not in {"primary", "subagent"}:
        raise web.HTTPNotFound()

    tail = request.match_info.get("tail", "")

    upstream_url = f"{UPSTREAM}/{tail}"

    if request.query_string:
        upstream_url += f"?{request.query_string}"

    body = rewrite_model(
        await request.read(),
        request,
    )

    async with gate.slot(lane):
        attempt = 0

        while True:
            attempt += 1

            session: ClientSession = request.app["client"]

            response = await session.request(
                request.method,
                upstream_url,
                data=body,
                headers=filtered_request_headers(request),
                allow_redirects=False,
            )

            # NRP explicitly recommends persistent retry/backoff for
            # automated scripts when models are temporarily unavailable.
            if response.status in {
                429,
                500,
                502,
                503,
                504,
            }:
                delay = retry_delay(response, attempt)

                error_body = await response.read()
                response.release()

                print(
                    f"upstream_retry "
                    f"lane={lane} "
                    f"status={response.status} "
                    f"attempt={attempt} "
                    f"delay={delay:.1f}s "
                    f"body_bytes={len(error_body)}",
                    flush=True,
                )

                await asyncio.sleep(delay)
                continue

            downstream = web.StreamResponse(
                status=response.status,
                headers=filtered_response_headers(response.headers),
            )

            await downstream.prepare(request)

            try:
                async for chunk in response.content.iter_any():
                    await downstream.write(chunk)
            finally:
                response.release()

            await downstream.write_eof()

            return downstream


async def create_client(app: web.Application) -> None:
    app["client"] = ClientSession(
        timeout=ClientTimeout(
            total=None,
            sock_connect=60,
            sock_read=None,
        ),
        auto_decompress=False
    )


async def close_client(app: web.Application) -> None:
    await app["client"].close()


def main() -> None:
    validate_policy()

    app = web.Application(
        # Accommodate multimodal requests.
        client_max_size=256 * 1024 ** 2,
    )

    app.on_startup.append(create_client)
    app.on_cleanup.append(close_client)

    app.router.add_get("/healthz", health)

    app.router.add_route(
        "*",
        "/{lane}/{tail:.*}",
        proxy,
    )

    web.run_app(
        app,
        host="0.0.0.0",
        port=8080,
        access_log=None,
    )


if __name__ == "__main__":
    main()
