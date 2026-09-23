#!/usr/bin/env python3

"""Authenticated HTTP/WebSocket bridge from Docker Desktop to macOS ComfyUI."""

from __future__ import annotations

import argparse
import asyncio
import hmac
import json
import os
import secrets
import ssl
import time

from aiohttp import ClientSession, ClientTimeout, WSMsgType, web

HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}

#: Everything the bridge serves itself rather than proxying. The catch-all route refuses these,
#: so a reserved name can never reach ComfyUI and ComfyUI can never gain one by accident.
BRIDGE_PREFIX = "/__bridge"
OPEN_PATH = f"{BRIDGE_PREFIX}/open"
GRANT_PATH = f"{BRIDGE_PREFIX}/grant"
SESSION_PATH = f"{BRIDGE_PREFIX}/session"
SESSION_COOKIE = "comfyui_bridge_session"
#: 32 bytes of entropy, so neither a grant nor a session cookie is guessable, replayable from a
#: nearby observer, or short enough to brute force inside its lifetime.
CREDENTIAL_BYTES = 32
#: The session endpoint takes one small JSON object from this bridge's own page. Refusing anything
#: else keeps a request that holds no credential from making the bridge buffer a body.
SESSION_BODY_LIMIT = 4096

BOOTSTRAP_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="referrer" content="no-referrer">
<title>ComfyUI bridge</title>
<style>
body { font: 16px/1.5 system-ui, sans-serif; margin: 4rem auto; max-width: 34rem; padding: 0 1rem; }
</style>
</head>
<body>
<p id="status">Opening ComfyUI&hellip;</p>
<script>
async function enter() {
  const status = document.getElementById("status");
  const grant = decodeURIComponent(location.hash.replace(/^#/, ""));
  // Take the credential out of this tab's history before anything can inherit the entry. The
  // fragment was never transmitted to the bridge, and it does not stay in the address bar either.
  history.replaceState(null, "", location.pathname);
  if (!grant) {
    status.textContent =
      "This page is not a frontend login. Open a freshly requested ComfyUI link.";
    return;
  }
  try {
    const response = await fetch("@SESSION_PATH@", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ grant }),
      cache: "no-store",
    });
    if (!response.ok) {
      // A grant is spent by its first redemption, so this is the same link arriving a second time
      // from wherever it was kept: another tab, or an older answer that printed it.
      status.textContent = "That link has been used or has expired. Open a freshly requested one.";
      return;
    }
    location.replace("/");
  } catch {
    status.textContent = "The ComfyUI bridge is not reachable.";
  }
}
enter();
</script>
</body>
</html>
"""


def bootstrap_page() -> str:
    """The login page, with its one route named in exactly one place: the registrar's."""
    return BOOTSTRAP_TEMPLATE.replace("@SESSION_PATH@", SESSION_PATH)


def bearer_authorized(request: web.Request) -> bool:
    expected = f"Bearer {request.app['token']}"
    supplied = request.headers.get("Authorization", "")
    return hmac.compare_digest(supplied, expected)


def authorized(request: web.Request) -> bool:
    """Whether this request carries a credential the bridge accepts on a proxied path.

    A browser cannot attach an ``Authorization`` header to a navigation or to the dozens of
    subresources the ComfyUI frontend asks for, so a browser arrives holding a session cookie
    instead - see :class:`FrontendPasses` for how it obtains one. Every path still requires a
    credential; only the shape of it differs, and the long-lived bearer token never leaves a
    client that can set a header.
    """
    if bearer_authorized(request):
        return True
    passes: FrontendPasses = request.app["passes"]
    return passes.session_active(
        request.cookies.get(SESSION_COOKIE, ""), request.headers.get("Host", "")
    )


