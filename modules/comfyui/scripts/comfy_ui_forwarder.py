#!/usr/bin/env python3

"""Forward the agent's plaintext ComfyUI frontend to the authenticated macOS bridge over TLS.

Why this exists
---------------
On an MPS host, ComfyUI listens on the host's own loopback, which no container can reach, and
the only path into it is the bridge's TLS listener. A browser cannot attach the bridge's bearer
token to a navigation or to the frontend's subresources, and Chromium will not trust the bridge's
per-launch self-signed certificate, so the agent needs an origin it can simply point at. This
process is that origin: plain HTTP bound to a Docker network private to the agent, pumped byte for
byte into the bridge over verified TLS.

What it does not do
-------------------
It parses no HTTP and terminates no session. Authentication stays entirely the bridge's decision,
on every path, exactly as before - which is why the same forwarder is correct in front of any
upstream. Because the listener is plaintext, it must never face anything but a private network:
the TLS hop is what keeps the bridge's credential off the host's LAN, and the CA is required
rather than optional so a missing certificate stops the service instead of quietly disabling
verification.

Why one loop owns each connection
---------------------------------
Both halves of a proxied conversation could be carried by a thread each, and that version was
written first. It is wrong. ``ssl.SSLSocket`` presents one OpenSSL record layer to its reader and
its writer, and ``SSL_write`` may itself pull bytes off the socket to serve a renegotiation while
``SSL_read`` is doing the same, so a reader and a writer in separate threads is not "one direction
each": measured on a 512 KiB body, 5 runs in 40 had the reader return an empty read - a clean
end-of-stream that its peer never sent - and the bytes behind it were gone. Serialising the two
threads behind one lock is not better either: whichever one blocks in ``recv`` holds the lock the
request half needs to finish sending, so the pair waits on each other until an idle timeout fires
instead of truncating.

So one asyncio loop owns every socket operation on a connection, in one thread, and the two
directions are coroutines rather than threads. The record layer is never entered concurrently
because there is no second thread to enter it from, and each direction still runs at full speed
because ``drain()`` provides the backpressure that a blocking ``sendall`` used to.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import signal
import socket
import ssl
import sys
import threading

#: A pump has no interest in message boundaries, so the buffer is sized for throughput alone.
BUFFER_BYTES = 64 * 1024

#: The ordinary ending for a direction: the stream ran out because a conversation finished.
CLEAN = "ran out"


async def pump(
    source: asyncio.StreamReader,
    sink: asyncio.StreamWriter,
    *,
    half_close: bool,
    idle_timeout: float,
) -> str:
    """Carry one direction until either end closes, stalls, or fails.

    ``half_close`` says whether this direction may signal end-of-stream to its sink, and only the
    plaintext side ever may. TLS has no half-close: shutting a ``ssl`` stream down discards the
    session in both directions, so ending the request side that way also kills the reply still in
    flight on the same connection.

    Returns how the direction ended. A stream that ran out is ordinary; anything else is a browser
    or a bridge that went away mid-conversation, and is worth the one line it costs.
    """
    try:
        while True:
            try:
                chunk = await asyncio.wait_for(source.read(BUFFER_BYTES), idle_timeout)
            except TimeoutError:
                # An idle keep-alive tab is the ordinary case: the browser sent its request and
                # then waited. Worth the line it costs, since a stream cut short by the idle bound
                # looks like a truncation from the client's side.
                return "stalled"
            if not chunk:
                break
            sink.write(chunk)
            try:
                await asyncio.wait_for(sink.drain(), idle_timeout)
            except TimeoutError:
                return "stalled"
    except (TimeoutError, OSError) as exc:
        # TimeoutError is an OSError, so this is one branch; ConnectionResetError from a reader
        # whose peer vanished mid-record, and BrokenPipeError from a tab the user closed, are the
        # everyday members of it.
        return f"stopped on {type(exc).__name__}"
    if half_close and sink.can_write_eof():
        with contextlib.suppress(OSError, RuntimeError):
            sink.write_eof()
    return CLEAN


async def aclose(writer: asyncio.StreamWriter) -> None:
    """Close a stream and see the close through.

    ``StreamWriter.close`` only schedules the descriptor release as a loop callback, so a listener
    that stops as soon as its conversations end can shut its loop down with the fd still open. The
    leak is per aborted tab, which is the ordinary way a browser conversation ends, so it is worth
    the await to make it per conversation instead. A close that fails was already going to happen.
    """
    writer.close()
    with contextlib.suppress(OSError, asyncio.CancelledError):
        await writer.wait_closed()


class UIForwarder:
    """The listener, its slot budget, and the notes it prints.

    Serving owns one event loop, so the slot counter needs no lock: every path through it runs on
    that loop's thread.
    """

    def __init__(self, options: argparse.Namespace):
        self.options = options
        #: Verification is unconditional: the CA comes from the volume the bridge's certificate
        #: was staged into, and the expected name is checked against it at connect time. There is
        #: no flag to relax either, because the whole reason this listener may be plaintext is
        #: that its peer is not.
        self.tls = ssl.create_default_context(cafile=options.tls_ca)
        self.address: tuple[str, int] | None = None
        self.active = 0
        self._live: set[asyncio.Task] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._server: asyncio.Server | None = None
        self._stop: asyncio.Future | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._bind_error: OSError | None = None

    def note(self, message: str) -> None:
        """One line about a connection, never a byte of one."""
        print(f"comfy-ui-forwarder: {message}", file=sys.stderr, flush=True)

    def serve_forever(self) -> None:
        """Bind and pump in this thread until ``stop`` is called from anywhere else.

        Signals belong to ``main``, whose handler asks this loop to stop the same way a test's
        teardown does. Nothing here needs unwinding beyond that, so a hard kill costs an open
        browser tab and nothing else.
        """
        try:
            loop = asyncio.new_event_loop()
            self._loop = loop
            asyncio.set_event_loop(loop)
            self._stop = loop.create_future()
            try:
                loop.run_until_complete(self._hold())
            except (KeyboardInterrupt, asyncio.CancelledError):
                pass
            except OSError as exc:
                self._bind_error = exc
            finally:
                with contextlib.suppress(Exception):
                    loop.run_until_complete(self._close())
                loop.close()
        finally:
            # Whatever went wrong, a caller waiting in ``start`` has to be told so, rather than
            # waiting for a listener that will never open.
            self._ready.set()

    async def _hold(self) -> None:
        self._server = await asyncio.start_server(
            self.handle, self.options.listen, self.options.port
        )
        # Port 0 is how a caller asks for whatever is free, so the address is read back rather
        # than assumed - the healthcheck and every test connect to this, not to a remembered port.
        self.address = self._server.sockets[0].getsockname()[:2]
        self._ready.set()
        await self._stop

    async def _close(self) -> None:
        if self._server is not None:
            self._server.close()
        # A browser that is still streaming when the container is asked to stop is not worth
        # waiting for: its tab dies with the listener anyway, and pending tasks left behind would
        # keep the loop from closing cleanly.
        for task in list(self._live):
            task.cancel()
        if self._live:
            await asyncio.gather(*list(self._live), return_exceptions=True)
        if self._server is not None:
            with contextlib.suppress(OSError):
                await self._server.wait_closed()
        # A task cancelled mid-conversation never reaches its own awaited close, and a transport
        # releases its descriptor in a loop callback, so one more turn of the loop is what stands
        # between a stopped service and a leaked fd.
        await asyncio.sleep(0)

    def start(self) -> None:
        """Serve on a background loop, and return only once the listener is actually open.

        Nobody can talk to a forwarder that has not bound yet, so both the entrypoint's banner and
        every caller's first connection depend on this waiting for the bind, not merely for the
        thread to have started.
        """
        self._thread = threading.Thread(
            target=self.serve_forever, name="comfy-ui-forwarder", daemon=True
        )
        self._thread.start()
        self._ready.wait()
        if self._bind_error is not None:
            raise self._bind_error

    def request_stop(self) -> None:
        """Ask the loop to finish from any thread, without waiting for it.

        This is the half a signal handler can safely run: it hands the loop a callback and returns,
        leaving the waiting to ``stop`` and to whoever owns the thread.
        """
        loop = self._loop
        if loop is not None:
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(self._request_stop_now)

    def stop(self, timeout: float = 30.0) -> None:
        """Ask the loop to finish, and wait for the service to be gone."""
        self.request_stop()
        if self._thread is not None:
            self._thread.join(timeout)

    def join(self) -> None:
        """Wait for the serving thread to finish, however it was asked to."""
        if self._thread is not None:
            self._thread.join()

    def _request_stop_now(self) -> None:
        if self._stop is not None and not self._stop.done():
            self._stop.set_result(None)

    async def handle(
        self, client_reader: asyncio.StreamReader, client_writer: asyncio.StreamWriter
    ) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._live.add(task)
        try:
            await self.forward(client_reader, client_writer)
        except (TimeoutError, OSError) as exc:
            # The default asyncio behaviour here is to dump a traceback naming the peer on every
            # aborted browser connection, which is noise a browser produces routinely. A cancelled
            # task is not this: it is the service shutting down, and it propagates on its own.
            self.note(f"connection ended on {type(exc).__name__}")
        finally:
            if task is not None:
                self._live.discard(task)
            # Every write was drained as it went, so nothing of the reply is still buffered here.
            await aclose(client_writer)

    async def forward(
        self, client_reader: asyncio.StreamReader, client_writer: asyncio.StreamWriter
    ) -> None:
        options = self.options
        if self.active >= options.max_connections:
            # Over capacity. Refusing is cheaper than queueing: a stalled tab would otherwise hold
            # every later request behind it, including the healthcheck's. The slot is taken before
            # anything is awaited, because a connect is a real wait and a second browser must see
            # the limit rather than the gap.
            self.note("refused a connection: no forward slots are free")
            return
        self.active += 1
        try:
            try:
                upstream = await asyncio.wait_for(self.connect(), options.connect_timeout)
            except (TimeoutError, OSError) as exc:
                # TimeoutError covers both a bridge that is not answering and a handshake that is
                # still going; a handshake that never finishes must not hold a slot either way.
                self.note(f"upstream unavailable: {type(exc).__name__}")
                return
            upstream_reader, upstream_writer = upstream
            try:
                for writer in (client_writer, upstream_writer):
                    connection = writer.transport.get_extra_info("socket")
                    if connection is not None:
                        with contextlib.suppress(OSError):
                            connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                endings = await asyncio.gather(
                    pump(
                        client_reader,
                        upstream_writer,
                        half_close=False,
                        idle_timeout=options.idle_timeout,
                    ),
                    pump(
                        upstream_reader,
                        client_writer,
                        half_close=True,
                        idle_timeout=options.idle_timeout,
                    ),
                )
                damaged = [ending for ending in endings if ending != CLEAN]
                if damaged:
                    self.note(", ".join(damaged))
            finally:
                await aclose(upstream_writer)
        finally:
            self.active -= 1

    async def connect(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        """Open one verified TLS connection to the bridge."""
        reader, writer = await asyncio.open_connection(
            self.options.upstream_host,
            self.options.upstream_port,
            ssl=self.tls,
            server_hostname=self.options.tls_server_name,
        )
        return reader, writer


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    # Loopback is the safe default; the compose overlay has to ask for the private network
    # explicitly rather than inheriting an open listener by omission.
    parser.add_argument("--listen", default="127.0.0.1")  # noqa: S104
    parser.add_argument("--port", type=int, default=8188)
    parser.add_argument("--upstream-host", default="host.docker.internal")
    parser.add_argument("--upstream-port", type=int, default=8190)
    parser.add_argument("--tls-ca", required=True)
    parser.add_argument("--tls-server-name", default="")
    parser.add_argument("--max-connections", type=int, default=16)
    parser.add_argument("--connect-timeout", type=float, default=10)
    parser.add_argument("--idle-timeout", type=float, default=3600)
    args = parser.parse_args(argv)
    args.tls_server_name = args.tls_server_name or args.upstream_host
    if args.port <= 0 or args.upstream_port <= 0:
        raise SystemExit("listener and upstream ports must be positive")
    if args.max_connections <= 0 or args.connect_timeout <= 0 or args.idle_timeout <= 0:
        raise SystemExit("forwarder limits must be positive")
    try:
        with open(args.tls_ca, "rb") as handle:
            if not handle.read(1):
                raise ValueError("empty certificate authority file")
    except OSError as exc:
        raise SystemExit(f"the bridge certificate is unreadable ({args.tls_ca}): {exc}") from None
    except ValueError as exc:
        raise SystemExit(f"the bridge certificate is unusable ({args.tls_ca}): {exc}") from None
    return args


def main(argv: list[str] | None = None) -> None:
    options = parse_args(argv)
    server = UIForwarder(options)
    # Bound before announcing, so the log line and the healthcheck's first probe cannot disagree
    # about whether this service is up.
    server.start()
    print(
        f"comfy-ui-forwarder: http://{server.address[0]}:{server.address[1]} -> "
        f"tls://{options.upstream_host}:{options.upstream_port}",
        file=sys.stderr,
        flush=True,
    )

    def requested(name: int, _frame: object) -> None:
        print(f"comfy-ui-forwarder: stopping on signal {name}", file=sys.stderr, flush=True)
        # Only the non-blocking half, because this runs on the thread that is about to wait in
        # ``join``: asking the loop to stop and returning is all a handler may do.
        server.request_stop()

    for name in (signal.SIGTERM, signal.SIGINT):
        signal.signal(name, requested)
    server.join()


if __name__ == "__main__":
    main()
