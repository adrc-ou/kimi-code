import argparse
import ast
import contextlib
import importlib.util
import io
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "comfyctl", ROOT / "runtime" / "tools" / "comfyctl.py"
)
assert SPEC and SPEC.loader
COMFYCTL = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(COMFYCTL)


def _fold(node, known):
    """Evaluate a top-level string assignment, resolving f-string names against `known`."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return known.get(node.id)
    if isinstance(node, ast.FormattedValue):
        return _fold(node.value, known)
    if isinstance(node, ast.JoinedStr):
        parts = [_fold(value, known) for value in node.values]
        return None if any(part is None for part in parts) else "".join(parts)
    return None


def bridge_constants():
    """String constants defined at the top level of the bridge, read without importing it."""
    tree = ast.parse((ROOT / "scripts" / "comfy_bridge.py").read_text())
    known: dict[str, str] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name):
            continue
        value = _fold(node.value, known)
        if value is not None:
            known[target.id] = value
    return known


BRIDGE = bridge_constants()
GRANT_VALUE = "grant-value"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, _format, *_args):
        return

    def send_json(self, payload):
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.headers.get("Authorization") != "Bearer test-token":
            self.send_error(401)
            return
        if self.path == "/system_stats":
            self.send_json({"system": "mock"})
        elif self.path == "/queue":
            self.send_json({"queue_running": [], "queue_pending": []})
        elif self.path.startswith("/history/prompt-1"):
            self.send_json(
                {
                    "prompt-1": {
                        "status": {"status_str": "success", "messages": []},
                        "outputs": {
                            "9": {
                                "images": [
                                    {
                                        "filename": "result.png",
                                        "subfolder": "",
                                        "type": "output",
                                    }
                                ]
                            }
                        },
                    }
                }
            )
        elif self.path.startswith("/view?"):
            body = b"mock-image"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_error(404)

    def do_POST(self):
        if self.headers.get("Authorization") != "Bearer test-token":
            self.send_error(401)
            return
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        if self.path == "/prompt":
            self.send_json({"prompt_id": "prompt-1", "number": 1, "node_errors": {}})
        elif self.path == BRIDGE["GRANT_PATH"]:
            self.send_json({"path": f"{BRIDGE['OPEN_PATH']}#{GRANT_VALUE}"})
        elif self.path == "/upload/image" and b'name="image"' in body:
            self.send_json({"name": "input.png", "type": "input", "subfolder": ""})
        elif self.path == "/interrupt":
            self.send_json({})
        else:
            self.send_error(404)


class ComfyctlTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        COMFYCTL.BASE_URL = f"http://127.0.0.1:{cls.server.server_port}"
        COMFYCTL.AUTH_TOKEN = "test-token"

    @classmethod
    def tearDownClass(cls):
        # ``shutdown`` only stops the accept loop; the listening socket stays bound until
        # ``server_close`` releases it, so leaving it out leaks a descriptor per class.
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def test_authenticated_json_request(self):
        self.assertEqual(COMFYCTL.request_json("GET", "/system_stats"), {"system": "mock"})

    def test_wait_returns_completed_history(self):
        history = COMFYCTL.wait_for_prompt("prompt-1", timeout=1, interval=0.01)
        self.assertIn("prompt-1", history)

    def test_upload_and_download(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "input.png"
            source.write_bytes(b"input")
            with contextlib.redirect_stdout(io.StringIO()):
                COMFYCTL.cmd_upload(
                    argparse.Namespace(file=str(source), subfolder="", overwrite=False)
                )
            output = Path(directory) / "outputs"
            with contextlib.redirect_stdout(io.StringIO()):
                COMFYCTL.cmd_download(
                    argparse.Namespace(prompt_id="prompt-1", directory=str(output))
                )
            self.assertEqual((output / "9-images-result.png").read_bytes(), b"mock-image")

    def test_structured_workflow_error(self):
        record = {
            "status": {
                "status_str": "error",
                "messages": [["execution_error", {"exception_message": "boom"}]],
            }
        }
        self.assertIn("boom", COMFYCTL.workflow_failure(record))

    def test_rejects_untrusted_output_components(self):
        for value in (
            "../escape",
            "/absolute",
            "nested/name",
            "nested\\name",
            "..",
            "CON",
            "trailing.",
            "unicode／slash",
        ):
            with self.assertRaises(SystemExit):
                COMFYCTL.safe_component(value, "component")

    def test_rejects_symlink_download_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            real = Path(directory) / "real"
            real.mkdir()
            link = Path(directory) / "link"
            link.symlink_to(real, target_is_directory=True)
            with self.assertRaisesRegex(SystemExit, "real directory"):
                COMFYCTL.cmd_download(argparse.Namespace(prompt_id="prompt-1", directory=str(link)))

    def test_download_does_not_overwrite_collision(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            existing = output / "9-images-result.png"
            existing.write_bytes(b"keep")
            with contextlib.redirect_stdout(io.StringIO()):
                COMFYCTL.cmd_download(argparse.Namespace(prompt_id="prompt-1", directory=directory))
            self.assertEqual(existing.read_bytes(), b"keep")
            self.assertEqual((output / "9-images-result-1.png").read_bytes(), b"mock-image")

    def test_failed_download_removes_its_temporary_file(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(COMFYCTL, "MAX_TRANSFER_BYTES", 5):
                with self.assertRaisesRegex(SystemExit, "MAX_TRANSFER_BYTES"):
                    COMFYCTL.cmd_download(
                        argparse.Namespace(prompt_id="prompt-1", directory=directory)
                    )
            self.assertEqual(list(Path(directory).iterdir()), [])


class FrontendUrlTests(unittest.TestCase):
    """The bridge path names comfyctl uses are the ones the bridge actually serves."""

    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        COMFYCTL.BASE_URL = f"http://127.0.0.1:{cls.server.server_port}"
        COMFYCTL.AUTH_TOKEN = "test-token"

    @classmethod
    def tearDownClass(cls):
        # ``shutdown`` only stops the accept loop; the listening socket stays bound until
        # ``server_close`` releases it, so leaving it out leaks a descriptor per class.
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def test_grant_path_matches_the_bridge_source(self):
        self.assertEqual(COMFYCTL.GRANT_PATH, BRIDGE["GRANT_PATH"])
        self.assertEqual(COMFYCTL.SESSION_PATH, BRIDGE["SESSION_PATH"])
        self.assertEqual(BRIDGE["GRANT_PATH"], f"{BRIDGE['BRIDGE_PREFIX']}/grant")

    def test_the_sandbox_healthcheck_probes_a_path_the_bridge_serves(self):
        # kimi-agent waits on this container's health, so a healthcheck naming a path the bridge
        # stops serving would hold the whole session down rather than report a fault.
        overlay = (ROOT / "compose.mps.yaml").read_text()
        self.assertIn(BRIDGE["OPEN_PATH"], overlay)

    def test_ui_url_is_the_plain_origin_without_a_token(self):
        with patch.object(COMFYCTL, "UI_URL", "http://comfyui:8188"):
            with patch.object(COMFYCTL, "AUTH_TOKEN", ""):
                with contextlib.redirect_stdout(io.StringIO()) as out:
                    COMFYCTL.cmd_ui_url(argparse.Namespace())
        self.assertEqual(out.getvalue().strip(), "http://comfyui:8188")

    def test_ui_url_exchanges_the_token_for_a_single_use_link(self):
        with patch.object(COMFYCTL, "UI_URL", "http://comfyui-ui:8188"):
            with contextlib.redirect_stdout(io.StringIO()) as out:
                COMFYCTL.cmd_ui_url(argparse.Namespace())
        link = out.getvalue().strip()
        self.assertEqual(link, f"http://comfyui-ui:8188{BRIDGE['OPEN_PATH']}#{GRANT_VALUE}")
        self.assertNotIn("test-token", link)

    def test_ui_url_falls_back_to_the_api_origin(self):
        with patch.object(COMFYCTL, "UI_URL", ""):
            with contextlib.redirect_stdout(io.StringIO()) as out:
                COMFYCTL.cmd_ui_url(argparse.Namespace())
        self.assertTrue(out.getvalue().startswith(COMFYCTL.BASE_URL))

    def test_ui_url_rejects_a_malformed_grant(self):
        with patch.object(COMFYCTL, "request_json", return_value={"path": "nope"}):
            with self.assertRaisesRegex(SystemExit, "did not offer"):
                COMFYCTL.cmd_ui_url(argparse.Namespace())


if __name__ == "__main__":
    unittest.main()
