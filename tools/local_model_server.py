#!/usr/bin/env python3
"""The authenticated listener in front of one engine worker process.

Three jobs, none of which belong in the process that owns the accelerator:

* hold the socket the Docker VM reaches, and authenticate every request against the session
  credential;
* translate between OpenAI-shaped HTTP and the worker's line-delimited control protocol;
* stay answerable while the engine is busy, so that ``/v1/harness/status`` describes a worker that
  is thinking rather than a worker that has gone quiet.

That third point is the reason this is a separate process and not a thread. The status and
token-counting paths are the harness's evidence about the engine, and a status endpoint that only
answers between tokens cannot distinguish a native-context prefill from a device that stopped.
Process isolation guarantees **listener liveness**; it does not guarantee that a wedged Metal call
releases its memory, and this file does not pretend otherwise. GPU references are released by the
worker's own synchronisation, and the last-resort guarantee is that killing the holder of a
reservation frees the reservation, which a thread cannot offer.

Standard library only. The frontend runs inside the provisioned native runtime and adds no
dependency of its own: a web framework would be a second reason for the host interpreter to fail to
start, for no capability the platform does not already have.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import hmac
import json
import os
import queue
import secrets
import signal
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
for entry in (str(ROOT / "tools"), str(ROOT / "proxy")):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from local_engine_contract import (  # noqa: E402
    WorkerError,
    unsupported_request_fields,
    validate_status_document,
)

#: What a status or counting call may take. Neither holds an execution permit, so both must be able
#: to answer while a generation is running; a bound here is what makes "the frontend is stuck" a
#: detectable condition rather than a hang.
CONTROL_TIMEOUT_SECONDS = 30.0

#: What one exact count may take from the tokenizer process. Far below the proxy's
#: preparation timeout on purpose: the point of the separate process is that this
#: never queues behind generation, so a longer bound here would only delay the
#: fallback that does.
COUNT_TIMEOUT_SECONDS = 20.0
MAX_REQUEST_BYTES = 64 * 1024 * 1024
#: SSE comments during a long prefill. Deliberately a comment line, not a chunk: a keepalive that
#: looks like model output would be counted as progress by exactly the lease that exists to notice
#: the absence of progress.
KEEPALIVE_INTERVAL_SECONDS = 15.0

#: How long a generation may go without emitting a *token* before this frontend gives up on it.
#: Deliberately an idle bound rather than a total one and set far above any plausible prefill: an
#: absolute cap on a request is what kills the longest, most valuable work, while silence after the
#: stream already started is a different kind of evidence. Keepalives do not reset this clock.
GENERATION_IDLE_LIMIT_SECONDS = 900.0


class _EOF:
    """Poison pill from the reader thread: the channel ended, and here is why.

    A sentinel rather than an exception raised in the thread, because nothing in that thread has a
    caller to receive it — the reason has to travel the same queue as the documents so the consumer
    that would have read the next one learns that there never will be one.
    """

    __slots__ = ("reason",)

    def __init__(self, reason: str) -> None:
        self.reason = reason


class WorkerGone(RuntimeError):
    """The engine process is no longer there. Nothing about this request can be answered."""


class WorkerChannel:
    """One line-delimited pipe pair to one engine process, serialised.

    The worker runs one generation at a time by construction, so requests are multiplexed over a
    single channel with a lock rather than queued invisibly behind a socket. The lock is held for
    the duration of a request, which is also what makes cancellation legible: a second caller is
    waiting on the channel, not on a device that has already stopped.
    """

    def __init__(self, argv: list[str], environment: dict[str, str], plan: dict[str, Any]) -> None:
        self.plan = plan
        self._sequence = 0
        self._lock = threading.Lock()
        self._inbox: queue.Queue[Any] = queue.Queue()
        self._fatal: str | None = None
        self._status: dict[str, Any] | None = None
        self._started_at = time.monotonic()
        # The resolved engine document reaches the worker on descriptor 3. It is not an argument:
        # the process table is readable by every other user on the machine, and this document is
        # the fingerprint the whole identity check rests on.
        plan_read, plan_write = os.pipe()
        with os.fdopen(plan_write, "w", encoding="utf-8") as sink:
            json.dump(plan, sink)
        os.set_inheritable(plan_read, True)

        # A dedicated byte channel for "stop". It is not a command on the existing pipe because
        # the command pipe is read only between requests, and the entire point of a cancellation is
        # that a request is in flight. Written from the disconnect path, which cannot also be
        # waiting on the channel lock that the generation itself is holding.
        cancel_read, self._cancel_write = os.pipe()
        os.set_inheritable(cancel_read, True)

        def _hand_over_plan() -> None:  # pragma: no cover - runs between fork and exec
            os.dup2(plan_read, 3)
            if plan_read != 3:
                os.close(plan_read)
            os.dup2(cancel_read, 4)
            if cancel_read != 4:
                os.close(cancel_read)

        self.process = subprocess.Popen(  # noqa: S603 - argv is built from the pinned runtime
            [*argv, "--plan-fd", "3", "--cancel-fd", "4"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
            pass_fds=(plan_read, cancel_read),
            preexec_fn=_hand_over_plan,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
        os.close(plan_read)
        # The parent keeps only the write end. Holding the read end too would mean the worker's
        # blocking read(1) could never see EOF, not even when this process has gone away.
        os.close(cancel_read)
        self._cancel_read_in_parent = None
        self._stdin = self.process.stdin
        self._stdout = self.process.stdout
        #: Held as an attribute so teardown can close it. The diagnostics reader below already
        #: reached through ``self.process.stderr``, which left this stream out of the close list
        #: and leaked a descriptor per engine stop.
        self._stderr = self.process.stderr
        # Started here rather than lazily: a document that arrives before the first consumer is
        # waiting must be kept, not lost to a pipe nobody has looked at yet.
        threading.Thread(
            target=self._reader, name="mlx-worker-reader", daemon=True
        ).start()

    # ------------------------------------------------------------------ lifecycle

    def cancel(self) -> None:
        """Tell the worker to abandon the generation in flight, without waiting for it.

        One byte is the protocol. Nothing here takes ``self._lock``: the lock is held by the very
        generation being cancelled, so a cancellation that queued behind it would arrive after the
        work it meant to stop. This is what lets a client hanging up actually release the device
        rather than leave the worker running to its token budget for an audience of none.
        """
        try:
            if self._cancel_write is not None:
                os.write(self._cancel_write, b"x")
        except OSError:
            # The worker is gone, which is a worse-but-equally-final answer than cancelling.
            pass

    def await_ready(self, timeout: float) -> dict[str, Any]:
        """Read the worker's opening document: readiness is *its* claim, not ours to assume."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise WorkerGone(self._exit_reason())
            message = self._read_line(deadline - time.monotonic())
            if message is None:
                continue
            if message.get("kind") == "fatal":
                self._fatal = str(message.get("reason", "engine failed during startup"))
                raise WorkerGone(self._fatal)
            if message.get("kind") == "ready":
                status = validate_status_document(message.get("status") or {})
                self._status = status
                return status
        raise WorkerGone(f"engine did not report readiness within {timeout}s")

    def alive(self) -> bool:
        return self.process.poll() is None and self._fatal is None

    def _exit_reason(self) -> str:
        code = self.process.poll()
        detail = ""
        try:
            if self.process.stderr is not None:
                detail = (self.process.stderr.read() or "")[-2000:]
        except (OSError, ValueError):
            detail = ""
        suffix = f": {detail}" if detail.strip() else ""
        if self._fatal:
            return f"engine latched a fatal failure: {self._fatal}"
        return f"engine process exited with status {code}{suffix}"

    def fatal(self) -> str | None:
        return self._fatal or (None if self.alive() else self._exit_reason())

    def stop(self) -> None:
        """End the worker, then close. Not the other way round.

        The reader thread is parked in ``readline()`` on the worker's stdout. Closing that stream
        from this thread while a blocking read is outstanding does not release it — the fd is
        released underneath a C-level read that keeps waiting — so a stop() that closed first hung
        the frontend's own teardown and it never exited on SIGTERM. Terminating the child closes the
        writer end, the read returns EOF, and the reader posts its sentinel and finishes.

        Closing the cancel write end happens first and deliberately: it is not read by the worker as
        a shutdown signal (see :func:`watch_cancellation`, which ignores an empty read), and leaving
        it open leaked one descriptor per engine teardown while also keeping the worker's read from
        ever seeing EOF.
        """
        self._close_cancel()
        if self.process.poll() is None:
            # escalate, never wait forever: a worker blocked in a driver call will not read the
            # termination signal, and the launcher cannot be held hostage by it.
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        self._close_pipes()

    def _close_cancel(self) -> None:
        for attribute in ("_cancel_write",):
            fd = getattr(self, attribute, None)
            if fd is not None:
                with contextlib.suppress(OSError):
                    os.close(fd)
                setattr(self, attribute, None)

    def _close_pipes(self) -> None:
        # All three, not just stdin: stdout is the stream the reader thread is parked in and
        # stderr is the worker's diagnostics pipe, and leaving either open leaks a descriptor on
        # every engine teardown. Closing stdout is also how a wedged reader is released — the
        # thread's readline() raises, and _reader turns that into the same EOF it would have
        # reported for a clean exit.
        for stream in (self._stdin, self._stdout, self._stderr):
            try:
                if stream is not None and not stream.closed:
                    stream.close()
            except OSError:
                pass

    # ------------------------------------------------------------------ requests

    def call(self, action: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Send one request and return its terminal message.

        The channel is held for the whole exchange: the worker serves one thing at a time, so two
        callers interleaving on the pipe would read each other's responses.
        """
        with self._lock:
            if self._fatal is not None:
                raise WorkerGone(self._fatal)
            if self.process.poll() is not None:
                raise WorkerGone(self._exit_reason())
            self._sequence += 1
            sequence = self._sequence
            if self._stdin is None:
                raise WorkerGone("engine channel is closed")
            self._stdin.write(
                json.dumps(
                    {"action": action, "sequence": sequence, "payload": payload or {}},
                    separators=(",", ":"),
                    allow_nan=False,
                )
                + "\n"
            )
            self._stdin.flush()
            while True:
                message = self._read_line(CONTROL_TIMEOUT_SECONDS)
                if message is None:
                    raise WorkerGone(f"engine produced nothing within {CONTROL_TIMEOUT_SECONDS}s")
                if message.get("kind") == "fatal":
                    self._fatal = str(message.get("reason", "engine failed"))
                    raise WorkerGone(self._fatal)
                if message.get("sequence") == sequence and message.get("kind") != "token":
                    return message

    def stream(self, payload: dict[str, Any]):
        """Yield generated text, keeping the channel for the whole generation.

        Emits a keepalive whenever the worker is silent for too long, so a slow prefill looks
        different from a dead device at the HTTP layer. The keepalive is an SSE comment and carries
        no delta, which is what allows the proxy's progress lease to remain honest.
        """
        with self._lock:
            if not self.alive():
                raise WorkerGone(self._fatal or self._exit_reason())
            self._sequence += 1
            sequence = self._sequence
            if self._stdin is None:
                raise WorkerGone("engine channel is closed")
            self._stdin.write(
                json.dumps(
                    {"action": "generate", "sequence": sequence, "payload": payload},
                    separators=(",", ":"),
                    allow_nan=False,
                )
                + "\n"
            )
            self._stdin.flush()
            idle_started = time.monotonic()
            while True:
                remaining = KEEPALIVE_INTERVAL_SECONDS
                if self._stdout is None:
                    raise WorkerGone("engine channel is closed")
                message = self._read_line(remaining)
                if message is None:
                    if not self.alive():
                        raise WorkerGone(self._fatal or self._exit_reason())
                    # Bounded by *silence since the last real token*, not by elapsed wall clock: an
                    # overall cap would kill the slowest native-context prefill, which is the one
                    # request worth waiting for. Keepalives do not reset it.
                    if time.monotonic() - idle_started >= GENERATION_IDLE_LIMIT_SECONDS:
                        raise WorkerGone(
                            f"engine emitted nothing for {GENERATION_IDLE_LIMIT_SECONDS:.0f}s "
                            "mid-generation"
                        )
                    if time.monotonic() - idle_started >= KEEPALIVE_INTERVAL_SECONDS:
                        yield {"kind": "keepalive"}
                    continue
                if message.get("kind") == "fatal":
                    self._fatal = str(message.get("reason", "engine failed"))
                    raise WorkerGone(self._fatal)
                if message.get("sequence") != sequence:
                    continue
                kind = message.get("kind")
                if kind == "token":
                    idle_started = time.monotonic()
                    yield {"kind": "token", "text": str(message.get("text", ""))}
                elif kind == "end":
                    # Carried through rather than inferred. The worker knows whether the model said
                    # stop or the budget ran out; this layer does not, and a hardcoded "stop" here
                    # would relabel a truncation as a finished answer for every client.
                    yield {
                        "kind": "end",
                        "content_chars": int(message.get("content_chars", 0)),
                        "finish_reason": message.get("finish_reason") or "length",
                        "generation_tokens": int(message.get("generation_tokens") or 0),
                        "peak_memory_bytes": message.get("peak_memory_bytes"),
                        "cancelled": bool(message.get("cancelled")),
                    }
                    return
                elif kind == "refused":
                    yield {"kind": "refused", "code": message.get("code")}
                    return

    def _reader(self) -> None:
        """Drain the worker's stdout into a queue, one document at a time, until it ends.

        A dedicated reader exists because ``select()`` on the pipe and ``readline()`` on a buffered
        text stream disagree about where a line lives. The wrapper reads ahead in 8 KiB blocks
        regardless of ``bufsize=1`` — line buffering governs writes, not reads — so a worker that
        wrote a token and its terminal ``end`` back to back left the second line stranded in
        userspace, invisible to ``select()`` forever. The caller then reported a live worker as idle
        and the frontend hung while its own health endpoint answered ``ready``.

        Reading in a thread removes the fd/userspace split entirely: there is one reader, and it
        never asks the kernel about bytes the interpreter already holds. It also keeps draining when
        no HTTP client is reading, which is what stops a disconnected client from filling the pipe
        and parking the worker inside its generation loop.
        """
        stream = self._stdout
        assert stream is not None  # set by __init__ before the thread starts
        while True:
            try:
                line = stream.readline()
            except (OSError, ValueError) as failure:
                self._inbox.put(_EOF(f"engine channel failed: {failure}"))
                return
            if not line:
                self._inbox.put(_EOF(self._exit_reason()))
                return
            line = line.strip()
            if not line:
                continue
            try:
                self._inbox.put(json.loads(line))
            except ValueError:
                # A torn control line means the channel can no longer be trusted to be synchronised
                # with anything. Recovering from it would require framing the worker does not
                # provide, so this is terminal by design.
                self._inbox.put(_EOF("engine control channel produced unparseable output"))
                return

    def _read_line(self, timeout: float) -> dict[str, Any] | None:
        """Next worker document, or ``None`` if none arrived within ``timeout``.

        Never returns a document the interpreter already holds but the kernel does not: that
        distinction is the whole reason for :meth:`_reader`.
        """
        try:
            message = self._inbox.get(timeout=max(0.05, timeout))
        except queue.Empty:
            if self.process.poll() is not None:
                raise WorkerGone(self._exit_reason()) from None
            return None
        if isinstance(message, _EOF):
            raise WorkerGone(message.reason)
        return message


class TokenCounter:
    """A tokenizer-only child process, so an exact count never waits for a generation.

    The worker reads one command at a time, which is a property of MLX's stream and cache rules and
    not something the frontend's locking can work around: releasing the channel lock would only
    move the queue from Python into the worker's stdin loop. Counting needs no device, so it runs
    beside the engine instead of in front of it.

    Availability is reported rather than assumed. If this process dies, counts fall back to the
    engine channel — correct but slow — and ``status`` says so, because silently returning to the
    behaviour that caused the original timeout would be invisible from outside.
    """

    def __init__(self, python: str, script: str, model_dir: str, environment: dict[str, str]):
        self._lock = threading.Lock()
        self._sequence = 0
        self._failed: str | None = None
        self.script = script
        try:
            self.process = subprocess.Popen(  # noqa: S603 - both paths come from the launch plan
                [python, script, "--model-dir", model_dir],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=environment,
                text=True,
                encoding="utf-8",
                bufsize=1,
            )
        except OSError as exc:
            self.process = None
            self._failed = f"counter could not start: {exc}"

    @property
    def available(self) -> bool:
        if self.process is None:
            return False
        code = self.process.poll()
        if code is None:
            return True
        # Record *why* it stopped being available. Without this the frontend can report a degraded
        # counting path but not what caused it, which is how a counter that dies on the operator's
        # machine becomes an afternoon of guessing instead of one line of output.
        if self._failed is None:
            self._failed = f"token counter exited with status {code}"
        return False

    @property
    def failure(self) -> str | None:
        return self._failed

    def count(self, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
        """Ask for an exact count. Raises WorkerGone if this process cannot answer."""
        if not self.available:
            raise WorkerGone(self._failed or "token counter has exited")
        with self._lock:
            assert self.process is not None and self.process.stdin is not None
            assert self.process.stdout is not None
            self._sequence += 1
            sequence = self._sequence
            self.process.stdin.write(
                json.dumps(
                    {"action": "input_tokens", "sequence": sequence, "payload": payload},
                    separators=(",", ":"),
                    allow_nan=False,
                )
                + "\n"
            )
            self.process.stdin.flush()
            deadline = time.monotonic() + timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise WorkerGone(f"token counter produced nothing within {timeout}s")
                line = self.process.stdout.readline()
                if not line:
                    raise WorkerGone(self._exit_reason())
                line = line.strip()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                except ValueError:
                    raise WorkerGone("token counter produced unparseable output") from None
                if message.get("kind") == "fatal":
                    self._failed = str(message.get("reason", "counter failed"))
                    raise WorkerGone(self._failed)
                if message.get("sequence") == sequence:
                    return message

    def _exit_reason(self) -> str:
        code = self.process.returncode if self.process is not None else None
        return f"token counter exited with code {code}"

    def stop(self) -> None:
        if self.process is None:
            return
        try:
            if self.process.stdin is not None:
                self.process.stdin.close()
        except OSError:
            pass
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        for stream in (self.process.stdout, self.process.stderr):
            try:
                if stream is not None:
                    stream.close()
            except OSError:
                pass


class Frontend:
    """The HTTP surface. One model, one worker, one credential."""

    def __init__(
        self,
        channel: WorkerChannel,
        token: str,
        plan: dict[str, Any],
        expected_status: dict[str, Any],
        counter: TokenCounter | None = None,
    ) -> None:
        self.channel = channel
        self.counter = counter
        self.counter_failures = 0
        self.count_fallbacks = 0
        self.token_digest = hashlib.sha256(token.encode()).digest()
        self.plan = plan
        self.expected_status = expected_status
        # Status is served from a snapshot taken at readiness and refreshed on request, never from
        # a worker round trip: the endpoint has to answer during a generation, and a status that
        # queues behind the thing it is reporting on is not evidence about it.
        self._status_lock = threading.Lock()
        self._status: dict[str, Any] = dict(expected_status)
        self._status_at = time.monotonic()
        self.requests = 0

    def note_counter_failure(self, reason: str) -> None:
        self.counter_failures += 1
        self._counter_reason = reason

    def note_count_fallback(self) -> None:
        self.count_fallbacks += 1

    def authorized(self, header: str | None) -> bool:
        if not header or not header.lower().startswith("bearer "):
            return False
        presented = header[7:].strip().encode()
        # Constant-time on the digest rather than the token: an attacker able to time this
        # comparison learns nothing usable, and the value being compared is the session credential.
        return hmac.compare_digest(hashlib.sha256(presented).digest(), self.token_digest)

    def publish_status(self, status: dict[str, Any]) -> None:
        with self._status_lock:
            self._status = status
            self._status_at = time.monotonic()

    def snapshot(self) -> dict[str, Any]:
        with self._status_lock:
            document = dict(self._status)
        fatal = self.channel.fatal()
        document["fatal"] = fatal
        document["ready"] = fatal is None and bool(document.get("ready"))
        document["observations"] = {
            **(document.get("observations") or {}),
            "status_age_seconds": round(time.monotonic() - self._status_at, 1),
            "engine_alive": self.channel.alive(),
            "uptime_seconds": round(time.monotonic() - self.channel._started_at, 1),
            # Reported so a degraded counting path is visible from outside rather than being
            # experienced as unexplained slowness. Counting still works when the counter is gone —
            # it just queues behind generation again, which is the condition this split exists to
            # remove.
            "token_counter_available": bool(self.counter and self.counter.available),
            "token_counter_failures": self.counter_failures,
            "counts_served_by_engine": self.count_fallbacks,
            "token_counter_reason": getattr(self, "_counter_reason", None),
            "counter_failure_reason": (None if self.counter is None else self.counter.failure),
        }
        return document

    def refresh_status(self) -> dict[str, Any]:
        try:
            message = self.channel.call("status")
        except WorkerGone:
            return self.snapshot()
        status = message.get("status")
        if isinstance(status, dict):
            self.publish_status(status)
        return self.snapshot()


def make_handler(frontend: Frontend) -> type[BaseHTTPRequestHandler]:
    """Build the request handler bound to one frontend.

    A class factory rather than globals: the process serves exactly one model, and binding the
    state into the handler keeps "which engine did this request reach" unanswerable by construction.
    """

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "kimi-local-engine/1"

        # ---------------------------------------------------------------- plumbing

        def log_message(self, *_args: Any) -> None:  # noqa: D102 - required by BaseHTTPRequestHandler
            # The default access log writes the request line, and a request line is a path; the
            # bodies that matter are logged by the proxy, which is the only place that can tie them
            # to a lane and a request id.
            return

        def _send(
            self, status: int, payload: bytes, content_type: str = "application/json"
        ) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload)

        def _error(self, status: int, code: str, message: str) -> None:
            body = {"error": {"message": message, "type": code, "code": status}}
            self._send(status, json.dumps(body, separators=(",", ":")).encode())

        def _guard(self) -> bool:
            if not frontend.authorized(self.headers.get("Authorization")):
                # 401 rather than 403: the harness cannot tell an unauthenticated caller from a
                # stale token, and a 403 would imply the identity was recognised.
                self._error(401, "unauthorized", "missing or invalid bearer token")
                return False
            return True

        def _body(self) -> dict[str, Any]:
            declared = self.headers.get("Content-Length")
            size = int(declared) if declared and declared.isdigit() else 0
            if size > MAX_REQUEST_BYTES:
                raise ValueError("request body exceeds the supported size")
            raw = self.rfile.read(size) if size else b""
            document = json.loads(raw or b"{}")
            if not isinstance(document, dict):
                raise ValueError("request body must be a JSON object")
            return document

        # ---------------------------------------------------------------- routes

        def do_GET(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
            path = urllib.parse.urlsplit(self.path).path
            if path in {"/v1/harness/status", "/health", "/v1/health"}:
                # Every endpoint on this listener authenticates, including health. The harness
                # probes liveness by connect() and process exit, not by an unauthenticated route,
                # and a document carrying the engine fingerprint, the plan identity and the memory
                # counters is not something to hand to whatever can reach the port.
                if not self._guard():
                    return
                self._send(
                    200,
                    json.dumps(
                        frontend.snapshot(), separators=(",", ":"), allow_nan=False
                    ).encode(),
                )
                return
            if path == "/v1/models":
                if not self._guard():
                    return
                self._send(
                    200,
                    json.dumps(
                        {
                            "object": "list",
                            "data": [{"id": self._model_id(), "object": "model"}],
                        }
                    ).encode(),
                )
                return
            self._error(404, "not_found", f"no route {path}")

        def _model_id(self) -> str:
            return str(frontend.plan.get("model_alias") or "")

        def do_POST(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
            path = urllib.parse.urlsplit(self.path).path
            if not self._guard():
                return
            try:
                payload = self._body()
            except (ValueError, json.JSONDecodeError) as exc:
                self._error(400, str(WorkerError.MALFORMED_REQUEST), str(exc))
                return
            bound = self._model_id()
            requested = str(payload.get("model") or bound)
            if requested != bound:
                # The proxy rewrites the model field to the bound name on every local request, so a
                # mismatch means something reached the engine that did not go through pricing.
                self._error(400, str(WorkerError.MODEL_MISMATCH), f"serves {bound!r} only")
                return
            if path == "/v1/chat/completions/input_tokens":
                self._count(payload)
                return
            if path == "/v1/chat/completions":
                self._complete(payload)
                return
            self._error(404, "not_found", f"no route {path}")

        def _count(self, payload: dict[str, Any]) -> None:
            # Counting goes to the tokenizer-only process when it is alive, so a queued request
            # never waits behind someone else's prefill and then times out into a retry that queues
            # again behind the same prefill. The engine channel stays as the fallback: a missing
            # count is worse than a slow one, but the degradation is reported, not hidden.
            counter = frontend.counter
            message: dict[str, Any] | None = None
            if counter is not None and counter.available:
                try:
                    message = counter.count(payload, COUNT_TIMEOUT_SECONDS)
                except WorkerGone as gone:
                    frontend.note_counter_failure(str(gone))
                    message = None
            if message is None:
                frontend.note_count_fallback()
                try:
                    message = frontend.channel.call("input_tokens", payload)
                except WorkerGone as gone:
                    self._error(503, str(WorkerError.COMPUTE_FAILED), str(gone))
                    return
            if message.get("kind") == "refused":
                self._error(400, str(message.get("code")), "engine refused the request")
                return
            self._send(200, json.dumps(message.get("count") or {}).encode())

        def _complete(self, payload: dict[str, Any]) -> None:
            refused = unsupported_request_fields(payload)
            if refused:
                # Answered before the headers, not inside the stream. Once 200 is out, the only way
                # to tell the client its request was inadmissible is an in-band error object, and
                # the proxy classifies an error object as engine damage and latches the lane — so a
                # client setting one unsupported field could disable a healthy engine for the rest
                # of the session. The worker refuses these too; this is the same policy (shared via
                # local_engine_contract so it cannot drift) applied where the status code is still
                # chooseable.
                self._error(
                    400,
                    str(WorkerError.UNSUPPORTED_SETTING),
                    "unsupported request fields: " + ", ".join(refused),
                )
                return
            if payload.get("stream") is False:
                self._complete_whole(payload)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            identifier = f"chatcmpl-{secrets.token_hex(12)}"
            created = int(time.time())
            try:
                for event in frontend.channel.stream(payload):
                    if event["kind"] == "keepalive":
                        # A comment, so no reader can mistake it for content. Its only job is to
                        # keep the connection recognisably alive across a long prefill.
                        self.wfile.write(b": keepalive\n\n")
                    elif event["kind"] == "token":
                        chunk = {
                            "id": identifier,
                            "object": "chat.completion.chunk",
                            "created": created,
                            "model": self._model_id(),
                            "choices": [
                                {"index": 0, "delta": {"content": event["text"]},
                                 "finish_reason": None}
                            ],
                        }
                        self.wfile.write(
                            b"data: " + json.dumps(chunk, separators=(",", ":")).encode() + b"\n\n"
                        )
                    elif event["kind"] == "refused":
                        error = {
                            "error": {
                                "message": "engine refused the request",
                                "type": str(event.get("code")),
                            }
                        }
                        self.wfile.write(
                            b"data: " + json.dumps(error, separators=(",", ":")).encode() + b"\n\n"
                        )
                        self.wfile.write(b"data: [DONE]\n\n")
                    elif event["kind"] == "end":
                        final = {
                            "id": identifier,
                            "object": "chat.completion.chunk",
                            "created": created,
                            "model": self._model_id(),
                            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                        }
                        self.wfile.write(
                            b"data: " + json.dumps(final, separators=(",", ":")).encode() + b"\n\n"
                        )
                        self.wfile.write(b"data: [DONE]\n\n")
            except (WorkerGone, BrokenPipeError, ConnectionResetError) as failure:
                if not isinstance(failure, WorkerGone):
                    # The client hung up while the engine was mid-generation. Nothing else in this
                    # process will ever read the rest of that completion, so without a cancellation
                    # the worker runs to its token budget for an audience of none — still holding
                    # the one execution slot and the device memory behind it — while /health keeps
                    # answering ready. Sent before anything else, and on its own byte channel,
                    # because the channel lock belongs to the generation being abandoned.
                    frontend.channel.cancel()
                # Headers are already sent, so the status line cannot be repaired. An in-band error
                # object is the only honest signal left, and the proxy classifies it.
                with_error = {
                    "error": {
                        "message": str(failure),
                        "type": str(WorkerError.COMPUTE_FAILED),
                    }
                }
                try:
                    self.wfile.write(
                        b"data: " + json.dumps(with_error, separators=(",", ":")).encode() + b"\n\n"
                    )
                    self.wfile.write(b"data: [DONE]\n\n")
                except OSError:
                    pass
            self.close_connection = True

        def _complete_whole(self, payload: dict[str, Any]) -> None:
            pieces: list[str] = []
            try:
                for event in frontend.channel.stream(payload):
                    if event["kind"] == "token":
                        pieces.append(event["text"])
                    elif event["kind"] == "end":
                        break
                    elif event["kind"] == "refused":
                        self._error(400, str(event.get("code")), "engine refused the request")
                        return
            except WorkerGone as gone:
                self._error(503, str(WorkerError.COMPUTE_FAILED), str(gone))
                return
            text = "".join(pieces)
            self._send(
                200,
                json.dumps(
                    {
                        "id": f"chatcmpl-{secrets.token_hex(12)}",
                        "object": "chat.completion",
                        "created": int(time.time()),
                        "model": self._model_id(),
                        "choices": [
                            {
                                "index": 0,
                                "message": {"role": "assistant", "content": text},
                                "finish_reason": "stop",
                            }
                        ],
                    }
                ).encode(),
            )

    return Handler


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--worker", required=True, help="worker executable")
    parser.add_argument("--worker-arg", action="append", default=[])
    parser.add_argument("--plan-file", required=True)
    parser.add_argument("--token-fd", type=int, default=-1)
    parser.add_argument("--ready-timeout", type=float, default=1800.0)
    parser.add_argument("--ready-file", default="")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    plan_path = Path(args.plan_file)
    try:
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"cannot read the resolved plan document: {exc}", file=sys.stderr)
        return 2
    if args.token_fd >= 0:
        with os.fdopen(args.token_fd, "r", encoding="utf-8") as handle:
            token = handle.read().strip()
    else:
        # An environment variable is the fallback for a manual run only. It is still not an
        # argument, and it is deleted from the environment as soon as it is read.
        token = os.environ.pop("LOCAL_MPS_TOKEN", "").strip()
    if len(token) < 32:
        print("a session credential of at least 32 characters is required", file=sys.stderr)
        return 2

    # Handlers are installed before the executor is spawned, and the whole lifecycle sits in one
    # try/finally, because the previous shape left two windows: no handler at all until just before
    # serve_forever(), and no teardown path for a signal that arrived during readiness, the plan
    # comparison, or the bind. A SIGTERM in either window ended the interpreter by default
    # disposition — running no ``finally`` — and left the worker resident with the model. The test
    # of the teardown raced that and failed roughly a third of the time; closing the window is the
    # fix, not a retry. macOS offers no PR_SET_PDEATHSIG equivalent, so there is nothing to fall
    # back on if this process dies unhandled.
    def _stop(signum, _frame):
        raise SystemExit(128 + int(signum))

    for signal_number in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(signal_number, _stop)

    channel: WorkerChannel | None = None
    listener: _Server | None = None
    counter: TokenCounter | None = None
    try:
        channel = WorkerChannel([args.worker, *args.worker_arg], dict(os.environ), plan)
        try:
            status = channel.await_ready(args.ready_timeout)
        except (WorkerGone, ValueError) as failure:
            print(f"engine failed to become ready: {failure}", file=sys.stderr)
            return 3

        mismatches = _compare_to_plan(status, plan)
        if mismatches:
            print(
                "engine readiness disagrees with the resolved plan: "
                + "; ".join(mismatches),
                file=sys.stderr,
            )
            return 4

        counter: TokenCounter | None = None
        if plan.get("model_dir"):
            # Same interpreter, same sanitized environment: the counter needs mlx_lm's tokenizer
            # loader and nothing else, so it shares the provisioned runtime rather than the host's.
            counter = TokenCounter(
                args.worker,
                str(Path(__file__).with_name("local_token_counter.py")),
                str(plan["model_dir"]),
                dict(os.environ),
            )
        frontend = Frontend(channel, token, plan, status, counter)
        handler = make_handler(frontend)
        try:
            listener = _Server((args.host, args.port), handler)
        except OSError as exc:
            print(f"cannot bind {args.host}:{args.port}: {exc}", file=sys.stderr)
            return 5

        if args.ready_file:
            # The receipt is written by the supervisor, after both identity verification and a real
            # generation have succeeded. Writing it here would repeat the mistake this replaced.
            Path(args.ready_file).write_text(
                json.dumps({"ready": True, "pid": os.getpid()}), encoding="utf-8"
            )
        print(
            f"local engine listening on {args.host}:{args.port} "
            f"(ctx={status['context_tokens']}, slots={status['sequences']})",
            flush=True,
        )
        listener.serve_forever()
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        if listener is not None:
            listener.shutdown()
            listener.server_close()
        if counter is not None:
            counter.stop()
        if channel is not None:
            channel.stop()
    return 0


def _compare_to_plan(status: dict[str, Any], plan: dict[str, Any]) -> list[str]:
    """Check the worker's own report against the plan it was launched from.

    Returns the list of mismatches instead of raising so the operator sees every way the two
    disagree at once, rather than fixing one and discovering the next on the following launch.
    """
    checks = {
        "model": plan.get("model_alias"),
        "context_tokens": plan.get("context_tokens"),
        "sequences": plan.get("sequences"),
        "total_context_tokens": plan.get("total_context_tokens"),
        "fingerprint": plan.get("fingerprint"),
        "launch_identity": plan.get("snapshot_id"),
        "artifact_format": plan.get("artifact_format"),
        "protocol_version": plan.get("protocol_version"),
    }
    problems: list[str] = []
    for field, expected in checks.items():
        if expected is None:
            continue
        if status.get(field) != expected:
            problems.append(f"{field}: engine {status.get(field)!r} != plan {expected!r}")
    return problems


if __name__ == "__main__":
    raise SystemExit(main())
