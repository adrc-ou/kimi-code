"""The re-read path for images the proxy evicts from conversation context.

These tests hold the contract between the two halves of the feature: the handle minted in
``model_proxy.evict_answered_images`` is the handle ``analyze_image`` resolves, and the bytes
returned are the bytes the model was originally shown.
"""

import base64
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
for _directory in (ROOT, ROOT / "tools"):
    if str(_directory) not in sys.path:
        sys.path.insert(0, str(_directory))


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


TOOL = load_module("analyze_image", ROOT / "tools" / "analyze_image.py")

# Only the two cross-component tests need the proxy, and importing it is expensive in a
# specific way: test_model_proxy raises SkipTest at module level when aiohttp is absent, and
# an unguarded `from ... import PROXY` would let that swallow this whole file — including the
# path-traversal, symlink and malformed-input tests, which are pure stdlib and matter most on
# a host that has not installed the proxy's dependencies. A green run must never be silent
# about which half it did not execute.
try:
    from tests.test_model_proxy import PROXY  # noqa: E402

    HAVE_PROXY = True
except Exception as exc:  # noqa: BLE001 - any import failure means the same thing here
    PROXY = None
    HAVE_PROXY = False
    PROXY_IMPORT_ERROR = f"{type(exc).__name__}: {exc}"

requires_proxy = unittest.skipUnless(
    HAVE_PROXY,
    "needs the proxy loaded (install aiohttp); the rest of this file runs without it",
)

PNG = bytes.fromhex("89504e470d0a1a0a") + b"\x00" * 40_000


class Fixture(unittest.TestCase):
    """A fake $HOME laid out the way a real session leaves it, with a project to belong to."""

    def setUp(self):
        if HAVE_PROXY:
            # Off unless opted in, and handle_from_proxy runs the real eviction path.
            patcher = patch.object(PROXY, "IMAGE_EVICTION_ENABLED", True)
            patcher.start()
            self.addCleanup(patcher.stop)
        storage = tempfile.TemporaryDirectory()
        self.addCleanup(storage.cleanup)
        self.home = Path(storage.name)
        self.cwd = Path(storage.name) / "project"
        self.cwd.mkdir()
        session = self.home / ".kimi-code/sessions/wd_x/session_y"
        self.media = session / "media"
        self.media.mkdir(parents=True)
        # media_roots() only offers sessions whose recorded cwd is this project, so the fixture
        # has to say so or every lookup comes back empty for the wrong reason.
        (session / "state.json").write_text(json.dumps({"cwd": str(self.cwd.resolve())}))
        self.key = "f_5241a03f-0000-4000-8000-000000000001"
        self.path = self.media / f"{self.key}.png"
        self.path.write_bytes(PNG)
        (self.media / "meta").mkdir()
        (self.media / "meta" / f"{self.key}.json").write_text(
            json.dumps({"name": "page_06-original.png", "mediaType": "image/png"})
        )
        self.catalogue = TOOL.build_catalogue(self.roots())

    def roots(self):
        return TOOL.media_roots(self.home, self.cwd.resolve())

    def reply(self, message, protocol="2025-03-26"):
        return TOOL.respond(message, self.home, protocol, cwd=self.cwd.resolve())

    def handle_from_proxy(self, url: str | None = None) -> str:
        """Evict one image through the proxy and read back the handle it wrote."""
        url = url or "data:image/png;base64," + base64.b64encode(PNG).decode()
        body = json.dumps(
            {
                "messages": [
                    {"role": "user", "content": [{"type": "image_url", "image_url": {"url": url}}]},
                    {"role": "assistant", "content": "answered"},
                ]
            }
        ).encode()
        evicted, report = PROXY.evict_body_images(body)
        self.assertTrue(report)
        text = json.loads(evicted)["messages"][0]["content"][0]["text"]
        return text.split('handle "', 1)[1].split('"', 1)[0]


