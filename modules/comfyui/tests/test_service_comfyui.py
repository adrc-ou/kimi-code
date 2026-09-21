"""The shipped service hook, walked over the shipped bridge and the shipped forwarder.

`service_comfyui.py` runs inside the agent container at every health check and offers no seam of
its own, so the only honest test is to give it the real chain: the bridge's routes behind the
forwarder's pump, with a stub ComfyUI at the end of both.
"""

import argparse
import asyncio
import contextlib
import importlib.util
import os
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


try:
    BRIDGE = load("comfy_bridge", "scripts/comfy_bridge.py")
except ImportError as exc:
    raise unittest.SkipTest(
        f"the bridge cannot be imported without its dependencies ({exc}); install what "
        "proxy/requirements.in pins to run these tests"
    ) from None

from aiohttp import web  # noqa: E402  (the bridge above is what requires aiohttp)

FWD = load("comfy_ui_forwarder", "scripts/comfy_ui_forwarder.py")
COMFYCTL = load("comfyctl", "runtime/tools/comfyctl.py")
SERVICE = load("service_comfyui", "runtime/tools/service_comfyui.py")

TOKEN = "b" * 43
OPENSSL = shutil.which("openssl")


class StubComfyUI(BaseHTTPRequestHandler):
    """Enough of ComfyUI for a proxied read to be recognisable as somebody else's answer."""

    def log_message(self, _format, *_args):
        return

    def do_GET(self):
        body = b'{"system": {"name": "stub-comfyui"}}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@contextlib.contextmanager
def environment(**values):
    previous = {name: os.environ.get(name) for name in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


@contextlib.contextmanager
def comfyctl_importable():
    """The hook does `import comfyctl`, which is how it runs beside it inside the container."""
    had = "comfyctl" in sys.modules
    previous = sys.modules.get("comfyctl")
    sys.modules["comfyctl"] = COMFYCTL
    try:
        yield
    finally:
        if had:
            sys.modules["comfyctl"] = previous
        else:
            del sys.modules["comfyctl"]


def start_stub(test):
    server = ThreadingHTTPServer(("127.0.0.1", 0), StubComfyUI)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    test.addCleanup(lambda: (server.shutdown(), server.server_close()))
    return f"http://127.0.0.1:{server.server_address[1]}"


def unused_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


class ProbeTests(unittest.TestCase):
    def setUp(self):
        self.stub_origin = start_stub(self)
        self.directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.directory, True)

    def use_api_plane(self, origin, token, ca_file=""):
        """Point the shipped helper at one plane, as the agent's environment does."""
        COMFYCTL.BASE_URL = origin
        COMFYCTL.AUTH_TOKEN = token
        COMFYCTL.CA_FILE = ca_file

    def test_the_hook_walks_a_browser_the_only_way_a_browser_can_be_walked(self):
        bridge, cert = self.start_bridge(TOKEN)
        origin = self.start_forwarder(bridge, cert)
        self.use_api_plane(f"https://localhost:{bridge}", TOKEN, str(cert))
        with environment(
            COMFYUI_UI_URL=origin, COMFYUI_TOKEN=TOKEN, COMFYUI_CONNECT_TIMEOUT="10"
        ):
            with comfyctl_importable():
                detail = SERVICE.probe(False)
        self.assertIn("authenticated stats", detail)
        self.assertIn("frontend grant redeemed into a session that reaches ComfyUI", detail)

    def test_a_plane_that_asks_for_nothing_says_so(self):
        # The CUDA shape: no bridge in front of ComfyUI, so one origin serves both planes.
        self.use_api_plane(self.stub_origin, "")
        with environment(COMFYUI_UI_URL=self.stub_origin, COMFYUI_TOKEN=""):
            with comfyctl_importable():
                detail = SERVICE.probe(False)
        self.assertIn("frontend answers without a credential", detail)

    def test_a_frontend_that_never_answers_is_a_failure_the_monitor_can_report(self):
        bridge, cert = self.start_bridge(TOKEN)
        self.use_api_plane(f"https://localhost:{bridge}", TOKEN, str(cert))
        with environment(
            COMFYUI_UI_URL=f"http://localhost:{unused_port()}",
            COMFYUI_TOKEN=TOKEN,
            COMFYUI_CONNECT_TIMEOUT="2",
        ):
            with comfyctl_importable():
                with self.assertRaises(ValueError) as caught:
                    SERVICE.probe(False)
        self.assertIn("unreachable", str(caught.exception))

    def test_an_absent_origin_is_a_note_and_not_a_fault(self):
        with environment(COMFYUI_UI_URL=""):
            self.assertEqual(
                SERVICE.frontend_probe(COMFYCTL), "no frontend origin is configured"
            )

    def start_bridge(self, token):
        """The shipped application on a real TLS listener, on this test's own event loop."""
        if OPENSSL is None:
            self.skipTest("the bridge in this chain needs openssl to have a certificate")
        cert = self.directory / "bridge.crt"
        key = self.directory / "bridge.key"
        subprocess.run(
            [
                OPENSSL, "req", "-x509", "-newkey", "rsa:2048", "-sha256", "-nodes", "-days", "1",
                "-keyout", str(key), "-out", str(cert), "-subj", "/CN=localhost",
                "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1",
            ],
            check=True,
            capture_output=True,
        )
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, key)
        options = argparse.Namespace(
            max_body=1024 * 1024,
            max_ws_message=1024 * 1024,
            max_connections=4,
            upstream=self.stub_origin,
            grant_ttl=60,
            session_ttl=3600,
            max_grants=4,
            max_sessions=4,
        )
        self.loop = asyncio.new_event_loop()
        self.loop_thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.loop_thread.start()

        async def start():
            runner = web.AppRunner(BRIDGE.build_app(options, token))
            await runner.setup()
            await web.TCPSite(runner, "localhost", 0, ssl_context=context).start()
            return runner

        runner = self.await_on_loop(start())
        self.addCleanup(lambda: self.stop(runner))
        return runner.addresses[0][1], cert

    def await_on_loop(self, coroutine):
        return asyncio.run_coroutine_threadsafe(coroutine, self.loop).result(timeout=30)

    def stop(self, runner):
        self.await_on_loop(runner.cleanup())
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.loop_thread.join(timeout=30)
        self.loop.close()

    def start_forwarder(self, bridge_port, ca_file):
        server = FWD.UIForwarder(
            argparse.Namespace(
                listen="127.0.0.1",
                port=0,
                upstream_host="localhost",
                upstream_port=bridge_port,
                tls_ca=str(ca_file),
                tls_server_name="localhost",
                max_connections=4,
                connect_timeout=5.0,
                idle_timeout=30.0,
            )
        )
        server.start()
        self.addCleanup(server.stop)
        return f"http://localhost:{server.address[1]}"


if __name__ == "__main__":
    unittest.main()
