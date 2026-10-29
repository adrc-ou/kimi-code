"""Weight state: what makes a local model ``Ready``, and what must refuse to say so.

``Ready?`` is the picker's only claim about bytes that are not in the repository, so these tests
are about the claim being *earned*: a lock that pins one commit and digests every file, a marker
that records those digests were compared, and a status that falls back to ``Not Installed`` for
every way the two can stop agreeing.

Install and serve are tested in the same file once they exist; state is the part the launch path
already depends on, because the picker reads it on every row it draws.
"""

import hashlib
import io
import json
import os
import shutil
import sys
import tarfile
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(ROOT / "tools"))

import local_llm  # noqa: E402

REVISION = "0123456789abcdef0123456789abcdef01234567"
DIGEST = "a" * 64
OTHER = "b" * 64

LOCK = {
    "schema_version": 1,
    "source": {
        "hub": "huggingface",
        "repo": "fixture/Model-GGUF",
        "revision": REVISION,
        "upstream": "fixture/Model",
    },
    "files": [{"path": "model-00001-of-00002.gguf", "sha256": DIGEST, "size": 4096}],
}


class WeightFixture(unittest.TestCase):
    """One model directory, its lock, and the definition dict discovery would have handed over."""

    def setUp(self):
        base = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, base, ignore_errors=True)
        self.directory = base / "models" / "local_fixture" / "local_model"
        self.directory.mkdir(parents=True)
        self.weights = self.directory / "weights"
        self.model = {
            "id": "local_fixture/local_model",
            "name": "local_model",
            "where": "models/local_fixture/local_model",
            "provider": "local_fixture",
            "label": "Local Model",
            "credential": "default",
            "model": "fixture-model",
            "install": {"variant": "Q4", "lock": "weights.lock.json", "sampling": {}},
            "lock_path": self.directory / "weights.lock.json",
            "weights_dir": self.weights,
        }

    def write_lock(self, **changes):
        """Write a lock and return it. ``artifact=None`` drops the block entirely.

        A schema-2 lock is only valid with an ``[artifact]`` table, so the fixture supplies the
        minimum truthful one rather than every test restating it; the tests that prove that rule
        need a way to leave it out, which is what the sentinel is for.
        """
        document = json.loads(json.dumps(LOCK))
        absent = object()
        explicit_artifact = changes.pop("artifact", absent)
        if changes.get("schema_version") == 2 and explicit_artifact is absent:
            document["artifact"] = {"format": "gguf", "variant": "Q4", "directory": ""}
        document.update(changes)
        if explicit_artifact is None:
            document.pop("artifact", None)
        elif explicit_artifact is not absent:
            document["artifact"] = explicit_artifact
        self.model["lock_path"].write_text(json.dumps(document))
        return document

    def write_weights(self, lock, digest=DIGEST, size=4096):
        self.weights.mkdir(parents=True, exist_ok=True)
        for entry in lock["files"]:
            (self.weights / entry["path"]).write_bytes(b"\0" * size)

    def write_marker(self, lock, revision=None, variant="Q4", digest=None):
        self.weights.mkdir(parents=True, exist_ok=True)
        recorded = {
            "schema_version": 1,
            "revision": revision or lock["source"]["revision"],
            "variant": variant,
            "files": [
                {"path": entry["path"], "sha256": digest or entry["sha256"]}
                for entry in lock["files"]
            ],
        }
        (self.weights / local_llm.STATE_FILE).write_text(json.dumps(recorded))


class LockTests(WeightFixture):
    def test_a_pinned_lock_loads(self):
        self.write_lock()
        document = local_llm.read_lock(self.model["lock_path"])
        self.assertEqual(document["source"]["revision"], REVISION)
        self.assertEqual(
            local_llm.download_url(document["source"], "model.gguf"),
            f"https://huggingface.co/fixture/Model-GGUF/resolve/{REVISION}/model.gguf",
        )

    def _refused(self, **changes):
        self.write_lock(**changes)
        with self.assertRaises(local_llm.LocalModelError) as caught:
            local_llm.read_lock(self.model["lock_path"])
        return str(caught.exception)

    def test_a_branch_instead_of_a_commit_is_refused(self):
        # main is a moving target: the digest would still catch a changed file, but only after
        # a hundred gigabytes had been fetched to find out.
        self.assertIn("40-hex", self._refused(source={**LOCK["source"], "revision": "main"}))

    def test_an_unpinned_digest_is_refused(self):
        files = [{**LOCK["files"][0], "sha256": "abc"}]
        self.assertIn("64-hex", self._refused(files=files))

    def test_a_file_without_a_size_is_refused(self):
        self.assertIn("positive size", self._refused(files=[{"path": "x.gguf", "sha256": DIGEST}]))

    def test_a_path_that_escapes_the_weights_directory_is_refused(self):
        for name in ("/etc/passwd", "../outside.gguf"):
            files = [{"path": name, "sha256": DIGEST, "size": 1}]
            with self.subTest(path=name):
                self.assertIn("relative to the weights directory", self._refused(files=files))

    def test_the_same_file_listed_twice_is_refused(self):
        self.assertIn("listed twice", self._refused(files=[LOCK["files"][0], LOCK["files"][0]]))

    def test_an_empty_file_list_is_refused(self):
        # An install block that installs nothing is a definition that will never reach Ready.
        self.assertIn("at least one file", self._refused(files=[]))

    def test_a_missing_lock_is_refused_not_treated_as_uninstalled(self):
        with self.assertRaises(local_llm.LocalModelError):
            local_llm.read_lock(self.directory / "absent.json")

    def test_an_unknown_lock_schema_version_is_refused(self):
        for schema_version in (0, 3, "2"):
            with self.subTest(schema_version=schema_version):
                self.assertIn("schema_version", self._refused(schema_version=schema_version))

    def test_schema_two_needs_the_artifact_it_claims_to_pin(self):
        # A schema-2 lock exists to say which loader can consume these bytes. Without the block it
        # is a schema-1 lock wearing a higher number, and readiness would be indistinguishable.
        self.assertIn("artifact", self._refused(schema_version=2, artifact=None))

    def test_the_artifact_format_is_the_only_thing_separating_two_containers_of_one_quant(self):
        lock = self.write_lock(schema_version=2)
        for kind in ("safetensors", "bin", "GGUF", ""):
            artifact = {**lock["artifact"], "format": kind}
            with self.subTest(format=kind):
                self.assertIn(
                    "artifact.format",
                    self._refused(
                        schema_version=2,
                        artifact=artifact,
                        source=lock["source"],
                        files=lock["files"],
                    ),
                )

    def test_the_artifact_directory_cannot_escape_the_weights_tree(self):
        lock = self.write_lock(schema_version=2)
        for directory in ("../outside", "/absOLUTE", "a/../../b"):
            with self.subTest(directory=directory):
                self.assertIn(
                    "artifact.directory",
                    self._refused(
                        schema_version=2,
                        artifact={**lock["artifact"], "directory": directory},
                        source=lock["source"],
                        files=lock["files"],
                    ),
                )

    def test_schema_two_with_a_valid_artifact_loads(self):
        lock = self.write_lock(schema_version=2)
        self.assertEqual(lock["artifact"]["format"], "gguf")