class RootDiscoveryTests(Fixture):
    def test_the_session_media_directory_is_found(self):
        self.assertIn(self.media, self.roots())

    def test_a_home_with_no_sessions_yields_nothing(self):
        self.assertEqual(TOOL.media_roots(self.home.parent, self.cwd.resolve()), [])

    def test_an_unscoped_lookup_offers_nothing(self):
        """No project to scope to must not widen the search into other projects."""
        self.assertEqual(TOOL.media_roots(self.home), [])

    def test_the_original_filename_is_recovered_from_the_meta_record(self):
        self.assertIn("page_06-original.png", self.catalogue.images[0].names)

    def test_a_media_directory_under_an_agent_is_also_scanned(self):
        agent_media = self.media.parent / "agents" / "agent-3" / "media"
        agent_media.mkdir(parents=True)
        (agent_media / "shot.png").write_bytes(PNG)

        roots = self.roots()

        self.assertIn(agent_media, roots)
        self.assertEqual(len(TOOL.build_catalogue(roots).images), 2)

    def test_another_projects_session_is_not_offered(self):
        """A listing must not read out attachments from a project this agent never opened."""
        other = self.home / ".kimi-code/sessions/wd_z/session_other"
        (other / "media").mkdir(parents=True)
        (other / "state.json").write_text(json.dumps({"cwd": str(self.home / "elsewhere")}))
        (other / "media" / "private.png").write_bytes(PNG)

        roots = self.roots()

        self.assertNotIn(other / "media", roots)
        handles = " ".join(i.display for i in TOOL.build_catalogue(roots).images)
        self.assertNotIn("private.png", handles)

    def test_a_session_that_records_nothing_is_left_out(self):
        broken = self.home / ".kimi-code/sessions/wd_w/session_none"
        (broken / "media").mkdir(parents=True)

        self.assertNotIn(broken / "media", self.roots())


class ResolveTests(Fixture):
    @requires_proxy
    def test_the_proxy_handle_resolves_to_the_file_it_came_from(self):
        handle = self.handle_from_proxy()

        image = TOOL.resolve(handle, self.catalogue)

        self.assertEqual(image.path, self.path)
        self.assertEqual(image.path.read_bytes(), PNG, "the model must see the same bytes")

    def test_a_raw_digest_is_accepted_too(self):
        digest = hashlib.sha256(PNG).hexdigest()
        self.assertEqual(TOOL.resolve(f"sha256:{digest}", self.catalogue).path, self.path)

    def test_name_key_and_filename_all_resolve(self):
        for handle in ("page_06-original.png", self.key, f"{self.key}.png"):
            with self.subTest(handle=handle):
                self.assertEqual(TOOL.resolve(handle, self.catalogue).path, self.path)

    def test_an_unknown_handle_says_what_is_available(self):
        with self.assertRaises(TOOL.HandleError) as caught:
            TOOL.resolve("sha256:deadbeef", self.catalogue)
        self.assertIn("1 images are available", str(caught.exception))

    def test_an_ambiguous_abbreviation_is_refused_rather_than_guessed(self):
        """Two different images sharing a digest prefix must not be resolved by guessing."""
        one = TOOL.Image(
            path=self.path, mime="image/png", size=len(PNG),
            digests=("aabb1111", "raw1"), names=("one.png",),
        )
        two = TOOL.Image(
            path=self.path, mime="image/png", size=len(PNG),
            digests=("aabb2222", "raw2"), names=("two.png",),
        )
        catalogue = TOOL.Catalogue(images=[one, two])

        self.assertIs(TOOL.resolve("sha256:aabb1111", catalogue), one)
        with self.assertRaises(TOOL.HandleError) as caught:
            TOOL.resolve("aabb", catalogue)
        self.assertIn("ambiguous", str(caught.exception))

    @requires_proxy
    def test_a_handle_copied_with_its_sentence_punctuation_still_resolves(self):
        """Models lift handles out of prose and bring the punctuation with them."""
        handle = self.handle_from_proxy()

        for noisy in (f"{handle}.", f"{handle},", f'"{handle}"', f"({handle})", f" {handle} "):
            with self.subTest(handle=noisy):
                self.assertEqual(TOOL.resolve(noisy, self.catalogue).path, self.path)

    def test_an_empty_handle_is_rejected(self):
        for handle in ("", "   ", "sha256:"):
            with self.subTest(handle=handle), self.assertRaises(TOOL.HandleError):
                TOOL.resolve(handle, self.catalogue)


