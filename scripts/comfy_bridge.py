#!/usr/bin/env python3

"""Authenticated HTTP/WebSocket bridge from Docker Desktop to macOS ComfyUI."""

from __future__ import annotations

import argparse
import asyncio
import hmac
import os

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
    return {
        name: value
        for name, value in request.headers.items()
        if name.lower()
        not in HOP_BY_HOP | {"authorization", "content-length", "host"}
    }


def response_headers(headers) -> dict[str, str]:
    return {
        name: value
        for name, value in headers.items()
        if name.lower() not in HOP_BY_HOP | {"content-length"}
    }


async def websocket_proxy(request: web.Request, upstream_url: str) -> web.StreamResponse:
    downstream = web.WebSocketResponse(heartbeat=30)
    await downstream.prepare(request)
    session: ClientSession = request.app["client"]

    try:
        upstream = await session.ws_connect(
            upstream_url,
            headers=request_headers(request),
            heartbeat=30,
            max_msg_size=0,
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
            elif message.type == WSMsgType.PING:
                await upstream.ping(message.data)
            elif message.type == WSMsgType.PONG:
                await upstream.pong(message.data)
            elif message.type in {WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.ERROR}:
                break

    async def upstream_to_client() -> None:
        async for message in upstream:
            if message.type == WSMsgType.TEXT:
                await downstream.send_str(message.data)
            elif message.type == WSMsgType.BINARY:
                await downstream.send_bytes(message.data)
            elif message.type == WSMsgType.PING:
                await downstream.ping(message.data)
            elif message.type == WSMsgType.PONG:
                await downstream.pong(message.data)
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
    websocket = web.WebSocketResponse().can_prepare(request)
    if websocket.ok:
        return await websocket_proxy(request, upstream_url)

    session: ClientSession = request.app["client"]
    body = await request.read()
    async with session.request(
        request.method,
        upstream_url,
        data=body,
        headers=request_headers(request),
        allow_redirects=False,
    ) as upstream:
        downstream = web.StreamResponse(
            status=upstream.status,
            reason=upstream.reason,
            headers=response_headers(upstream.headers),
        )
        await downstream.prepare(request)
        async for chunk in upstream.content.iter_any():
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
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8190)
    parser.add_argument("--upstream", default="http://127.0.0.1:8188")
    args = parser.parse_args()

    token = os.environ.get("COMFYUI_TOKEN", "")
    if len(token) < 32:
        raise SystemExit("COMFYUI_TOKEN must contain at least 32 characters")

    app = web.Application(client_max_size=512 * 1024**2)
    app["token"] = token
    app["upstream"] = args.upstream.rstrip("/")
    app.on_startup.append(create_client)
    app.on_cleanup.append(close_client)
    app.router.add_route("*", "/{tail:.*}", proxy)
    web.run_app(app, host=args.host, port=args.port, access_log=None)


if __name__ == "__main__":
    main()
