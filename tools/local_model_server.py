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
import hashlib
import hmac
import json
import os
import secrets
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
    validate_status_document,
)

#: What a status or counting call may take. Neither holds an execution permit, so both must be able
#: to answer while a generation is running; a bound here is what makes "the frontend is stuck" a
#: detectable condition rather than a hang.
CONTROL_TIMEOUT_SECONDS = 30.0
MAX_REQUEST_BYTES = 64 * 1024 * 1024
MAX_STATUS_BYTES = 1024 * 1024
#: SSE comments during a long prefill. Deliberately a comment line, not a chunk: a keepalive that
#: looks like model output would be counted as progress by exactly the lease that exists to notice
#: the absence of progress.
KEEPALIVE_INTERVAL_SECONDS = 15.0


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
        self._reader_lock = threading.Lock()
        self._deadline = 0.0
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

        def _hand_over_plan() -> None:  # pragma: no cover - runs between fork and exec
            os.dup2(plan_read, 3)
            if plan_read != 3:
                os.close(plan_read)

        self.process = subprocess.Popen(  # noqa: S603 - argv is built from the pinned runtime
            [*argv, "--plan-fd", "3"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
            pass_fds=(plan_read,),
            preexec_fn=_hand_over_plan,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
        os.close(plan_read)
        self._stdin = self.process.stdin
        self._stdout = self.process.stdout

    # ------------------------------------------------------------------ lifecycle

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
        for close in (self._close_pipes,):
            close()
        if self.process.poll() is None:
            # escalate, never wait forever: a worker blocked in a driver call will not read the
            # termination signal, and the launcher cannot be held hostage by it.
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()

    def _close_pipes(self) -> None:  # noqa: B027
        for stream in (self._stdin,):
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
                message = self._read_line(remaining, hold_lock=True)
                if message is None:
                    if not self.alive():
                        raise WorkerGone(self._fatal or self._exit_reason())
                    if time.monotonic() - idle_started >= KEEPALIVE_INTERVAL_SECONDS:
                        yield {"kind": "keepalive"}
                        idle_started = time.monotonic()
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
                    yield {"kind": "end", "content_chars": int(message.get("content_chars", 0))}
                    return
                elif kind == "refused":
                    yield {"kind": "refused", "code": message.get("code")}
                    return

    def _read_line(self, timeout: float, *, hold_lock: bool = False) -> dict[str, Any] | None:
        """Read one control line without blocking the frontend's other threads.

        ``select`` on the worker's stdout rather than a blocking ``readline`` is what lets a status
        request be answered while a generation is mid-flight: the read cannot park the interpreter
        in a syscall that outlives the caller's patience.
        """
        import select

        if self._stdout is None:
            raise WorkerGone("engine channel is closed")
        deadline = time.monotonic() + max(0.05, timeout)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            ready, _, _ = select.select([self._stdout], [], [], min(remaining, 0.25))
            if not ready:
                if self.process.poll() is not None:
                    raise WorkerGone(self._exit_reason())
                continue
            line = self._stdout.readline()
            if not line:
                raise WorkerGone(self._exit_reason())
            line = line.strip()
            if not line:
                continue
            try:
                return json.loads(line)
            except ValueError:
                # A torn control line means the channel can no longer be trusted to be
                # synchronised with anything. Recovering from it would require framing the worker
                # does not provide, so this is terminal by design.
                raise WorkerGone("engine control channel produced unparseable output") from None


class Frontend:
    """The HTTP surface. One model, one worker, one credential."""

    def __init__(
        self,
        channel: WorkerChannel,
        token: str,
        plan: dict[str, Any],
        expected_status: dict[str, Any],
    ) -> None:
        self.channel = channel
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
            if path in {"/v1/chat/completions", "/chat/completions"}:
                self._complete(payload)
                return
            self._error(404, "not_found", f"no route {path}")

        def _count(self, payload: dict[str, Any]) -> None:
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

    channel = WorkerChannel(
        [args.worker, *args.worker_arg],
        dict(os.environ),
        plan,
    )
    try:
        status = channel.await_ready(args.ready_timeout)
    except (WorkerGone, ValueError) as failure:
        print(f"engine failed to become ready: {failure}", file=sys.stderr)
        channel.stop()
        return 3

    mismatches = _compare_to_plan(status, plan)
    if mismatches:
        print(
            "engine readiness disagrees with the resolved plan: " + "; ".join(mismatches),
            file=sys.stderr,
        )
        channel.stop()
        return 4

    frontend = Frontend(channel, token, plan, status)
    handler = make_handler(frontend)
    try:
        server = _Server((args.host, args.port), handler)
    except OSError as exc:
        print(f"cannot bind {args.host}:{args.port}: {exc}", file=sys.stderr)
        channel.stop()
        return 5

    if args.ready_file:
        # The receipt is written by the supervisor, after both identity verification and a real
        # generation have succeeded. Writing it here would repeat the mistake this replaced.
        Path(args.ready_file).write_text(json.dumps({"ready": True, "pid": os.getpid()}))
    print(
        f"local engine listening on {args.host}:{args.port} "
        f"(ctx={status['context_tokens']}, slots={status['sequences']})",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()
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