class StateTests(WeightFixture):
    def test_files_present_needs_every_byte_count(self):
        lock = self.write_lock()
        self.assertFalse(local_llm.files_present(self.model, lock))
        self.write_weights(lock)
        self.assertTrue(local_llm.files_present(self.model, lock))
        # A truncated download that resumed into the right name is the case this catches.
        (self.weights / "model-00001-of-00002.gguf").write_bytes(b"\0" * 4095)
        self.assertFalse(local_llm.files_present(self.model, lock))

    def test_nothing_is_verified_until_the_bytes_have_been_hashed_once(self):
        lock = self.write_lock()
        self.write_weights(lock)
        self.assertFalse(local_llm.verified(self.model, lock))
        self.assertEqual(local_llm.status_text(self.model), local_llm.MISSING)

    def test_a_verified_weight_set_reports_ready(self):
        lock = self.write_lock()
        self.write_weights(lock)
        self.write_marker(lock)
        self.assertTrue(local_llm.verified(self.model, lock))
        self.assertEqual(local_llm.status_text(self.model), local_llm.READY)

    def test_a_different_commit_invalidates_the_marker(self):
        lock = self.write_lock()
        self.write_weights(lock)
        self.write_marker(lock, revision="f" * 40)
        self.assertFalse(local_llm.verified(self.model, lock))

    def test_a_different_artifact_format_invalidates_the_marker(self):
        # One model, one variant name, two containers: the marker must not cross between them.
        lock = self.write_lock()
        self.write_weights(lock)
        local_llm.record(self.model, lock)
        self.assertTrue(local_llm.verified(self.model, lock))
        mlx = {**lock, "schema_version": 2, "artifact": {"format": "mlx", "variant": "Q4"}}
        self.assertFalse(local_llm.verified(self.model, mlx))

    def test_a_different_variant_invalidates_the_marker(self):
        lock = self.write_lock()
        self.write_weights(lock)
        self.write_marker(lock, variant="Q8")
        self.assertFalse(local_llm.verified(self.model, lock))

    def test_a_digest_that_stopped_matching_the_lock_invalidates_the_marker(self):
        lock = self.write_lock()
        self.write_weights(lock)
        self.write_marker(lock, digest=OTHER)
        self.assertFalse(local_llm.verified(self.model, lock))

    def test_a_corrupt_marker_is_uninstalled_rather_than_a_failed_launch(self):
        # The picker reads this on every row it draws; a half-written marker means "hash it
        # again", not "the launcher cannot start".
        lock = self.write_lock()
        self.write_weights(lock)
        (self.weights / local_llm.STATE_FILE).write_text("{not json")
        self.assertFalse(local_llm.verified(self.model, lock))
        self.assertEqual(local_llm.status_text(self.model), local_llm.MISSING)

    def test_a_remote_model_is_ready_without_being_asked(self):
        remote = {"id": "other", "provider": "nrp", "label": "Remote", "install": None}
        self.assertEqual(local_llm.status_text(remote), local_llm.READY)

    def test_a_local_model_with_a_broken_lock_says_not_installed(self):
        # Discovery refuses this shape outright, so the only way to reach it is a lock edited
        # after load; the column then reports the truth instead of raising in the middle of a
        # screen the operator is trying to answer.
        self.model["lock_path"].write_text("{}")
        self.assertEqual(local_llm.status_text(self.model), local_llm.MISSING)


class TextTests(unittest.TestCase):
    def test_sizes_are_written_the_way_the_publisher_writes_them(self):
        # Decimal, because "17.6 GB" is what the vendor's own requirements table says and a
        # picker that quietly converts to GiB contradicts the document the operator just read.
        self.assertEqual(local_llm.size_text(17_600_000_000), "17.6 GB")
        self.assertEqual(local_llm.size_text(111_300_000_000), "111.3 GB")
        self.assertEqual(local_llm.size_text(900), "900 B")
        self.assertEqual(local_llm.size_text(2_500_000), "2.5 MB")

    def test_total_bytes_adds_the_lock(self):
        lock = {"files": [{"size": 1000}, {"size": 250}]}
        self.assertEqual(local_llm.total_bytes(lock), 1250)


# --------------------------------------------------------------------------------------------
# install, engine, serve
# --------------------------------------------------------------------------------------------


