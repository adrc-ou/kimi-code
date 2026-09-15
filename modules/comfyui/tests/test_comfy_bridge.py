import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("comfy_bridge", ROOT / "scripts" / "comfy_bridge.py")
assert SPEC and SPEC.loader
BRIDGE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BRIDGE)


class Request:
    def __init__(self, authorization=""):
        self.app = {"token": "a" * 64}
        self.headers = {
            "Authorization": authorization,
            "Host": "untrusted.example",
            "Content-Type": "application/json",
        }


class Headers(dict):
    pass


class BridgeTests(unittest.TestCase):
    def test_requires_exact_bearer_token(self):
        self.assertFalse(BRIDGE.authorized(Request()))
        self.assertFalse(BRIDGE.authorized(Request("Bearer wrong")))
        self.assertTrue(BRIDGE.authorized(Request(f"Bearer {'a' * 64}")))

    def test_strips_authorization_and_host_upstream(self):
        headers = BRIDGE.request_headers(Request(f"Bearer {'a' * 64}"))
        self.assertEqual(headers, {"Content-Type": "application/json"})

    def test_strips_connection_nominated_headers(self):
        request = Request(f"Bearer {'a' * 64}")
        request.headers["Connection"] = "X-Private"
        request.headers["X-Private"] = "remove"
        self.assertNotIn("X-Private", BRIDGE.request_headers(request))


if __name__ == "__main__":
    unittest.main()
