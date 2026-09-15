import argparse
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
        cls.server.shutdown()
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


if __name__ == "__main__":
    unittest.main()