class PayloadTests(Fixture):
    def test_the_prompt_rides_with_the_image_and_the_cue_follows(self):
        image = TOOL.resolve(self.key, self.catalogue)

        parts = TOOL.tool_result(image, "What colours?")["content"]

        self.assertEqual([p["type"] for p in parts], ["text", "image", "text"])
        self.assertIn("What colours?", parts[0]["text"])
        self.assertIn(image.handle, parts[0]["text"], "the same handle must survive the round trip")
        self.assertEqual(parts[1]["mimeType"], "image/png")
        self.assertEqual(base64.b64decode(parts[1]["data"]), PNG)
        self.assertEqual(parts[2]["text"], TOOL.CONTINUE_INSTRUCTION)

    def test_a_blank_prompt_still_produces_a_usable_turn(self):
        parts = TOOL.tool_result(TOOL.resolve(self.key, self.catalogue), "  ")["content"]
        self.assertTrue(parts[0]["text"].strip())

    def test_a_name_that_ends_in_punctuation_is_read_literally_first(self):
        """Tidying must not eat a real filename, so the literal handle has to win."""
        odd = self.media / "figure (1).png"
        odd.write_bytes(b"\x89PNG" + b"z" * 40_000)
        catalogue = TOOL.build_catalogue(self.roots())

        self.assertEqual(TOOL.resolve("figure (1).png", catalogue).path, odd)

    def test_listing_shows_the_handle_and_the_original_filename(self):
        text = TOOL.listing(self.catalogue)["content"][0]["text"]
        self.assertIn(self.catalogue.images[0].handle, text)
        self.assertIn("page_06-original.png", text)

    def test_an_empty_catalogue_lists_without_error(self):
        text = TOOL.listing(TOOL.Catalogue())["content"][0]["text"]
        self.assertIn("No stored images", text)


class TrustBoundaryTests(Fixture):
    """The handle comes from the model; the file it names comes from a shared volume."""

    def test_a_symlink_into_the_media_directory_is_not_served(self):
        """The agent can write here, so a planted link must not become a read primitive."""
        secret = self.home / "outside.txt"
        secret.write_text("do not hand this to a model")
        link = self.media / "evil.png"
        try:
            link.symlink_to(secret)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable here")

        catalogue = TOOL.build_catalogue(self.roots())

        self.assertNotIn("evil.png", [i.path.name for i in catalogue.images])
        with self.assertRaises(TOOL.HandleError):
            TOOL.resolve("evil.png", catalogue)

    def test_a_file_replaced_by_a_symlink_after_cataloguing_is_refused(self):
        image = TOOL.resolve(self.key, self.catalogue)
        victim = image.path
        payload = victim.read_bytes()
        victim.unlink()
        other = self.media / "other.png"
        other.write_bytes(payload)
        try:
            victim.symlink_to(other)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable here")

        stale = TOOL.Image(
            path=victim,
            mime="image/png",
            size=len(payload),
            digests=image.digests,
            names=image.names,
            roots=image.roots,
        )
        with self.assertRaises(TOOL.HandleError):
            TOOL.tool_result(stale, "prompt")

    def test_oversized_files_are_measured_before_being_read(self):
        huge = self.media / "huge.png"
        huge.write_bytes(b"\x89PNG" + b"0" * 5000)
        with patch.object(TOOL, "MAX_IMAGE_BYTES", 1000):
            read_sizes = []
            original = Path.read_bytes

            def spy(self_path):
                read_sizes.append(self_path.name)
                return original(self_path)

            with patch.object(Path, "read_bytes", spy):
                TOOL.build_catalogue(self.roots())

        self.assertNotIn("huge.png", read_sizes, "the cap must be checked before the read")

    def test_malformed_stdin_cannot_kill_the_server(self):
        """A dead server hangs the client until its own timeout, with nothing to read."""
        script = ROOT / "tools" / "analyze_image.py"
        cases = [
            (
                "null then a valid request",
                "null\n" + json.dumps({"id": 0, "method": "tools/list"}) + "\n",
            ),
            ("a deep nest", "[" * 200000 + "\n"),
            ("bare list", "[1,2,3]\n"),
            ("garbage", "not json at all\n"),
        ]
        for label, stdin in cases:
            with self.subTest(label):
                proc = subprocess.run(
                    [sys.executable, str(script)],
                    input=stdin,
                    capture_output=True,
                    text=True,
                    timeout=60,
                    env={**os.environ, "HOME": str(self.home)},
                    cwd=str(self.cwd),
                )
                self.assertEqual(proc.returncode, 0, proc.stderr[-400:])
                if label.startswith("null"):
                    frames = [json.loads(line) for line in proc.stdout.splitlines()]
                    self.assertEqual(frames[0]["id"], 0, "the valid request is still answered")


