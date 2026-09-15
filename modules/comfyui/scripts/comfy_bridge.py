#!/usr/bin/env python3

"""Authenticated HTTP/WebSocket bridge from Docker Desktop to macOS ComfyUI."""

from __future__ import annotations

import argparse
import asyncio
import hmac
import os
import ssl

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


def authorized(request: web.Request) -> bool:
    expected = f"Bearer {request.app['token']}"
    supplied = request.headers.get("Authorization", "")
    return hmac.compare_digest(supplied, expected)


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


async def proxy(request: web.Request) -> web.StreamResponse:
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
    args = parser.parse_args()

    token = os.environ.get("COMFYUI_TOKEN", "")
    if len(token) < 32:
        raise SystemExit("COMFYUI_TOKEN must contain at least 32 characters")

    if args.max_body <= 0 or args.max_ws_message <= 0 or args.max_connections <= 0:
        raise SystemExit("bridge limits must be positive")
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.minimum_version = ssl.TLSVersion.TLSv1_2
    tls.load_cert_chain(args.tls_cert, args.tls_key)
    app = web.Application(client_max_size=args.max_body)
    app["token"] = token
    app["upstream"] = args.upstream.rstrip("/")
    app["max_ws_message"] = args.max_ws_message
    app["max_response"] = args.max_body
    app["connections"] = asyncio.Semaphore(args.max_connections)
    app.on_startup.append(create_client)
    app.on_cleanup.append(close_client)
    app.router.add_route("*", "/{tail:.*}", proxy)
    web.run_app(app, host=args.host, port=args.port, ssl_context=tls, access_log=None)


if __name__ == "__main__":
    main()