class FrontendPasses:
    """The bridge's browser credentials: single-use grants, then origin-bound sessions.

    The launcher or :mod:`comfyctl` asks for a grant while holding the bearer token, and hands a
    browser a link whose *fragment* carries it. A fragment is never transmitted, so the grant
    reaches the bootstrap page without ever appearing in a request line, a log, or a Referer, and
    the page erases it from the session history before it uses it. Redeeming the grant yields an
    HttpOnly session cookie, which the browser then sends on every frontend request.

    A grant is worth one navigation for a few seconds; a session is worth one origin for its
    lifetime. Neither is the bearer token, so neither outlives this process in a link, a bookmark
    or a browser profile.
    """

    def __init__(self, *, grant_ttl: int, session_ttl: int, max_grants: int, max_sessions: int):
        self.grant_ttl = grant_ttl
        self.session_ttl = session_ttl
        self.max_grants = max_grants
        self.max_sessions = max_sessions
        self._grants: dict[str, float] = {}
        # A session belongs to the Host that redeemed its grant, so a cookie captured on one
        # plane cannot be replayed against another, and cannot mint further sessions.
        self._sessions: dict[str, tuple[str, float]] = {}

    def _prune_grants(self, now: float) -> None:
        for grant in [grant for grant, expiry in self._grants.items() if expiry <= now]:
            del self._grants[grant]

    def _prune_sessions(self, now: float) -> None:
        for session in [
            session for session, (_, expiry) in self._sessions.items() if expiry <= now
        ]:
            del self._sessions[session]

    def mint_grant(self) -> str | None:
        now = time.monotonic()
        self._prune_grants(now)
        if len(self._grants) >= self.max_grants:
            return None
        grant = secrets.token_urlsafe(CREDENTIAL_BYTES)
        self._grants[grant] = now + self.grant_ttl
        return grant

    def redeem_grant(self, value: str) -> bool:
        """Consume one outstanding grant. Grants are never host-bound: the API plane that mints
        one and the frontend plane that redeems it are different origins by design."""
        if not value:
            return False
        now = time.monotonic()
        self._prune_grants(now)
        for grant in list(self._grants):
            if hmac.compare_digest(grant, value):
                del self._grants[grant]
                return True
        return False

    def session_active(self, value: str, host: str) -> bool:
        if not value:
            return False
        session = self._sessions.get(value)
        if session is None:
            return False
        minted_for, expiry = session
        if expiry <= time.monotonic():
            del self._sessions[value]
            return False
        return bool(host) and hmac.compare_digest(minted_for, host)

    def mint_session(self, host: str) -> str | None:
        now = time.monotonic()
        self._prune_sessions(now)
        if len(self._sessions) >= self.max_sessions:
            return None
        session = secrets.token_urlsafe(CREDENTIAL_BYTES)
        self._sessions[session] = (host, now + self.session_ttl)
        return session

    def session_cookie(self, value: str) -> str:
        """The Set-Cookie value for a fresh session.

        Deliberately without ``Secure``: both frontend planes are plaintext by necessity - host
        loopback for the operator's browser and a private Docker network for the agent's - and an
        attribute claiming otherwise would only make the cookie unserviceable. The bridge's own
        listener is TLS on every path, so the credential never crosses a network the operator does
        not own, and ``SameSite=Strict`` keeps it out of any other site's requests.
        """
        return (
            f"{SESSION_COOKIE}={value}; HttpOnly; SameSite=Strict; Path=/; "
            f"Max-Age={self.session_ttl}"
        )


def request_headers(request: web.Request) -> dict[str, str]:
    nominated = {
        value.strip().lower()
        for value in request.headers.get("Connection", "").split(",")
        if value.strip()
    }
    return {
        name: value
        for name, value in request.headers.items()
        if name.lower() not in HOP_BY_HOP | nominated | {"authorization", "content-length", "host"}
    }


def response_headers(headers) -> dict[str, str]:
    nominated = {
        value.strip().lower() for value in headers.get("Connection", "").split(",") if value.strip()
    }
    return {
        name: value
        for name, value in headers.items()
        if name.lower() not in HOP_BY_HOP | nominated | {"content-length"}
    }