def fake_hub(files: dict[str, bytes], truncated: frozenset[str] = frozenset()):
    """A hub-shaped HTTP server: ``/resolve/<revision>/<name>``, Range-aware, optionally lying.

    Real HTTP rather than a stubbed ``urlopen``, because the parts of the installer worth testing
    are the ones a stub would smooth over: the range request that resumes a partial file, the
    short body that must not become a "successful" download, and the header that must never be
    sent to a mirror.
    """
    seen: list[tuple[str, dict]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler's name
            seen.append((self.path, dict(self.headers.items())))
            name = self.path.rsplit("/", 1)[-1]
            if name not in files:
                self.send_error(404)
                return
            blob = files[name]
            if name in truncated:
                blob = blob[: len(blob) // 2]
            start = 0
            header = self.headers.get("Range")
            if header and name not in truncated:
                start = int(header.split("=", 1)[1].split("-", 1)[0])
            chunk = blob[start:]
            self.send_response(206 if start else 200)
            self.send_header("Content-Length", str(len(chunk)))
            if start:
                self.send_header("Content-Range", f"bytes {start}-{len(blob) - 1}/{len(blob)}")
            self.end_headers()
            self.wfile.write(chunk)

        def log_message(self, *args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"

    def stop():
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    return base, seen, stop


class InstallFixture(WeightFixture):
    """One local model whose weights are two shards, served from a hub on loopback."""

    def setUp(self):
        super().setUp()
        self.first = b"first shard\x00" * 64
        self.second = b"second shard" * 48
        self.shards = {
            "shard-00001-of-00002.gguf": self.first,
            "shard-00002-of-00002.gguf": self.second,
        }

    def hub(self, files=None, truncated=frozenset(), served=None):
        # `files` is what the lock pins and `served` is what the hub returns. Splitting them is the
        # only way to exercise a re-uploaded file, which is exactly the failure a digest exists for.
        base, seen, stop = fake_hub(served or files or self.shards, truncated)
        self.addCleanup(stop)
        lock = {
            "schema_version": 1,
            "source": {
                "hub": "huggingface",
                "repo": "fixture/Model-GGUF",
                "revision": REVISION,
                "base": base,
            },
            "files": [
                {
                    "path": name,
                    "sha256": hashlib.sha256(blob).hexdigest(),
                    "size": len(blob),
                }
                for name, blob in (files or self.shards).items()
            ],
        }
        self.model["lock_path"].write_text(json.dumps(lock))
        return base, seen

    def installed(self):
        return local_llm.install(self.model)

    def fake_engine(self):
        """A path the override check will accept: a real file whose execute bit is honoured.

        ``engine_binary()`` refuses anything it cannot run, so the name has to be executable
        rather than merely plausible - and a temporary file will not do here, because this
        container mounts its temp directory noexec.
        """
        return Path(sys.executable)


class InstallTests(InstallFixture):
    def test_install_fetches_verifies_and_records(self):
        self.hub()
        self.assertTrue(local_llm.install(self.model))
        directory = self.weights
        for name, blob in self.shards.items():
            self.assertEqual((directory / name).read_bytes(), blob)
            self.assertFalse((directory / (name + ".part")).exists())
        self.assertTrue((directory / local_llm.STATE_FILE).is_file())
        self.assertEqual(local_llm.status_text(self.model), local_llm.READY)
        # No .part survives a completed file, and the marker is owner-only: it records that a
        # digest was matched, and a world-readable artifact invites someone else to trust it.
        self.assertEqual((directory / local_llm.STATE_FILE).stat().st_mode & 0o777, 0o600)

    def test_a_reinstall_of_verified_bytes_asks_for_nothing(self):
        base, seen = self.hub()
        local_llm.install(self.model)
        seen.clear()
        self.assertTrue(local_llm.install(self.model))
        self.assertEqual(seen, [], f"re-install refetched from {base}")

    def test_hand_copied_weights_are_hashed_in_place_not_refetched(self):
        # The documented escape hatch: weights placed by rsync or a copy from another Mac become
        # "Ready" after one read, because what was missing was the verification, not the bytes.
        _, seen = self.hub()
        self.weights.mkdir(parents=True)
        for name, blob in self.shards.items():
            (self.weights / name).write_bytes(blob)
        self.assertTrue(local_llm.install(self.model))
        self.assertEqual(seen, [])
        self.assertEqual(local_llm.status_text(self.model), local_llm.READY)

    def test_a_part_transfer_resumes(self):
        _, seen = self.hub()
        name, blob = "shard-00001-of-00002.gguf", self.first
        self.weights.mkdir(parents=True)
        (self.weights / f"{name}.part").write_bytes(blob[: len(blob) // 2])
        local_llm.install(self.model)
        self.assertEqual((self.weights / name).read_bytes(), blob)
        ranges = [headers.get("Range") for path, headers in seen if name in path]
        self.assertIn(f"bytes={len(blob) // 2}-", ranges)

    def test_bytes_that_do_not_match_the_lock_are_deleted_and_refused(self):
        corrupted = dict(self.shards)
        # Same length, different bytes: the case a size check cannot see and a digest can.
        name = "shard-00002-of-00002.gguf"
        corrupted[name] = b"x" * len(self.shards[name])
        self.hub(served=corrupted)
        with self.assertRaises(local_llm.LocalModelError) as caught:
            local_llm.install(self.model)
        self.assertIn("SHA-256 does not match", str(caught.exception))
        self.assertFalse((self.weights / local_llm.STATE_FILE).exists())
        self.assertEqual(local_llm.status_text(self.model), local_llm.MISSING)

    def test_a_short_body_is_not_a_successful_download(self):
        # Content-Length lies about the transfer and the digest would catch it, but only because
        # the size the lock states is checked first: a truncated file must never be what a
        # verification pass re-hashes on a Sunday.
        self.hub(truncated=frozenset({"shard-00001-of-00002.gguf"}))
        with self.assertRaises(local_llm.LocalModelError) as caught:
            local_llm.install(self.model)
        self.assertIn("incomplete", str(caught.exception))

    def test_a_mirror_gets_the_bytes_but_never_the_token(self):
        # The lock's `base` is the private-mirror hook. The digest still decides whether the
        # mirror is trusted, and the hub token is not handed to it to find out.
        env = self.model["lock_path"].parent.parent.parent / "resolved.env"
        env.write_text("HF_TOKEN=super-secret-token\n")
        _, seen = self.hub()
        with mock.patch.dict(os.environ, {"HARNESS_RESOLVED_BOOTSTRAP": str(env)}):
            local_llm.install(self.model)
        self.assertTrue(seen)
        for _, headers in seen:
            self.assertNotIn("Authorization", headers)

    def test_the_token_goes_to_the_hub_and_nowhere_else(self):
        env = self.model["lock_path"].parent.parent.parent / "resolved.env"
        env.write_text("HF_TOKEN=super-secret-token\n")
        base, seen = self.hub()
        # The mirror base is removed, so every URL now derives from HUB itself - pointed at
        # loopback, because a test must not reach the real hub to prove where a header goes.
        lock = json.loads(self.model["lock_path"].read_text())
        del lock["source"]["base"]
        self.model["lock_path"].write_text(json.dumps(lock))
        with (
            mock.patch.dict(os.environ, {"HARNESS_RESOLVED_BOOTSTRAP": str(env)}),
            mock.patch.object(local_llm, "HUB", base),
        ):
            local_llm.install(self.model)
        self.assertTrue(seen)
        self.assertTrue(all(h.get("Authorization") == "Bearer super-secret-token" for _, h in seen))

    def test_an_unpinned_lock_is_refused_before_any_transfer(self):
        _, seen = self.hub()
        lock = json.loads(self.model["lock_path"].read_text())
        lock["source"]["revision"] = "main"
        self.model["lock_path"].write_text(json.dumps(lock))
        with self.assertRaises(local_llm.LocalModelError):
            local_llm.install(self.model)
        self.assertEqual(seen, [])


class EngineTests(InstallFixture):
    def test_inspecting_a_missing_engine_never_fetches_it(self):
        with (
            mock.patch.dict(os.environ, {"LOCAL_MPS_LLAMA_SERVER": ""}),
            mock.patch.object(local_llm, "engine_root", return_value=self.directory / "runtimes"),
            mock.patch.object(local_llm, "engine_plan", return_value={"version": "missing"}),
            mock.patch.object(local_llm, "fetch_engine") as fetch,
        ):
            with self.assertRaisesRegex(local_llm.EngineError, "not installed"):
                local_llm.engine_binary(install=False)
        fetch.assert_not_called()

    def test_an_operator_supplied_binary_skips_the_runtime_entirely(self):
        script = self.fake_engine()
        with mock.patch.dict(os.environ, {"LOCAL_MPS_LLAMA_SERVER": str(script)}):
            self.assertEqual(local_llm.engine_binary(), script)

    def test_a_named_binary_that_cannot_run_is_refused_not_downloaded(self):
        with mock.patch.dict(os.environ, {"LOCAL_MPS_LLAMA_SERVER": "/nope/llama-server"}):
            with self.assertRaises(local_llm.EngineError):
                local_llm.engine_binary()

    def test_the_pinned_archive_is_verified_then_unpacked_once(self):
        # Built the way the real release is built: a top-level directory, an executable
        # llama-server beside the dylibs, and a versioned dylib symlink pointing at its neighbour.
        payload = self.directory / "engine-payload" / "llama-x"
        payload.mkdir(parents=True)
        binary = payload / "llama-server"
        binary.write_text("#!/bin/sh\nexit 0\n")
        binary.chmod(0o755)
        (payload / "libllama.0.7.0.dylib").write_bytes(b"\xcf\xfa\xed\xfe")
        (payload / "libllama.0.dylib").symlink_to("libllama.0.7.0.dylib")
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode="w:gz") as bundle:
            bundle.add(payload, arcname="llama-x", filter=self.portable)
        blob = archive.getvalue()
        base, seen, stop = fake_hub({"llama.tar.gz": blob})
        self.addCleanup(stop)
        entry = {
            "version": "x1",
            "url": f"{base}/releases/download/x1/llama.tar.gz",
            "digest": "sha256:" + hashlib.sha256(blob).hexdigest(),
        }
        target = self.directory / "runtime" / "x1"
        with (
            mock.patch.object(local_llm, "engine_root", lambda: target.parent),
            mock.patch.object(local_llm, "engine_plan", lambda: entry),
        ):
            local_llm.fetch_engine(entry, target)
            found = target / "llama-x" / "llama-server"
            self.assertTrue(found.is_file())
            # The archive's mode bit survives the unpack, which is the part this code controls.
            # Whether the volume honours it is the operator's mount choice - and this container
            # mounts its temp directory noexec, so an os.access() here would fail for a reason
            # that has nothing to do with the fetch.
            self.assertTrue(found.stat().st_mode & 0o111, "the engine lost its executable bit")
            self.assertTrue((target / "llama-x" / "libllama.0.dylib").is_symlink())
            # The archive is not kept: 16 MB of duplicate, already accounted for by its digest.
            self.assertEqual(list(target.parent.glob("*.tgz")), [])
            seen.clear()
            self.assertEqual(local_llm.engine_binary(), found)
            self.assertEqual(seen, [], "an installed engine was fetched twice")

    @staticmethod
    def portable(member):
        """Keep the symlink in the archive instead of following it into a duplicate file."""
        if member.issym():
            member.uid = member.gid = 0
            member.uname = member.gname = ""
        return member

    def test_an_archive_that_links_outside_itself_is_refused(self):
        payload = self.directory / "engine-payload"
        payload.mkdir(parents=True, exist_ok=True)
        escape = payload / "llama-server"
        escape.write_text("#!/bin/sh\nexit 0\n")
        link = payload / "sneaky.dylib"
        link.symlink_to("/etc/passwd")
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode="w:gz") as bundle:
            bundle.add(str(payload / "llama-server"), arcname="llama-x/llama-server")
            bundle.add(str(link), arcname="llama-x/sneaky.dylib")
        base, _, stop = fake_hub({"llama.tar.gz": archive.getvalue()})
        self.addCleanup(stop)
        entry = {
            "version": "x3",
            "url": f"{base}/releases/download/x3/llama.tar.gz",
            "digest": "sha256:" + hashlib.sha256(archive.getvalue()).hexdigest(),
        }
        target = self.directory / "runtime" / "x3"
        with mock.patch.object(local_llm, "engine_root", lambda: target.parent):
            with self.assertRaises(local_llm.EngineError) as caught:
                local_llm.fetch_engine(entry, target)
        self.assertIn("links out of its directory", str(caught.exception))
        self.assertFalse(target.exists())

    def test_an_engine_archive_that_fails_its_digest_never_lands(self):
        blob = b"not really an engine"
        base, _, stop = fake_hub({"llama.tar.gz": blob})
        self.addCleanup(stop)
        entry = {
            "version": "x2",
            "url": f"{base}/releases/download/x2/llama.tar.gz",
            "digest": "sha256:" + hashlib.sha256(b"someone else's bytes").hexdigest(),
        }
        target = self.directory / "runtime" / "x2"
        with mock.patch.object(local_llm, "engine_root", lambda: target.parent):
            with self.assertRaises(local_llm.EngineError):
                local_llm.fetch_engine(entry, target)
        self.assertFalse(target.exists())


class _ChunkedStream:
    """A streamed HTTP response body, as :func:`urllib.request.urlopen` hands one back.

    The canary reads in fixed blocks, so the fixture has to support ``read(n)`` until it returns
    empty, plus the context-manager protocol ``wait_engine_ready`` enters.
    """

    def __init__(self, payload: bytes, status: int = 200) -> None:
        self._body = io.BytesIO(payload)
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self._body.close()
        return False

    def read(self, size: int = -1) -> bytes:
        return self._body.read(size)


class _StubLaunch:
    def __init__(self, argv):
        self.argv = tuple(argv)
        self.environment = {}
        self.host = "127.0.0.1"
        self.port = 8181
        self.secret_argument_indices = ()


class _StubAdapter:
    """Stands in for the engine adapter so a serve test does not provision or download."""

    def validate_artifact(self, model, lock):
        return type(
            "A", (), {"primary": Path(model["lock_path"]).parent / "fixture.gguf"}
        )()

    def provision(self, *, install=True):
        return type(
            "R",
            (),
            {
                "identity": "stub",
                "directory": Path(tempfile.gettempdir()),
                "executable": Path(sys.executable),
                "details": {},
            },
        )()

    def launch(self, model, artifact, runtime, engine, token):
        return _StubLaunch(
            local_llm.engine_args(model, 8192, 2, token, dict(engine["allocation"]))
        )


def _stub_adapter(_kind):
    return _StubAdapter()


class ServeTests(InstallFixture):
    @staticmethod
    def transport(props: dict, *, canary: str = "content") -> mock.Mock:
        """Answer ``/props`` and the compute canary the way a real engine does.

        Readiness is now a generation, not a metadata read, so a fixture that only serves ``/props``
        describes the buggy contract. ``canary`` picks what generation does, which is how the tests
        below cover the poisoned-backend case: metadata healthy, compute producing nothing.
        """
        tools_events = [
            b'data: {"choices":[{"delta":{"tool_calls":[{"index":0}]}}]}\n\n',
            b"data: [DONE]\n\n",
        ]
        events = {
            "content": [
                b'data: {"choices":[{"delta":{"content":"ready"}}]}\n\n',
                b"data: [DONE]\n\n",
            ],
            "empty": [b"data: [DONE]\n\n"],
            "error": [b'data: {"error":{"message":"metal could not allocate"}}\n\n'],
            "reasoning": [
                b'data: {"choices":[{"delta":{"reasoning_content":"hmm"}}]}\n\n',
                b"data: [DONE]\n\n",
            ],
        }[canary]

        def urlopen(request, timeout=None):
            url = getattr(request, "full_url", str(request))
            if url.endswith("/props"):
                return io.BytesIO(json.dumps(props).encode())
            body = json.loads(getattr(request, "data", b"{}") or b"{}")
            if "tools" in body:
                return _ChunkedStream(b"".join(tools_events))
            return _ChunkedStream(b"".join(events))

        return mock.Mock(side_effect=urlopen)

    def test_ready_engine_is_not_rejected_for_high_or_unknown_memory(self):
        import host_caps

        self.model["install"]["port"] = 8181
        engine = {
            "context_tokens": 4096,
            "sequences": 1,
            "model_alias": "fixture",
            "model_path": "/fixture.gguf",
            "build_commit": "abcdef0",
        }
        props = {
            "default_generation_settings": {"n_ctx": 4096},
            "total_slots": 1,
            "model_alias": "fixture",
            "model_path": "/fixture.gguf",
            "build_info": "fixture abcdef0",
            "is_sleeping": False,
        }
        for rss in (0, 1 << 50):
            child = mock.Mock(pid=4321)
            child.poll.return_value = None
            with (
                self.subTest(rss=rss),
                mock.patch.object(
                    local_llm.urllib.request, "urlopen", self.transport(props)
                ),
                mock.patch.object(host_caps, "process_memory_bytes", return_value=rss),
                mock.patch.object(host_caps, "available_memory_bytes", return_value=0),
            ):
                result = local_llm.wait_engine_ready(child, self.model, engine, "fixture-value")
            self.assertEqual(result["context_tokens"], 4096)
            self.assertEqual(result["resident_bytes"], rss or None)
            self.assertIsNone(result["available_host_bytes"])
            self.assertEqual(result["canary_outcome"], "content")

    def test_metadata_readiness_alone_never_publishes_ready(self):
        # The recorded incident: /props answered perfectly while Metal could no longer compute.
        # An engine that produces no content is a failed launch, not a healthy one.
        import host_caps

        self.model["install"]["port"] = 8181
        engine = {
            "context_tokens": 4096,
            "sequences": 1,
            "model_alias": "fixture",
            "model_path": "/fixture.gguf",
            "build_commit": "abcdef0",
        }
        props = {
            "default_generation_settings": {"n_ctx": 4096},
            "total_slots": 1,
            "model_alias": "fixture",
            "model_path": "/fixture.gguf",
            "build_info": "fixture abcdef0",
            "is_sleeping": False,
        }
        for canary in ("empty", "error", "reasoning"):
            child = mock.Mock(pid=4321)
            child.poll.return_value = None
            with (
                self.subTest(canary=canary),
                mock.patch.object(
                    local_llm.urllib.request, "urlopen", self.transport(props, canary=canary)
                ),
                mock.patch.object(host_caps, "process_memory_bytes", return_value=0),
                mock.patch.object(host_caps, "available_memory_bytes", return_value=0),
            ):
                with self.assertRaisesRegex(local_llm.EngineError, "canary"):
                    local_llm.wait_engine_ready(child, self.model, engine, "fixture-value")

    def test_the_canary_is_authenticated_and_asks_for_little(self):
        # A canary that asks for a long completion would itself be the memory spike that fails.
        canary = local_llm.LocalModelCanary("127.0.0.1", 8181, "fixture-value", self.model)
        request = canary.request()
        self.assertEqual(request.get_header("Authorization"), "Bearer fixture-value")
        payload = json.loads(request.data)
        self.assertLessEqual(payload["max_tokens"], 16)
        self.assertTrue(payload["stream"], "a non-streaming canary cannot observe an empty answer")

    def test_a_canary_answer_of_any_word_still_passes(self):
        # The canary proves compute works; it must not prove the quant reproduces one completion.
        self.model["install"]["port"] = 8181
        for text in ("ready", "Ready.", "I am ready and standing by"):
            with self.subTest(text=text):
                events = (
                    b'data: {"choices":[{"delta":{"content":"' + text.encode() + b'"}}]}\n\n'
                    b"data: [DONE]\n\n"
                )
                stream = _ChunkedStream(events)
                with mock.patch.object(
                    local_llm.urllib.request, "urlopen", return_value=stream
                ):
                    record = local_llm.LocalModelCanary(
                        "127.0.0.1", 8181, "fixture-value", self.model
                    ).run()
                self.assertEqual(record["outcome"], "content")

    def _engine_record(self):
        return {
            "context_tokens": 4096,
            "sequences": 1,
            "model_alias": "fixture",
            "model_path": "/fixture.gguf",
            "build_commit": "abcdef0",
        }

    def _props(self):
        return {
            "default_generation_settings": {"n_ctx": 4096},
            "total_slots": 1,
            "model_alias": "fixture",
            "model_path": "/fixture.gguf",
            "build_info": "fixture abcdef0",
            "is_sleeping": False,
        }

    def test_a_declared_tool_capability_is_proved_at_readiness(self):
        # Declaring tool_use without the template applied yields prose where the agent expected a
        # function call, which looks like a healthy completion at the transport layer.
        import host_caps

        self.model["install"]["port"] = 8181
        self.model["capabilities"] = ["thinking", "tool_use"]
        child = mock.Mock(pid=4321)
        child.poll.return_value = None
        with (
            mock.patch.object(
                local_llm.urllib.request, "urlopen", self.transport(self._props())
            ),
            mock.patch.object(host_caps, "process_memory_bytes", return_value=0),
            mock.patch.object(host_caps, "available_memory_bytes", return_value=0),
        ):
            result = local_llm.wait_engine_ready(
                child, self.model, self._engine_record(), "fixture-value"
            )
        self.assertEqual(result["canary_outcome"], "content")
        self.assertEqual(result["tool_canary"], "tool_only")

    def test_a_declared_tool_capability_fails_the_launch_when_prose_comes_back(self):
        import host_caps

        self.model["install"]["port"] = 8181
        self.model["capabilities"] = ["tool_use"]
        props = self._props()

        def urlopen(request, timeout=None):
            url = getattr(request, "full_url", str(request))
            if url.endswith("/props"):
                return io.BytesIO(json.dumps(props).encode())
            body = json.loads(getattr(request, "data", b"{}") or b"{}")
            if "tools" in body:
                # The shape a server returns when it accepted the tools array and parsed with its
                # built-in template: a perfectly successful completion with no call in it.
                return _ChunkedStream(
                    b'data: {"choices":[{"delta":{"content":"7"}}]}\n\ndata: [DONE]\n\n'
                )
            return _ChunkedStream(
                b'data: {"choices":[{"delta":{"content":"ready"}}]}\n\ndata: [DONE]\n\n'
            )

        child = mock.Mock(pid=4321)
        child.poll.return_value = None
        with (
            mock.patch.object(local_llm.urllib.request, "urlopen", mock.Mock(side_effect=urlopen)),
            mock.patch.object(host_caps, "process_memory_bytes", return_value=0),
            mock.patch.object(host_caps, "available_memory_bytes", return_value=0),
        ):
            with self.assertRaisesRegex(local_llm.EngineError, "tool_use"):
                local_llm.wait_engine_ready(
                    child, self.model, self._engine_record(), "fixture-value"
                )

    def test_the_template_flag_follows_the_declared_capability(self):
        from tests.test_capacity import PROFILE

        self.write_lock()
        without = local_llm.engine_args(self.model, 8192, 2, "t", dict(PROFILE))
        declared = dict(self.model, capabilities=["thinking", "tool_use"])
        with_tools = local_llm.engine_args(declared, 8192, 2, "t", dict(PROFILE))
        self.assertNotIn("--jinja", without)
        self.assertIn("--jinja", with_tools)

    def test_the_canary_shares_the_proxys_vocabulary(self):
        # Two definitions of "usable" is the drift this change set exists to remove.
        source = (ROOT / "tools" / "local_llm.py").read_text(encoding="utf-8")
        self.assertIn("from local_engine_contract import", source)

    def test_stale_memory_sized_engine_plan_requires_relaunch(self):
        plan = {"schema_version": 2, "engines": {self.model["id"]: {}}}
        with self.assertRaisesRegex(local_llm.EngineError, "relaunch"):
            local_llm.lane_sizes(self.model, plan)

    def test_startup_failure_cleans_up_only_its_owned_child_and_never_publishes_ready(self):
        from tests.test_capacity import PROFILE

        self.write_lock()
        runtime = self.directory / "runtime"
        runtime.mkdir()
        plan = self.plan()
        plan["engines"] = {
            self.model["id"]: {
                "context_tokens": 4096,
                "sequences": 2,
                "total_context_tokens": 8192,
                "allocation": PROFILE,
                # Every field a schema-4 plan carries for an engine. A fixture that omits them is
                # describing a plan the resolver cannot emit.
                "engine_kind": "llama_cpp",
                "serving_profile": "default",
                "protocol_version": 1,
                "capacity_strategy": "native_context",
                "memory_management": "os_managed",
                "native_context_tokens": 4096,
                "artifact_format": "gguf",
                "serving_profile_id": "f" * 64,
                "fingerprint": "e" * 64,
                "snapshot_id": "d" * 32,
            }
        }
        (runtime / "model-policy.json").write_text(json.dumps(plan))
        for occupied in (False, True):
            child = mock.Mock(pid=4321)
            child.poll.return_value = None
            with (
                self.subTest(occupied=occupied),
                mock.patch.dict(os.environ, {"HARNESS_RUNTIME_DIR": str(runtime)}),
                mock.patch.object(local_llm, "require_model", return_value=self.model),
                mock.patch.object(local_llm, "is_ready", return_value=True),
                mock.patch.object(local_llm, "validate_launch"),
                mock.patch.object(local_llm, "capacity_adapter", _stub_adapter),
                mock.patch.object(local_llm, "api_token", return_value="fixture-value"),
                mock.patch.object(local_llm, "engine_args", return_value=["fixture-binary"]),
                mock.patch.object(local_llm, "probe_port", return_value=occupied),
                mock.patch.object(local_llm.signal, "signal"),
                mock.patch.object(local_llm.subprocess, "Popen", return_value=child) as spawn,
                mock.patch.object(
                    local_llm,
                    "wait_engine_ready",
                    side_effect=local_llm.EngineError("fixture mismatch"),
                ),
                self.assertRaises(local_llm.EngineError),
            ):
                local_llm.cmd_serve([self.model["id"]])
            if occupied:
                spawn.assert_not_called()
                child.terminate.assert_not_called()
            else:
                spawn.assert_called_once()
                child.terminate.assert_called_once()
            self.assertFalse(local_llm.readiness_path(runtime, self.model["id"]).exists())

    def test_missing_resolved_local_definition_prevents_engine_enumeration(self):
        runtime = self.directory / "runtime"
        runtime.mkdir()
        (runtime / "model-policy.json").write_text(
            json.dumps(
                {
                    "lanes": {"primary": {"model_id": "local_fixture/removed"}},
                    "engines": {"local_fixture/removed": {}},
                }
            )
        )
        with (
            mock.patch.object(local_llm, "all_models", return_value={}),
            self.assertRaisesRegex(local_llm.EngineError, "missing"),
        ):
            local_llm.selected_models(runtime)

    def test_engine_credential_uses_the_plans_resolved_scope(self):
        runtime = self.directory / "runtime"
        (runtime / "credentials").mkdir(parents=True)
        (runtime / "credentials" / "local_fixture__model_scoped").write_text("fixture-value")
        plan = {
            "lanes": {
                "primary": {
                    "model_id": self.model["id"],
                    "provider": "local_fixture",
                    "credential": "scoped",
                }
            },
            "providers": {
                "local_fixture": {
                    "credentials": {
                        "scoped": {"secret_name": "local_fixture__model_scoped"},
                    }
                }
            },
        }
        with mock.patch.dict(os.environ, {"HARNESS_RUNTIME_DIR": str(runtime)}):
            self.assertEqual(local_llm.api_token(self.model, plan), "fixture-value")

    def test_properties_attest_context_slots_model_and_build(self):
        engine = {
            "context_tokens": 4096,
            "sequences": 2,
            "model_alias": "fixture",
            "model_path": "/fixture/model.gguf",
            "build_commit": "abcdef0",
        }
        props = {
            "default_generation_settings": {"n_ctx": 4096},
            "total_slots": 2,
            "model_alias": "fixture",
            "model_path": "/fixture/model.gguf",
            "build_info": "b1-abcdef0",
            "is_sleeping": False,
        }
        local_llm.validate_properties(props, engine)
        for field, value in (
            ("total_slots", 1),
            ("model_alias", "other"),
            ("build_info", "b2-1234567"),
            ("is_sleeping", True),
            ("default_generation_settings", []),
            ("total_slots", 2.0),
        ):
            with self.subTest(field=field), self.assertRaises(local_llm.EngineError):
                local_llm.validate_properties(dict(props, **{field: value}), engine)

    def test_projection_and_server_share_the_allocation_arguments(self):
        from tests.test_capacity import PROFILE

        projection = local_llm.allocation_args(PROFILE, 12288, 3, server=False)
        server = local_llm.allocation_args(PROFILE, 12288, 3, server=True)
        self.assertEqual(server[: len(projection)], projection)
        self.assertEqual(
            server[len(projection) :],
            [
                "--no-kv-unified",
                "--cache-ram",
                "0",
                "--ctx-checkpoints",
                "0",
            ],
        )
        self.assertIn("--offline", projection)
        self.assertEqual(projection[projection.index("--ctx-size") + 1], "12288")

    def test_readiness_receipt_is_bound_to_this_resolution_and_owned_supervisor(self):
        runtime = self.directory / "runtime"
        runtime.mkdir()
        engine = {"fingerprint": "current", "snapshot_id": "new-resolution"}
        (runtime / "model-policy.json").write_text(
            json.dumps(
                {
                    "engines": {self.model["id"]: engine},
                }
            )
        )
        receipt = local_llm.readiness_path(runtime, self.model["id"])
        receipt.parent.mkdir()
        receipt.write_text(
            json.dumps(
                {
                    "fingerprint": "stale",
                    "snapshot_id": "old-resolution",
                    "supervisor_pid": 123,
                }
            )
        )

        def publish(_delay):
            receipt.write_text(json.dumps(dict(engine, supervisor_pid=123)))

        with (
            mock.patch.dict(os.environ, {"HARNESS_RUNTIME_DIR": str(runtime)}),
            mock.patch.object(local_llm.os, "kill"),
            mock.patch.object(local_llm.time, "sleep", side_effect=publish) as sleep,
        ):
            self.assertEqual(local_llm.cmd_await_ready([self.model["id"]], 123), 0)
            sleep.assert_called_once()

    def plan(self):
        return {
            "lanes": {
                "primary": {
                    "model_id": "local_fixture/local_model",
                    "reservation": 28672,
                    "counters": ["count:model:local_fixture/Model"],
                },
                "subagent": {
                    "model_id": "local_fixture/local_model",
                    "reservation": 12288,
                    "counters": ["count:model:local_fixture/Model"],
                },
            },
            "counters": {
                "count:model:local_fixture/Model": {"family": "count", "max": 2},
            },
        }

    def test_the_engine_is_sized_from_the_plan_not_from_a_second_declaration(self):
        context, parallel = local_llm.lane_sizes(self.model, self.plan())
        # The widest lane, and the tightest count ceiling bound to it: the proxy admits requests
        # against the same two numbers, so the engine cannot be smaller than what gets through.
        self.assertEqual((context, parallel), (57344, 2))

    def test_managed_engine_allocation_names_total_context_for_full_slots(self):
        plan = self.plan()
        plan["engines"] = {
            self.model["id"]: {
                "context_tokens": 4096,
                "sequences": 3,
                "total_context_tokens": 12288,
            }
        }
        self.assertEqual(local_llm.lane_sizes(self.model, plan), (12288, 3))

    def test_managed_engine_inconsistent_total_fails_closed(self):
        plan = self.plan()
        plan["engines"] = {
            self.model["id"]: {
                "context_tokens": 4096,
                "sequences": 3,
                "total_context_tokens": 4096,
            }
        }
        with self.assertRaises(local_llm.EngineError):
            local_llm.lane_sizes(self.model, plan)

    def test_inherited_llama_settings_cannot_override_the_plan(self):
        with mock.patch.dict(os.environ, {"LLAMA_ARG_CTX_SIZE": "999", "KEEP_FIXTURE": "yes"}):
            environment = local_llm.engine_environment()
        self.assertNotIn("LLAMA_ARG_CTX_SIZE", environment)
        self.assertEqual(environment["KEEP_FIXTURE"], "yes")

    def test_a_model_in_no_counter_gets_one_slot_rather_than_none(self):
        plan = self.plan()
        plan["lanes"]["primary"]["counters"] = []
        plan["lanes"]["subagent"]["counters"] = []
        self.assertEqual(local_llm.lane_sizes(self.model, plan)[1], 1)

    def test_the_command_line_names_the_alias_the_proxy_will_send(self):
        self.hub()
        local_llm.install(self.model)
        with mock.patch.dict(os.environ, {"LOCAL_MPS_LLAMA_SERVER": str(self.fake_engine())}):
            args = local_llm.engine_args(self.model, 28672, 2, "session-token")
        pairs = dict(zip(args, args[1:], strict=False))
        self.assertEqual(pairs["--alias"], "fixture-model")
        # The fixture declares no port, so the provider's documented one stands.
        self.assertEqual(pairs["--port"], "8181")
        self.assertEqual(pairs["--ctx-size"], "28672")
        self.assertEqual(pairs["--parallel"], "2")
        self.assertEqual(pairs["--api-key"], "session-token")
        self.assertEqual(pairs["--host"], "0.0.0.0")  # noqa: S104 - the shipped default, asserted as such
        self.assertIn("--no-webui", args)
        # The first shard, not the last: llama.cpp finds the rest by naming pattern.
        self.assertIn("-00001-of-00002.gguf", pairs["--model"])

    def test_a_declared_port_and_the_vendors_sampler_reach_the_engine(self):
        self.hub()
        self.model["install"] = {
            "variant": "Q4",
            "lock": "weights.lock.json",
            "port": 8182,
            "sampling": {"temperature": 1.0, "top_p": 0.95, "top_k": 20.0, "min_p": 0.0},
        }
        with mock.patch.dict(os.environ, {"LOCAL_MPS_LLAMA_SERVER": str(self.fake_engine())}):
            args = local_llm.engine_args(self.model, 1024, 1, "t")
        pairs = dict(zip(args, args[1:], strict=False))
        self.assertEqual(pairs["--port"], "8182")
        self.assertEqual(pairs["--top-k"], "20")
        self.assertEqual(pairs["--temp"], "1")
        self.assertEqual(pairs["--top-p"], "0.95")
        self.assertEqual(pairs["--min-p"], "0")

    def test_a_narrow_bind_is_the_operators_to_choose(self):
        self.hub()
        self.model["install"] = {
            "variant": "Q4",
            "lock": "weights.lock.json",
            "port": 8181,
            "sampling": {},
        }
        with mock.patch.dict(
            os.environ,
            {"LOCAL_MPS_LLAMA_SERVER": str(self.fake_engine()), "LOCAL_MPS_BIND": "192.168.65.1"},
        ):
            args = local_llm.engine_args(self.model, 1024, 1, "t")
        self.assertEqual(args[args.index("--host") + 1], "192.168.65.1")
