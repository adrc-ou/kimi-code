"""Tests for the agent-side frontend forwarder: a byte pump that must never weaken the bridge."""

import contextlib
import importlib.util
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "comfy_ui_forwarder", ROOT / "scripts" / "comfy_ui_forwarder.py"
)
assert SPEC and SPEC.loader
FWD = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(FWD)

OPENSSL = shutil.which("openssl")
needs_openssl = unittest.skipUnless(OPENSSL, "a throwaway upstream certificate needs openssl")


def make_certificate(directory: Path, common_name: str = "localhost"):
    cert = directory / f"{common_name}.crt"
    key = directory / f"{common_name}.key"
    subprocess.run(
        [
            OPENSSL, "req", "-x509", "-newkey", "rsa:2048", "-sha256", "-nodes", "-days", "1",
            "-keyout", str(key), "-out", str(cert), "-subj", f"/CN={common_name}",
            "-addext", f"subjectAltName=DNS:{common_name}",
        ],
        check=True,
        capture_output=True,
    )
    return cert, key


class TLSEchoServer:
    """A stand-in for the bridge: TLS that the peer must verify, echoing every byte it receives."""

    def __init__(self, cert, key):
        self.context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.context.load_cert_chain(cert, key)
        self.received = []
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(8)
        self.port = self.listener.getsockname()[1]
        self.stopping = threading.Event()
        self.thread = threading.Thread(target=self.accept_forever, name="stand-in-accept", daemon=True)
        self.thread.start()

    def accept_forever(self):
        self.listener.settimeout(0.1)
        while not self.stopping.is_set():
            try:
                raw, _ = self.listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return  # closed by tearDown
            threading.Thread(
                target=self.answer, args=(raw,), name="stand-in-answer", daemon=True
            ).start()

    def answer(self, raw):
        try:
            connection = self.context.wrap_socket(raw, server_side=True)
        except OSError:
            raw.close()
            return
        try:
            with connection:
                # Longer than the pump's idle bound and the reader's window below, so a
                # stand-in is never the first thing to give up on a live stream.
                connection.settimeout(120)
                # An unsolicited greeting first: the pump must forward what the upstream offers
                # without waiting to be asked, which is how a WebSocket carries events.
                connection.sendall(b"greeting;")
                while chunk := connection.recv(65536):
                    self.received.append(chunk)
                    connection.sendall(chunk)
        except OSError:
            pass

    def close(self):
        self.stopping.set()
        with contextlib.suppress(OSError):
            self.listener.close()


def read_until(sock: socket.socket, predicate, timeout: float = 15.0) -> bytes:
    """Accumulate whatever arrives until ``predicate`` is satisfied, the peer hangs up, or time is."""
    sock.settimeout(timeout)
    collected = b""
    try:
        while not predicate(collected):
            chunk = sock.recv(4096)
            if not chunk:
                break
            collected += chunk
    except OSError:
        pass
    return collected



class RecordedForwarder(FWD.UIForwarder):
    """A forwarder that keeps the notes it prints, so a closed connection can say why it closed."""

    def __init__(self, options):
        self.notes = []
        super().__init__(options)

    def note(self, message):
        self.notes.append(message)
        super().note(message)


def start_forwarder(**overrides):
    """One forwarder on the limits the image ships with, addressed at the stand-in bridge.

    The limits come from ``parse_args`` rather than from this file: a harness that invents its
    own numbers tests a service no deployment runs. The addressing is overridden here, along
    with an idle bound short enough that a stall fails the run instead of wedging it.
    """
    options = FWD.parse_args(["--tls-ca", str(overrides.pop("tls_ca", ""))])
    options.listen = "127.0.0.1"
    options.port = 0
    options.upstream_host = "127.0.0.1"
    options.tls_server_name = "localhost"
    options.idle_timeout = 90.0
    for name, value in overrides.items():
        setattr(options, name, value)
    server = RecordedForwarder(options)
    server.start()
    return server