async def websocket_proxy(request: web.Request, upstream_url: str) -> web.StreamResponse:
    maximum = request.app["max_ws_message"]
    downstream = web.WebSocketResponse(heartbeat=30, max_msg_size=maximum)
    await downstream.prepare(request)
    session: ClientSession = request.app["client"]

    try:
        upstream = await session.ws_connect(
            upstream_url,
            headers=request_headers(request),
            heartbeat=30,
            max_msg_size=maximum,
        )
    except Exception:
        await downstream.close(code=1011, message=b"upstream unavailable")
        raise

    async def client_to_upstream() -> None:
        async for message in downstream:
            if message.type == WSMsgType.TEXT:
                await upstream.send_str(message.data)
            elif message.type == WSMsgType.BINARY:
                await upstream.send_bytes(message.data)
            elif message.type in {WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.ERROR}:
                break

    async def upstream_to_client() -> None:
        async for message in upstream:
            if message.type == WSMsgType.TEXT:
                await downstream.send_str(message.data)
            elif message.type == WSMsgType.BINARY:
                await downstream.send_bytes(message.data)
            elif message.type in {WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.ERROR}:
                break

    tasks = {
        asyncio.create_task(client_to_upstream()),
        asyncio.create_task(upstream_to_client()),
    }
    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)
    for task in done:
        task.result()
    await upstream.close()
    await downstream.close()
    return downstream


async def frontend_open(request: web.Request) -> web.Response:
    """Serve the login page.

    Public, because a browser has to reach something before it holds a credential, and inert
    without one: the document is this constant, it touches no upstream path, and the grant it
    expects arrives in a fragment the browser never transmits.
    """
    return web.Response(
        text=bootstrap_page(),
        content_type="text/html",
        headers={
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
            "Content-Security-Policy": (
                "default-src 'none'; script-src 'unsafe-inline'; connect-src 'self'; "
                "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
            ),
        },
    )


async def frontend_grant(request: web.Request) -> web.Response:
    """Trade the bearer token for one frontend grant.

    This is the only place a browser credential originates, and it stays reachable only to a
    client that can set an ``Authorization`` header, so the token itself never travels in a URL.
    """
    if not bearer_authorized(request):
        raise web.HTTPUnauthorized()
    passes: FrontendPasses = request.app["passes"]
    grant = passes.mint_grant()
    if grant is None:
        raise web.HTTPServiceUnavailable(text="no frontend grant slots are free")
    return web.json_response(
        {"path": f"{OPEN_PATH}#{grant}", "expires_in": passes.grant_ttl},
        headers={"Cache-Control": "no-store"},
    )


async def frontend_session(request: web.Request) -> web.Response:
    """Redeem a grant for an HttpOnly session cookie scoped to this frontend origin."""
    passes: FrontendPasses = request.app["passes"]
    declared = request.content_length
    if not declared or declared > SESSION_BODY_LIMIT:
        raise web.HTTPBadRequest(text="a short JSON body with a length is required")
    try:
        payload = json.loads(await request.read())
    except ValueError:
        raise web.HTTPBadRequest(text="invalid JSON body") from None
    if not isinstance(payload, dict) or not isinstance(payload.get("grant"), str):
        raise web.HTTPBadRequest(text="a frontend grant is required")
    if not passes.redeem_grant(payload["grant"]):
        raise web.HTTPUnauthorized(text="unknown, used or expired frontend grant")
    session = passes.mint_session(request.headers.get("Host", ""))
    if session is None:
        raise web.HTTPServiceUnavailable(text="no frontend session slots are free")
    return web.json_response(
        {"status": "session", "expires_in": passes.session_ttl},
        headers={"Cache-Control": "no-store", "Set-Cookie": passes.session_cookie(session)},
    )


