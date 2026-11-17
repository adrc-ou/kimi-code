"""The MLX frontend's channel, driven as a real process pair.

``tools/local_model_server.py`` had no executed test coverage at all before this file: the only
references to it were regex matches over its source text. That is how a transport bug that hung
19 times out of 19 attempts shipped inside the change whose purpose was to stop hangs.

Everything here runs the real ``WorkerChannel`` against real OS pipes with a synthetic worker
written in stdlib Python. No MLX, no Metal, no GPU — the questions are about framing, buffers and
process lifetimes, and they have the same answers on any platform.

The transport cases loop rather than running once. A single attempt can pass by scheduler luck: if
the worker's second write happens to land after the reader has already parked, the stranded-buffer
bug never surfaces. Measured during the audit, one trial in five hid a defect that reproduced on
every trial when the writes were back to back.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
for _directory in (
    ROOT,
    ROOT / "tools",
    ROOT / "tools" / "local_engines",
    ROOT / "proxy",
):
    if str(_directory) not in sys.path:
        sys.path.insert(0, str(_directory))

import local_model_server as server  # noqa: E402

#: Enough trials that a race with a ~20% hide rate fails to pass by luck.
TRIALS = 25

#: A completed generation over a local pipe takes milliseconds, so this is a wide margin
#: whose only purpose is to turn a regression into a named failure instead of a hang.
GENERATION_TRIAL_SECONDS = 8.0

PLAN: dict[str, Any] = {"model_alias": "fixture", "protocol_version": 1}

#: A worker that speaks the real line-delimited protocol but does no device work. Each case is a
#: tiny script so the test controls exactly when bytes reach the pipe.
WORKERS = {
    # begin/token/end in a single os.write: the shape that stranded the terminal line in the
    # reader's buffer while select() reported the pipe empty.
    "coalesced": """
        import os
        os.write(1, ('{"kind":"ready","status":' + STATUS + '}\\n').encode())
        request = sys.stdin.readline()
        os.write(1, (
            '{"kind":"begin","sequence":1}\\n'
            '{"kind":"token","sequence":1,"text":"he"}\\n'
            '{"kind":"end","sequence":1,"content_chars":2,'
            '"finish_reason":"stop","generation_tokens":2,"peak_memory_bytes":null}\\n'
        ).encode())
        """,
    # Same documents, one syscall each — what the real worker's flush-per-line _write() does. The
    # reader still coalesces them on its side, so this must pass too.
    "flushed": """
        def emit(document):
            sys.stdout.write(json.dumps(document) + "\\n")
            sys.stdout.flush()
        emit({"kind": "ready", "status": json.loads(STATUS)})
        sys.stdin.readline()
        emit({"kind": "begin", "sequence": 1})
        emit({"kind": "token", "sequence": 1, "text": "hello"})
        emit({"kind": "end", "sequence": 1, "content_chars": 5,
              "finish_reason": "stop", "generation_tokens": 1, "peak_memory_bytes": None})
        """,
    # A document split across two writes inside one JSON line: framing must survive it.
    "fragmented": """
        import os
        os.write(1, ('{"kind":"ready","status":' + STATUS + '}\\n').encode())
        sys.stdin.readline()
        document = json.dumps({"kind": "end", "sequence": 1, "content_chars": 3,
                               "finish_reason": "stop", "generation_tokens": 1,
                               "peak_memory_bytes": None}) + "\\n"
        half = len(document) // 2
        os.write(1, document[:half].encode())
        os.write(1, document[half:].encode())
        """,
    "dies_after_begin": """
        import os
        os.write(1, ('{"kind":"ready","status":' + STATUS + '}\\n').encode())
        sys.stdin.readline()
        os.write(1, b'{"kind":"begin","sequence":1}\\n')
        raise SystemExit(7)
        """,
    "exits_immediately": """
        raise SystemExit(3)
        """,
    "garbage": """
        import os
        os.write(1, ('{"kind":"ready","status":' + STATUS + '}\\n').encode())
        sys.stdin.readline()
        os.write(1, b'not json at all\\n')
        """,
    "fatal": """
        import os
        os.write(1, ('{"kind":"ready","status":' + STATUS + '}\\n').encode())
        sys.stdin.readline()
        os.write(1, b'{"kind":"fatal","reason":"metal could not allocate"}\\n')
        """,
    # Answers any request, so a control call can be shown to round-trip and an admissible
    # generation can be shown *not* to be refused.
    "echoes": """
        sys.stdout.write(json.dumps({"kind": "ready", "status": json.loads(STATUS)}) + "\\n")
        sys.stdout.flush()
        for line in sys.stdin:
            request = json.loads(line)
            action = request["action"]
            sequence = request["sequence"]
            def say(document):
                sys.stdout.write(json.dumps(document) + "\\n")
                sys.stdout.flush()
            if action == "input_tokens":
                say({"kind": "input_tokens", "sequence": sequence,
                     "count": {"input_tokens": 7}})
            elif action == "generate":
                say({"kind": "begin", "sequence": sequence})
                say({"kind": "end", "sequence": sequence, "content_chars": 2,
                     "finish_reason": "stop", "generation_tokens": 1,
                     "peak_memory_bytes": None})
            else:
                say({"kind": action, "sequence": sequence})
        """,
}

READY_STATUS = json.dumps(
    {
        "engine_kind": "mlx_lm",
        "model": "fixture",
        "model_path": "/fixture",
        "protocol_version": 1,
        "ready": True,
        "context_tokens": 4096,
        "sequences": 1,
        "total_context_tokens": 4096,
        "fingerprint": "f" * 64,
        "launch_identity": "s" * 32,
        "runtime_identity": "r" * 64,
        "serving_profile_id": "p" * 64,
        "tokenizer_identity": "t" * 64,
        "template_identity": "",
        "artifact_format": "mlx",
        "cache_allocation": "growing",
        "attested": [],
        "declared": [],
    }
)


class ChannelFixture(unittest.TestCase):
    """Builds a real WorkerChannel over real pipes."""

    def channel(self, worker: str, **overrides: Any) -> server.WorkerChannel:
        source = textwrap.dedent(WORKERS[worker]).strip()
        script = self.scratch / f"{worker}.py"
        # STATUS is interpolated rather than embedded so each worker stays readable.
        preamble = (
            "import json, sys\n"
            # STATUS arrives as a JSON string and is parsed here, matching the other fixtures.
            # Embedding READY_STATUS directly produced a NameError on `true`, and passing it
            # through json.dumps() made the status a *string*, which the frontend rejected at
            # validate_status_document — both silently, because the worker died before any
            # signal was sent and the case still reported success.
            f"STATUS = {json.dumps(READY_STATUS)}\n"
        )
        script.write_text(preamble + source + "\n")
        argv = [sys.executable, str(script)]
        plan = dict(PLAN)
        plan.update(overrides)
        channel = server.WorkerChannel(argv, dict(os.environ), plan)
        self.addCleanup(channel.stop)
        return channel

    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.scratch = Path(self._temporary.name)


class TransportTests(ChannelFixture):
    def drive_generate(self, worker: str) -> tuple[list[dict], Exception | None]:
        """Consume one generation, refusing rather than hanging.

        The bug this file exists for is an infinite loop, so the harness must not be capable of one
        either: without the wall-clock cap a regression here would hang the suite until CI killed
        it, and a killed run does not say which case hung or why.
        """
        channel = self.channel(worker)
        channel.await_ready(timeout=30.0)
        events: list[dict] = []
        failure: Exception | None = None
        deadline = time.monotonic() + GENERATION_TRIAL_SECONDS
        try:
            for event in channel.stream({"messages": [{"role": "user", "content": "x"}]}):
                events.append(event)
                if event["kind"] in {"end", "refused"}:
                    break
                if time.monotonic() > deadline:
                    self.fail(
                        f"{worker}: no terminal document within {GENERATION_TRIAL_SECONDS:.0f}s; "
                        f"kinds so far {[entry['kind'] for entry in events]}"
                    )
        except server.WorkerGone as exc:
            failure = exc
        return events, failure

    def test_the_terminal_document_arrives_when_writes_are_coalesced(self):
        """F7: select() could not see a line the buffered reader had already fetched."""
        for attempt in range(TRIALS):
            with self.subTest(trial=attempt):
                events, failure = self.drive_generate("coalesced")
                self.assertIsNone(failure)
                kinds = [event["kind"] for event in events]
                self.assertIn("end", kinds, f"terminal document lost after {kinds}")
                self.assertNotIn(
                    "keepalive", kinds, "keepalives were emitted while the answer sat buffered"
                )

    def test_the_terminal_document_arrives_when_writes_are_flushed_per_line(self):
        for attempt in range(TRIALS):
            with self.subTest(trial=attempt):
                events, failure = self.drive_generate("flushed")
                self.assertIsNone(failure)
                self.assertIn("end", [event["kind"] for event in events])

    def test_a_document_split_across_two_writes_is_reframed(self):
        events, failure = self.drive_generate("fragmented")
        self.assertIsNone(failure, failure)
        self.assertEqual([event["kind"] for event in events], ["end"], events)
        self.assertEqual(events[0]["finish_reason"], "stop")

    def test_finish_reason_reaches_the_caller(self):
        events, _failure = self.drive_generate("coalesced")
        end = [event for event in events if event["kind"] == "end"][0]
        self.assertEqual(end["finish_reason"], "stop", end)

    def test_worker_death_mid_stream_is_reported_not_hung(self):
        events, failure = self.drive_generate("dies_after_begin")
        self.assertIsNotNone(failure, f"expected WorkerGone, got {events}")
        self.assertIn("exited", str(failure).lower(), failure)

    def test_an_unparseable_line_ends_the_channel(self):
        _events, failure = self.drive_generate("garbage")
        self.assertIsNotNone(failure, "unparseable framing must be terminal")

    def test_a_fatal_document_latches_the_channel(self):
        events, failure = self.drive_generate("fatal")
        self.assertIsNotNone(failure)
        self.assertIn("metal could not allocate", str(failure))
        # And it stays latched: a later call must not pretend the worker is there.
        channel = self.channel("fatal")
        channel.await_ready(timeout=30.0)
        with self.assertRaises(server.WorkerGone):
            channel.call("status")

    def test_a_worker_that_exits_never_blocks_the_reader_forever(self):
        channel = self.channel("exits_immediately")
        started = time.monotonic()
        with self.assertRaises(server.WorkerGone):
            channel.await_ready(timeout=20.0)
        self.assertLess(time.monotonic() - started, 15.0, "await_ready ignored the exit")


class ControlCallTests(ChannelFixture):
    def test_a_control_call_round_trips(self):
        channel = self.channel("echoes")
        channel.await_ready(timeout=30.0)
        answer = channel.call("input_tokens", {"messages": []})
        self.assertEqual(answer["count"], {"input_tokens": 7})

class LaunchArgumentTests(unittest.TestCase):
    """F3: the adapter emitted ``--worker-arg --model-dir``, which argparse rejects outright.

    Asserted by calling the real parsers rather than comparing flag names in source text, which is
    the check that existed before and passed while the command could not start at all.
    """

    def test_generated_frontend_argv_parses_and_the_worker_argv_parses(self):
        from local_engines import mlx_lm as adapter_module
        from local_engines.base import Artifact, Runtime

        root = self.enterContext(__import__("tempfile").TemporaryDirectory())
        primary = Path(root) / "model-00001.safetensors"
        primary.write_bytes(b"x")
        runtime = Runtime(
            kind="mlx_lm",
            identity="r" * 64,
            directory=Path(root),
            executable=Path(root) / "environment" / "bin" / "python3",
            details={"site_packages": str(Path(root) / "environment" / "site-packages")},
        )
        artifact = Artifact(
            format="mlx",
            variant="4bit",
            primary=primary,
            paths=(primary,),
            tensor_bytes=1,
            total_bytes=1,
        )
        adapter = adapter_module.MlxLmAdapter()

        plan_root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        env = {"HARNESS_RUNTIME_DIR": str(plan_root)}
        import unittest.mock as mock

        with mock.patch.dict(os.environ, env, clear=False):
            launch = adapter.launch(
                {"id": "local_mps/x", "model": "x", "where": "models/local_mps/x",
                 "install": {"sampling": {}}, "native_tokens": 4096},
                artifact,
                runtime,
                {"context_tokens": 4096, "sequences": 1, "total_context_tokens": 4096,
                 "fingerprint": "f" * 64, "snapshot_id": "s" * 32,
                 "serving_profile_id": "p" * 64, "runtime_identity": "r" * 64,
                 "allocation": {}, "wiring": {"wired_bytes": 1234}},
                "token-value",
            )
        frontend = server.parse_args(list(launch.argv[2:]))
        self.assertEqual(frontend.worker_arg, [str(_worker_script())])
        # What WorkerChannel then spawns: the interpreter plus that script, then the
        # descriptor it hands the plan document over.
        worker = mlx_worker_parse([])
        self.assertEqual(worker.plan_fd, 3)

    def test_the_launch_environment_puts_the_provisioned_tree_on_pythonpath(self):
        """F3: dependencies install to environment/site-packages, which is not a default path."""
        from local_engines.mlx_lm import _sanitized_environment

        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        site = str(root / "environment" / "site-packages")
        environment = _sanitized_environment({"site_packages": site})
        self.assertEqual(environment["PYTHONPATH"], site)

    def test_a_runtime_that_records_no_site_packages_is_refused_not_launched_unconfigured(self):
        from local_engines.base import EngineAdapterError
        from local_engines.mlx_lm import _sanitized_environment

        with self.assertRaises(EngineAdapterError):
            _sanitized_environment({})


def _worker_script() -> Path:
    from local_engines.mlx_lm import _worker_path

    return _worker_path()


def mlx_worker_parse(argv: list[str]):
    """Parse what the worker will actually receive once WorkerChannel appends the descriptor."""
    import mlx_worker

    return mlx_worker.parse_args([*argv, "--plan-fd", "3"])


class RefusalStatusTests(unittest.TestCase):
    """F10b: a client's inadmissible request must not arrive as engine damage.

    The proxy classifies an in-band SSE error object as fatal and latches the lane. If a refusal is
    only expressible after HTTP 200 has been sent, one unsupported field from one client disables a
    healthy engine for the rest of the session. These assert the answer is a 4xx with no stream.
    """

    def _post(self, body: dict):
        import http.client

        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        self.addCleanup(connection.close)
        connection.request(
            "POST",
            "/v1/chat/completions",
            body=json.dumps(body),
            headers={"Authorization": "Bearer " + "t" * 48,
                     "Content-Type": "application/json"},
        )
        response = connection.getresponse()
        payload = response.read()
        return response.status, payload

    def setUp(self):
        import tempfile

        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.scratch = Path(self._temporary.name)
        self.channel = self.channel_for("echoes")
        self.channel.await_ready(timeout=30.0)
        self.port = self.serve(self.channel)

    def channel_for(self, worker: str):
        script = self.scratch / f"{worker}.py"
        script.write_text(
            f"import json, sys\nSTATUS = {json.dumps(READY_STATUS)}\n"
            + textwrap.dedent(WORKERS[worker]).strip()
            + "\n"
        )
        channel = server.WorkerChannel(
            [sys.executable, str(script)], dict(os.environ), dict(PLAN)
        )
        self.addCleanup(channel.stop)
        return channel

    def serve(self, channel) -> int:
        status = {"context_tokens": 4096, "sequences": 1, "ready": True}
        frontend = server.Frontend(channel, "t" * 48, {"protocol_version": 1}, status)
        port = _free_port()
        httpd = server._Server(("127.0.0.1", port), server.make_handler(frontend))
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(lambda: (httpd.shutdown(), httpd.server_close()))
        return port

    def test_an_unsupported_field_is_a_400_before_any_stream_state(self):
        status, payload = self._post(
            {"messages": [{"role": "user", "content": "x"}], "tool_choice": "required"}
        )
        self.assertEqual(status, 400, payload[:200])
        self.assertNotIn(b"text/event-stream", payload, "a refusal must not open an SSE stream")
        body = json.loads(payload)
        self.assertIn("tool_choice", json.dumps(body))

    def test_an_admissible_request_is_not_refused(self):
        status, payload = self._post(
            {"messages": [{"role": "user", "content": "x"}], "top_k": 5}
        )
        self.assertNotEqual(status, 400, payload[:200])

    def test_the_worker_and_the_frontend_share_one_field_policy(self):
        """The point of putting the set in the shared contract: they cannot disagree.

        Checked behaviourally rather than by object identity, because an identity assert would pass
        on an unused import while the worker still refused on a private list of its own.
        """
        import mlx_worker

        import local_engine_contract as contract

        self.assertFalse(
            [
                name
                for name in vars(mlx_worker)
                if name.endswith("ACCEPTED_REQUEST_FIELDS")
            ],
            "the worker grew its own copy of the accept list again",
        )
        # Anything the frontend 400s the worker must also refuse; otherwise a request slips past
        # one gate and the two layers disagree about what was honoured.
        for field in ("tool_choice", "seed", "presence_penalty"):
            with self.subTest(field=field):
                self.assertIn(field, contract.unsupported_request_fields({field: "x"}))
                with self.assertRaises(mlx_worker.WorkerRefused):
                    mlx_worker.check_request(
                        {"messages": [{"role": "user", "content": "x"}], field: "x"}
                    )


class ShutdownTests(unittest.TestCase):
    """P2e: a killed frontend must not leave its executor holding the device.

    These use real child PIDs rather than a stub supervisor, because the question is what the
    kernel does with a process tree, and a mocked ``stop()`` only answers what the code intends.

    Scope note: a handler can only run for a signal the process is allowed to catch. SIGKILL is
    not one of them, so nothing asserted here claims otherwise — the supervisor's own signal
    forwarding (``cmd_serve``) is what keeps an ordinary shutdown off that path.
    """

    def _spawn(self, directory: str):
        """Start the real frontend with a worker that records its pid and then lives forever."""
        scratch = Path(directory)
        pidfile = scratch / "worker.pid"
        worker = scratch / "resident_worker.py"
        worker.write_text(
            "import json, os, sys\n"
            f"STATUS = {json.dumps(READY_STATUS)}\n"
            f"open({str(pidfile)!r}, 'w').write(str(os.getpid()))\n"
            "sys.stdout.write(json.dumps({'kind': 'ready',\n"
            "                                        'status': json.loads(STATUS)}) + chr(10))\n"
            "sys.stdout.flush()\n"
            "sys.stdin.readline()\n"
            "import time\n"
            "time.sleep(600)\n"
        )
        entry = scratch / "run_frontend.py"
        entry.write_text(
            "import sys\n"
            "from local_model_server import main\n"
            "raise SystemExit(main())\n"
        )
        plan = scratch / "plan.json"
        plan.write_text(json.dumps(PLAN))
        # The frontend sets allow_reuse_address = False, so a port this helper probed and released
        # can still be held by a sibling test's lingering listener, and a bind failure looks
        # exactly like an unrelated early exit. Bounded retries keep a collision from reading as a
        # regression in the behaviour under test.
        for attempt in range(4):
            _process, ready = self._launch(str(entry), str(worker), str(plan))
            if ready.exists():
                return _process, int(ready.read_text())
            if attempt == 3:
                self.fail(
                    f"frontend never brought its worker up (exit={_process.returncode}): "
                    "the retry budget on a port collision is too small"
                )
            time.sleep(0.25)
        raise AssertionError("unreachable")

    def _launch(self, entry: str, worker: str, plan: str):
        scratch = Path(entry).parent
        pidfile = scratch / "worker.pid"
        process = subprocess.Popen(  # noqa: S603 - argv is this test's own fixture
            [
                sys.executable,
                entry,
                "--host",
                "127.0.0.1",
                "--port",
                str(_free_port()),
                "--worker",
                sys.executable,
                "--worker-arg",
                worker,
                "--plan-file",
                plan,
            ],
            env={
                **os.environ,
                "PYTHONPATH": os.pathsep.join(
                    str(ROOT / name) for name in ("tools", "proxy")
                ),
                # The frontend refuses to start without a session credential of its own; the
                # descriptor path is exercised elsewhere, and this pair is about process lifetime.
                "LOCAL_MPS_TOKEN": "f" * 48,
            },
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        # Tolerant by design: a bind collision is a retryable condition here and the caller
        # decides whether a failure is real, so _launch only reports what it observed.
        deadline = time.monotonic() + 25.0
        while not pidfile.exists():
            if time.monotonic() > deadline or process.poll() is not None:
                break
            time.sleep(0.05)
        if pidfile.exists():
            self.addCleanup(self._reap, int(pidfile.read_text()))
        return process, pidfile

    @staticmethod
    def _reap(pid: int) -> None:
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            return

    @staticmethod
    def _alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except (ProcessLookupError, PermissionError):
            return False
        return True

    def _wait_gone(self, pid: int, seconds: float = 10.0) -> bool:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if not self._alive(pid):
                return True
            time.sleep(0.05)
        return False

    def assert_signal_takes_the_worker(self, signum: int) -> None:
        with tempfile.TemporaryDirectory() as directory:
            process, worker_pid = self._spawn(directory)
            self.assertTrue(
                self._alive(worker_pid),
                "the worker was not running before the signal, so this case could not have "
                "shown anything — an earlier version of this test passed precisely that way",
            )
            self.assertIsNone(process.poll(), "the frontend had already exited")
            process.send_signal(signum)
            try:
                process.wait(timeout=25)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
            self.assertTrue(
                self._wait_gone(worker_pid),
                f"{signum}: the frontend died and left the executor alive holding the model",
            )

    def test_sigterm_to_the_frontend_takes_the_worker_with_it(self):
        self.assert_signal_takes_the_worker(signal.SIGTERM)

    def test_sighup_to_the_frontend_takes_the_worker_with_it(self):
        self.assert_signal_takes_the_worker(signal.SIGHUP)


def _free_port() -> int:
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


class CancellationTests(ChannelFixture):
    """S12a: a cancellation has to reach a worker that is busy, not queue behind its own work.

    The first implementation put cancel on the command pipe, where it could only be read after the
    generation finished — i.e. exactly when it was worthless. These assert the byte channel, and
    that a stop is reported as a cancellation rather than as engine damage.
    """

    def test_a_byte_on_the_cancel_channel_reaches_the_worker(self):
        marker = self.scratch / "saw-cancel"
        marker.parent.mkdir(parents=True, exist_ok=True)
        script = self.scratch / "cancel_watcher.py"
        script.write_text(
            f"import json, os, sys, time\nSTATUS = {json.dumps(READY_STATUS)}\n"
            "sys.stdout.write(json.dumps({'kind': 'ready', 'status': json.loads(STATUS)})"
            " + chr(10))\n"
            "sys.stdout.flush()\n"
            "control = os.fdopen(4, 'rb', closefd=True)\n"
            "deadline = time.time() + 20\n"
            "while time.time() < deadline:\n"
            "    if control.read(1):\n"
            f"        open({str(marker)!r}, 'w').write('cancelled')\n"
            "        raise SystemExit(0)\n"
            "raise SystemExit(1)\n"
        )
        channel = server.WorkerChannel(
            [sys.executable, str(script)], dict(os.environ), dict(PLAN)
        )
        self.addCleanup(channel.stop)
        channel.await_ready(timeout=30.0)
        channel.cancel()
        deadline = time.monotonic() + 20.0
        while not marker.exists() and time.monotonic() < deadline:
            if channel.process.poll() is not None:
                break
            time.sleep(0.05)
        self.assertTrue(
            marker.exists(),
            "the worker never saw the cancellation: fd 4 is not reaching it, or the parent is "
            "holding the read end open",
        )

    def test_cancelling_a_finished_worker_is_not_an_error(self):
        channel = self.channel("exits_immediately")
        with self.assertRaises(server.WorkerGone):
            channel.await_ready(timeout=20.0)
        channel.cancel()  # must swallow the dead-pipe error rather than raise from a handler


class DescriptorHygieneTests(ChannelFixture):
    """A stopped engine must give back every descriptor it opened.

    ``stop()`` used to close only stdin, so stdout (the stream a reader thread was parked in),
    stderr, and the cancel write end each leaked per teardown — and the leaked write end also meant
    the worker's blocking read could never see EOF.
    """

    @staticmethod
    def _open_fds() -> set[int]:
        return {
            int(name)
            for name in os.listdir("/proc/self/fd")
            if name.isdigit()  # noqa: SIM118 - listing the fd directory, not a mapping
        }

    def test_repeated_engine_teardown_does_not_accumulate_descriptors(self):
        baseline = len(self._open_fds())
        for _ in range(6):
            channel = self.channel("echoes")
            channel.await_ready(timeout=30.0)
            channel.call("input_tokens", {"messages": []})
            channel.stop()
        # One per channel is tolerated for the popen bookkeeping subprocess object itself; a leak
        # here is three or more per iteration and grows without bound.
        self.assertLessEqual(
            len(self._open_fds()) - baseline,
            2,
            f"descriptors grew from {baseline} to {len(self._open_fds())} across 6 teardowns",
        )


class CountingIndependenceTests(ChannelFixture):
    """S12b: an exact count must not wait for someone else's generation.

    This is the whole reason for the separate process. The worker reads one command at a time, so a
    count sent down the same channel is answered only after the prefill ahead of it finishes — and
    the proxy bounds that wait with LOCAL_PREPARATION_TIMEOUT, turning the normal case on a
    single-slot engine into a 503 that re-queues behind the very prefill that starved it.
    """

    def script(self, name: str, body: str) -> Path:
        path = self.scratch / name
        path.write_text(
            f"import json, os, sys, time\nSTATUS = {json.dumps(READY_STATUS)}\n"
            + textwrap.dedent(body).strip()
            + "\n"
        )
        return path

    def test_a_count_is_answered_while_a_generation_is_still_running(self):
        # The worker takes the generate, emits one token, and then never finishes: no end
        # document, no exit. It is holding the channel lock for the whole test.
        worker = self.script(
            "slow_worker.py",
            """
            def say(document):
                sys.stdout.write(json.dumps(document) + chr(10))
                sys.stdout.flush()

            say({"kind": "ready", "status": json.loads(STATUS)})
            request = json.loads(sys.stdin.readline())
            say({"kind": "begin", "sequence": request["sequence"]})
            say({"kind": "token", "sequence": request["sequence"], "text": "first"})
            open(os.environ["SLOW_MARK"], "w").write("generating")
            time.sleep(600)
            """,
        )
        counter = self.script(
            "counter.py",
            """
            request = json.loads(sys.stdin.readline())
            sys.stdout.write(json.dumps({
                "kind": "input_tokens",
                "sequence": request["sequence"],
                "count": {"input_tokens": 123},
            }) + chr(10))
            sys.stdout.flush()
            """,
        )
        mark = self.scratch / "worker-generating"
        env = {**os.environ, "SLOW_MARK": str(mark)}
        channel = server.WorkerChannel([sys.executable, str(worker)], env, dict(PLAN))
        self.addCleanup(channel.stop)
        channel.await_ready(timeout=30.0)
        token_counter = server.TokenCounter(sys.executable, str(counter), "/fixture", env)
        self.addCleanup(token_counter.stop)
        self.assertTrue(token_counter.available, token_counter.failure)

        threading.Thread(
            target=lambda: list(channel.stream({"messages": [], "max_tokens": 8})),
            daemon=True,
        ).start()
        # Wait on the worker's own signal that it is mid-generation, then check the lock. Waiting on
        # the lock alone is a race in the other direction: the frontend takes it before the request
        # has even reached the child, and the assertion below would then fail on a healthy system.
        deadline = time.monotonic() + 20.0
        while (not mark.exists() or not channel._lock.locked()) and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(mark.exists(), "the worker never entered generation")
        self.assertTrue(
            channel._lock.locked(),
            "the generation never held the channel lock, so nothing was being blocked",
        )

        # The count returns while the lock above is demonstrably still held.
        started = time.monotonic()
        answer = token_counter.count(
            {"messages": [{"role": "user", "content": "x"}]}, timeout=10.0
        )
        self.assertEqual(answer["count"]["input_tokens"], 123, answer)
        self.assertLess(time.monotonic() - started, 5.0, "the count waited for the generation")
        self.assertTrue(
            channel._lock.locked(), "the generation ended early; the test proved nothing"
        )

    def test_a_dead_counter_is_reported_as_unavailable_rather_than_hanging(self):
        dead = self.script("dead_counter.py", "raise SystemExit(9)")
        counter = server.TokenCounter(sys.executable, str(dead), "/fixture", dict(os.environ))
        self.addCleanup(counter.stop)
        deadline = time.monotonic() + 10.0
        while counter.available and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(counter.available)
        self.assertIsNotNone(counter.failure)
        with self.assertRaises(server.WorkerGone):
            counter.count({"messages": []}, timeout=2.0)

    def test_a_missing_artifact_directory_is_refused_before_generation(self):
        worker = self.script(
            "echo2.py",
            """
            sys.stdout.write(json.dumps({"kind": "ready", "status": json.loads(STATUS)}) + chr(10))
            sys.stdout.flush()
            for line in sys.stdin:
                request = json.loads(line)
                reply = {"kind": request["action"], "sequence": request["sequence"]}
                if request["action"] == "input_tokens":
                    reply["count"] = {"input_tokens": 7}
                sys.stdout.write(json.dumps(reply) + chr(10))
                sys.stdout.flush()
            """,
        )
        channel = server.WorkerChannel(
            [sys.executable, str(worker)], dict(os.environ), dict(PLAN)
        )
        self.addCleanup(channel.stop)
        channel.await_ready(timeout=30.0)
        self.assertEqual(channel.call("input_tokens", {"messages": []})["count"]["input_tokens"], 7)

    def test_the_frontend_reports_counter_availability_in_its_snapshot(self):
        channel = self.channel("echoes")
        status = channel.await_ready(timeout=30.0)
        counter = server.TokenCounter(
            sys.executable,
            str(self.script("idle_counter.py", "time.sleep(60)")),
            "/fixture",
            dict(os.environ),
        )
        self.addCleanup(counter.stop)
        frontend = server.Frontend(channel, "t" * 48, {"protocol_version": 1}, status, counter)
        observations = frontend.snapshot()["observations"]
        self.assertTrue(observations["token_counter_available"], observations)
        self.assertEqual(observations["counts_served_by_engine"], 0, observations)
        frontend.note_count_fallback()
        self.assertEqual(
            frontend.snapshot()["observations"]["counts_served_by_engine"], 1
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