class TransportTests(Fixture):
    def test_initialize_negotiates_the_version_the_client_asked_for(self):
        for asked in TOOL.PROTOCOL_SUPPORTED:
            result = self.reply(
                {"id": 0, "method": "initialize", "params": {"protocolVersion": asked}}
            )
            self.assertEqual(result["result"]["protocolVersion"], asked)

    def test_an_unknown_version_falls_back_instead_of_failing(self):
        result = self.reply(
            {"id": 0, "method": "initialize", "params": {"protocolVersion": "1999-01-01"}}
        )
        self.assertEqual(result["result"]["protocolVersion"], TOOL.PROTOCOL_FALLBACK)

    def test_notifications_get_no_reply(self):
        self.assertIsNone(self.reply({"method": "notifications/initialized"}))

    def call(self, request_id, name, arguments=None):
        return self.reply(
            {
                "id": request_id,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments or {}},
            }
        )

    def test_tools_list_advertises_both_tools(self):
        listed = self.reply({"id": 1, "method": "tools/list"})["result"]["tools"]
        self.assertEqual([t["name"] for t in listed], ["analyze_image", "list_images"])

    def test_an_unknown_method_is_a_jsonrpc_error_not_a_crash(self):
        reply = self.reply({"id": 2, "method": "resources/list"})
        self.assertEqual(reply["error"]["code"], -32601)

    def test_an_unknown_tool_is_a_jsonrpc_error(self):
        self.assertEqual(self.call(3, "nope")["error"]["code"], -32602)

    def test_a_missing_argument_is_reported_as_a_tool_error(self):
        self.assertTrue(self.call(4, "analyze_image")["result"]["isError"])

    def test_an_unresolvable_handle_is_a_tool_error_the_model_can_read(self):
        result = self.call(5, "analyze_image", {"handle": "ghost", "prompt": "x"})["result"]
        self.assertTrue(result["isError"])
        self.assertIn("no image matches", result["content"][0]["text"])

    def test_every_reply_is_one_line_of_json(self):
        """The stdio contract forbids embedded newlines in a message."""
        for message in (
            {"id": 0, "method": "initialize", "params": {}},
            {"id": 1, "method": "tools/list"},
            {"id": 2, "method": "tools/call", "params": {"name": "list_images"}},
        ):
            with self.subTest(method=message["method"]):
                encoded = json.dumps(self.reply(message), separators=(",", ":"))
                self.assertNotIn("\n", encoded)


if __name__ == "__main__":
    unittest.main()