async def proxy(request: web.Request) -> web.StreamResponse:
    if request.rel_url.path == BRIDGE_PREFIX or request.rel_url.path.startswith(
        f"{BRIDGE_PREFIX}/"
    ):
        # Reserved for the handlers above. Nothing of ComfyUI's lives here, so an unregistered
        # name under it is a mistake rather than a route, and must not reach the upstream.
        raise web.HTTPNotFound()
    if not authorized(request):
        raise web.HTTPUnauthorized()

    upstream_url = f"{request.app['upstream']}{request.rel_url}"
    async with request.app["connections"]:
        websocket = web.WebSocketResponse().can_prepare(request)
        if websocket.ok:
            return await websocket_proxy(request, upstream_url)
        session: ClientSession = request.app["client"]
        async with session.request(
            request.method,
            upstream_url,
            data=request.content.iter_chunked(1024 * 1024),
            headers=request_headers(request),
            allow_redirects=False,
        ) as upstream:
            if (
                upstream.content_length is not None
                and upstream.content_length > request.app["max_response"]
            ):
                raise web.HTTPBadGateway(text="upstream response exceeds byte limit")
            downstream = web.StreamResponse(
                status=upstream.status,
                reason=upstream.reason,
                headers=response_headers(upstream.headers),
            )
            await downstream.prepare(request)
            output_bytes = 0
            async for chunk in upstream.content.iter_any():
                output_bytes += len(chunk)
                if output_bytes > request.app["max_response"]:
                    upstream.close()
                    if request.transport is not None:
                        request.transport.close()
                    return downstream
                await downstream.write(chunk)
            await downstream.write_eof()
            return downstream


async def create_client(app: web.Application) -> None:
    app["client"] = ClientSession(
        timeout=ClientTimeout(total=None, sock_connect=30, sock_read=None),
        auto_decompress=False,
    )


async def close_client(app: web.Application) -> None:
    await app["client"].close()


def build_app(options: argparse.Namespace, token: str) -> web.Application:
    """The bridge's application: its limits, its credentials, and the order of its routes.

    The frontend handlers are registered ahead of the catch-all, which claims everything else, so
    a route added after it would never be reached.
    """
    app = web.Application(client_max_size=options.max_body)
    app["token"] = token
    app["upstream"] = options.upstream.rstrip("/")
    app["max_ws_message"] = options.max_ws_message
    app["max_response"] = options.max_body
    app["connections"] = asyncio.Semaphore(options.max_connections)
    app["passes"] = FrontendPasses(
        grant_ttl=options.grant_ttl,
        session_ttl=options.session_ttl,
        max_grants=options.max_grants,
        max_sessions=options.max_sessions,
    )
    app.on_startup.append(create_client)
    app.on_cleanup.append(close_client)
    app.router.add_get(OPEN_PATH, frontend_open)
    app.router.add_post(GRANT_PATH, frontend_grant)
    app.router.add_post(SESSION_PATH, frontend_session)
    app.router.add_route("*", "/{tail:.*}", proxy)
    return app


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")  # noqa: S104
    parser.add_argument("--port", type=int, default=8190)
    parser.add_argument("--upstream", default="http://127.0.0.1:8188")
    parser.add_argument("--tls-cert", required=True)
    parser.add_argument("--tls-key", required=True)
    parser.add_argument("--max-body", type=int, default=512 * 1024**2)
    parser.add_argument("--max-ws-message", type=int, default=64 * 1024**2)
    parser.add_argument("--max-connections", type=int, default=16)
    parser.add_argument("--grant-ttl", type=int, default=60)
    parser.add_argument("--session-ttl", type=int, default=12 * 3600)
    parser.add_argument("--max-grants", type=int, default=16)
    parser.add_argument("--max-sessions", type=int, default=8)
    args = parser.parse_args()

    token = os.environ.get("COMFYUI_TOKEN", "")
    if len(token) < 32:
        raise SystemExit("COMFYUI_TOKEN must contain at least 32 characters")

    if any(
        value <= 0
        for value in (
            args.max_body,
            args.max_ws_message,
            args.max_connections,
            args.grant_ttl,
            args.session_ttl,
            args.max_grants,
            args.max_sessions,
        )
    ):
        raise SystemExit("bridge limits must be positive")
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.minimum_version = ssl.TLSVersion.TLSv1_2
    tls.load_cert_chain(args.tls_cert, args.tls_key)
    web.run_app(
        build_app(args, token),
        host=args.host,
        port=args.port,
        ssl_context=tls,
        access_log=None,
    )


if __name__ == "__main__":
    main()