@needs_openssl
class ForwarderConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.directory, True)
        self.ca, _ = make_certificate(self.directory)

    def test_a_missing_authority_stops_the_service_rather_than_disabling_verification(self):
        with self.assertRaises(SystemExit):
            FWD.parse_args(["--tls-ca", str(self.directory / "absent.crt")])

    def test_an_empty_authority_is_equally_fatal(self):
        blank = self.directory / "blank.crt"
        blank.write_bytes(b"")
        with self.assertRaises(SystemExit):
            FWD.parse_args(["--tls-ca", str(blank)])

    def test_the_listener_is_loopback_unless_a_private_network_is_asked_for(self):
        self.assertEqual(FWD.parse_args(["--tls-ca", str(self.ca)]).listen, "127.0.0.1")

    def test_the_expected_server_name_follows_the_upstream_by_default(self):
        options = FWD.parse_args(["--tls-ca", str(self.ca), "--upstream-host", "bridge.internal"])
        self.assertEqual(options.tls_server_name, "bridge.internal")

    def test_limits_cannot_be_zeroed_into_an_unbounded_or_dead_listener(self):
        for flag in ("--max-connections", "--connect-timeout", "--idle-timeout", "--port"):
            with self.subTest(flag=flag), self.assertRaises(SystemExit):
                FWD.parse_args(["--tls-ca", str(self.ca), flag, "0"])


@needs_openssl
class ForwarderPumpTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.directory, True)
        self.ca, self.key = make_certificate(self.directory)
        self.upstream = TLSEchoServer(self.ca, self.key)
        self.addCleanup(self.upstream.close)

    def start_one(self, **overrides):
        """One forwarder wired to the stand-in bridge, and the address its browsers dial."""
        options = {"upstream_port": self.upstream.port, "tls_ca": str(self.ca)}
        options.update(overrides)
        server = start_forwarder(**options)
        self.server = server
        # Added before any client's own close so teardown stops the forwarder last: a client that
        # hangs up first is the ordinary case, and the reverse order would mask it.
        self.addCleanup(server.stop)
        self.origin = ("127.0.0.1", server.address[1])
        return self.origin

    def dial(self, origin=None):
        """One browser-side connection, from a test that wants more than one per forwarder."""
        client = socket.create_connection(origin or self.origin, timeout=15)
        self.addCleanup(client.close)
        return client

    def open_client(self, **overrides):
        """One plain-HTTP-side client of a forwarder wired to the stand-in bridge."""
        origin = self.start_one(**overrides)
        return origin[1], self.dial(origin)

    def test_bytes_reach_the_upstream_and_the_reply_comes_back_intact(self):
        _, client = self.open_client()
        request = b"GET /prompt HTTP/1.1\r\nHost: comfyui-ui:8188\r\n\r\n"
        client.sendall(request)
        arrived = read_until(client, lambda value: value == b"greeting;" + request)
        self.assertEqual(arrived, b"greeting;" + request)
        self.assertEqual(b"".join(self.upstream.received), request)

    def test_a_large_payload_arrives_without_loss_or_reordering(self):
        payload = bytes(index % 251 for index in range(512 * 1024))
        expected = b"greeting;" + payload
        origin = self.start_one()
        # A race is not caught by one draw of it, and this body is the one that used to come back
        # short: repeated here so a pump that splits a connection across threads has three chances
        # to show it rather than one.
        for attempt in range(1, 4):
            client = self.dial(origin)
            client.sendall(payload)
            arrived, why = b"", []
            client.settimeout(60)
            while len(arrived) < len(expected):
                try:
                    chunk = client.recv(4096)
                except OSError as exc:
                    why.append(f"recv raised {type(exc).__name__} errno={exc.errno}")
                    break
                if not chunk:
                    why.append("recv hit EOF")
                    break
                arrived += chunk
            why.append(f"attempt {attempt} of 3")
            why.append(f"upstream echoed {sum(len(c) for c in self.upstream.received)} bytes so far")
            why.append(f"forwarder notes={self.server.notes}")
            diagnostic = "; ".join(why)
            self.assertEqual(len(arrived), len(expected), diagnostic)
            self.assertEqual(arrived, expected, diagnostic)
            client.close()

    def test_a_client_that_stops_writing_still_gets_the_whole_reply(self):
        # A browser that finishes its request is done *writing*, not done reading. Ending that
        # half of the pump used to shut down the TLS hop, which discards the session in both
        # directions and takes the reply that is still in flight down with it.
        request = b"GET /graph HTTP/1.1\r\nHost: comfyui-ui:8188\r\n\r\n"
        _, client = self.open_client()
        client.sendall(request)
        client.shutdown(socket.SHUT_WR)
        arrived = read_until(client, lambda value: value == b"greeting;" + request)
        self.assertEqual(arrived, b"greeting;" + request)
        self.assertEqual(b"".join(self.upstream.received), request)

    def test_carrying_three_conversations_spawns_no_thread_of_its_own(self):
        # The pump used to have a thread per direction, and that is what truncated large replies: an
        # ssl.SSLSocket has one record layer shared by its reader and its writer, so two threads
        # split by direction are not one direction each, and the reader starts reporting an
        # end-of-stream its peer never sent. One loop now owns a whole connection, so extra
        # conversations cost no extra threads - a fact about the process rather than a race to
        # lose, which is what makes it worth pinning down.
        origin = self.start_one()
        before = self.forwarder_threads()
        clients = [self.dial(origin) for _ in range(3)]
        for client in clients:
            client.sendall(b"GET /graph HTTP/1.1\r\nHost: comfyui-ui:8188\r\n\r\n")
            # The greeting is only legible once this connection is actually being pumped, so what
            # follows measures three live conversations and not merely three open sockets.
            arrived = read_until(client, lambda value: value.startswith(b"greeting;"))
            self.assertTrue(arrived.startswith(b"greeting;"), "the conversation never started")
        added = self.forwarder_threads() - before
        self.assertEqual(
            added,
            set(),
            f"three conversations added {len(added)} threads: a thread per direction is what "
            "made a reader see an end-of-stream that never happened",
        )

    def forwarder_threads(self):
        """Ids of the live threads this forwarder owns, excluding the fixture's own stand-in."""
        return {
            thread.ident
            for thread in threading.enumerate()
            if thread is not threading.main_thread() and not thread.name.startswith("stand-in")
        }

    def test_an_unrelated_authority_is_refused_and_nothing_reaches_the_upstream(self):
        foreign = self.directory / "foreign"
        foreign.mkdir()
        other_ca, _ = make_certificate(foreign, common_name="localhost")
        _, client = self.open_client(tls_ca=str(other_ca))
        client.sendall(b"GET / HTTP/1.1\r\n\r\n")
        # Same name, different CA: exactly a foreign certificate. The pump must fail closed, and
        # the browser must not end up talking to the bridge over an unverified connection.
        self.assertEqual(read_until(client, lambda value: value), b"")
        self.assertEqual(self.upstream.received, [])

    def test_the_listener_refuses_more_connections_than_it_has_slots(self):
        port, first = self.open_client(max_connections=1)
        second = socket.create_connection(("127.0.0.1", port), timeout=15)
        self.addCleanup(second.close)
        # Which of the two a single free slot lands on is the accept loop's affair, not the
        # client's: both hand their bytes to a pump thread and race for the slot there. What the
        # limit promises, and what a healthcheck depends on, is that exactly one is served and
        # the other is closed at once rather than queued behind it.
        served = []
        refused = []
        for name, client in (("first", first), ("second", second)):
            client.sendall(b"probe-" + name.encode())
            arrived = read_until(client, lambda value: b"greeting;" in value)
            (served if arrived.startswith(b"greeting;") else refused).append(name)
        self.assertEqual(len(served), 1, "one slot served two connections, or neither")
        self.assertEqual(len(refused), 1, "the spare connection was refused without a queue")


if __name__ == "__main__":
    unittest.main()
