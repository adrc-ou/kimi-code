"""Fetching and unpacking the pinned standalone interpreter.

``provision()`` itself is gated to Apple silicon and cannot run here, but ``_fetch_interpreter()``
is not: it is download, digest, and unpack over a URL, which is where the failure modes worth
catching live - a truncated archive, a pin that does not match what was published, and a cache
directory that outlives the attempt that wrote it. Served over real HTTP from a fixture rather than
stubbed, because a stubbed ``urlopen`` would smooth over exactly the half-written body that matters.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import shutil
import sys
import tarfile
import tempfile
import threading
import unittest
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from tests.helpers import load_script  # noqa: E402

RUNTIME = load_script("local_mlx_runtime", Path("tools/local_mlx_runtime.py"))


def interpreter_archive(version: str = "3.12.11") -> bytes:
    """One python-build-standalone-shaped tree: ``bin/python3`` addressed by ``lib/``'s runpath."""
    bundle = io.BytesIO()
    with tarfile.open(fileobj=bundle, mode="w:gz") as archive:
        for name, payload, mode in (
            ("bin/python3", b"\x7fELF fake interpreter", 0o755),
            ("lib/libpython3.so", b"shared object", 0o644),
        ):
            info = tarfile.TarInfo(name)
            blob = payload + version.encode()
            info.size = len(blob)
            info.mode = mode
            archive.addfile(info, io.BytesIO(blob))
    return bundle.getvalue()


class Hub:
    """A one-path origin that counts requests, and can lie about the bytes it holds."""

    def __init__(self, payload: bytes):
        self.payload = payload
        self.hits = 0
        self.truncate = False
        hub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                return

            def do_GET(self):  # noqa: N802 - required by BaseHTTPRequestHandler
                hub.hits += 1
                body = hub.payload[: len(hub.payload) // 2] if hub.truncate else hub.payload
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}/cpython.tar.gz"

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def pin_for(hub: Hub, payload: bytes, version: str = "3.12.11") -> dict[str, str]:
    # ``interpreter_pin()`` strips the ``sha256:`` prefix, and the comparison in
    # ``_fetch_interpreter`` is against the bare hex. Shaped to match what it really returns.
    return {
        "version": version,
        "url": hub.base,
        "digest": hashlib.sha256(payload).hexdigest(),
    }


class FetchInterpreterTests(unittest.TestCase):
    def setUp(self):
        self.cache = Path(tempfile.mkdtemp()) / "cache"
        self.payload = interpreter_archive()

    def _fetch(self, hub: Hub, version: str = "3.12.11") -> Path:
        return RUNTIME._fetch_interpreter(
            self.cache, pin_for(hub, hub.payload, version)
        )

    def test_a_matching_archive_unpacks_a_usable_tree(self):
        hub = Hub(self.payload)
        self.addCleanup(hub.stop)
        target = self._fetch(hub)
        self.assertTrue((target / "bin" / "python3").is_file())
        self.assertTrue((target / "lib" / "libpython3.so").is_file())
        self.assertFalse(list(self.cache.glob(".*extracting*")), "a staging tree outlived the run")

    def test_a_digest_mismatch_deletes_the_archive_and_installs_nothing(self):
        hub = Hub(self.payload)
        self.addCleanup(hub.stop)
        pin = pin_for(hub, self.payload)
        pin["digest"] = "0" * 64
        with self.assertRaises(RUNTIME.ProvisionError):
            RUNTIME._fetch_interpreter(self.cache, pin)
        self.assertEqual(list(self.cache.glob("*.tar.gz")), [], "the wrong bytes were left behind")
        self.assertFalse([p for p in self.cache.rglob("python3")])

    def test_a_short_body_never_becomes_an_unpackable_cache_entry(self):
        hub = Hub(self.payload)
        hub.truncate = True
        self.addCleanup(hub.stop)
        with self.assertRaises((RUNTIME.ProvisionError, tarfile.TarError, urllib.error.URLError)):
            self._fetch(hub)
        # Whatever the failure, the cache may not keep a tree that looks complete.
        self.assertFalse([p for p in self.cache.rglob("bin/python3")])

    def test_the_second_call_reuses_the_cache_and_asks_for_nothing(self):
        # The whole point of keeping the cache outside the staging tree. Inside it the directory was
        # deleted at the start of every attempt, so this branch could never be reached and the
        # digest check in front of the download was permanently false.
        hub = Hub(self.payload)
        self.addCleanup(hub.stop)
        self._fetch(hub)
        self.assertEqual(hub.hits, 1)
        self._fetch(hub)
        self.assertEqual(hub.hits, 1, f"a cached interpreter was fetched again ({hub.hits})")

    def test_a_different_pinned_version_does_not_reuse_an_older_tree(self):
        hub = Hub(self.payload)
        self.addCleanup(hub.stop)
        first = self._fetch(hub, version="3.12.11")
        # A version bump publishes different bytes under a different pin. The cache must key on it:
        # one shared "interpreter" directory would let the old tree satisfy the new pin, because the
        # reuse test above is exactly "if bin/python3 is there, do not unpack again".
        newer = interpreter_archive("3.13.1")
        hub.payload = newer
        target = self._fetch(hub, version="3.13.1")
        self.assertNotEqual(first, target)
        self.assertIn(b"3.12.11", (first / "bin" / "python3").read_bytes())
        self.assertIn(b"3.13.1", (target / "bin" / "python3").read_bytes())

    def test_an_archive_without_an_interpreter_is_refused_not_published(self):
        empty = io.BytesIO()
        with tarfile.open(fileobj=empty, mode="w:gz") as archive:
            info = tarfile.TarInfo("README.txt")
            info.size = 4
            archive.addfile(info, io.BytesIO(b"note"))
        hub = Hub(empty.getvalue())
        self.addCleanup(hub.stop)
        with self.assertRaises(RUNTIME.ProvisionError):
            self._fetch(hub)
        self.assertFalse(list(self.cache.rglob("bin")))
        self.assertFalse(list(self.cache.glob(".*extracting*")))


class RequirementsLockTrustTests(unittest.TestCase):
    """Provisioning must refuse a lock the repository does not describe.

    ``provision()`` cannot run on this host, but this gate can, and it is the one AGENTS.md makes
    load-bearing: MLX dependencies are installed on the operator's machine, outside any image and
    any package manager CI has, so a lock edited in place would otherwise install bytes no review
    ever saw - successfully.
    """

    LOCK_BODY = "mlx==0.29.0 \\\n    --hash=sha256:" + "a" * 64 + "\n"
    FILE_DIGEST = "sha256:" + hashlib.sha256(LOCK_BODY.encode()).hexdigest()

    def tree(self, recorded: str | None) -> Path:
        """A ROOT-shaped tree: the lock on disk, and what the dependency lock records."""
        root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        lock = root / RUNTIME.REQUIREMENTS_LOCK
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text(self.LOCK_BODY, encoding="utf-8")
        files = {} if recorded is None else {RUNTIME.REQUIREMENTS_LOCK: recorded}
        (root / RUNTIME.DEPENDENCIES_LOCK).write_text(
            json.dumps({"files": files}), encoding="utf-8"
        )
        return root

    def test_a_lock_matching_the_recorded_digest_is_accepted(self):
        root = self.tree(self.FILE_DIGEST)
        with mock.patch.object(RUNTIME, "ROOT", root):
            self.assertEqual(RUNTIME.requirements_lock_digest(), self.FILE_DIGEST)

    def test_an_edited_lock_is_refused_and_names_both_digests(self):
        root = self.tree("sha256:" + "b" * 64)
        with (
            mock.patch.object(RUNTIME, "ROOT", root),
            self.assertRaises(RUNTIME.ProvisionError) as caught,
        ):
            RUNTIME.requirements_lock_digest()
        message = str(caught.exception)
        # Both numbers, so the operator can tell "I edited the lock" from "I forgot to
        # re-record it".
        self.assertIn("b" * 64, message)
        self.assertIn(self.FILE_DIGEST, message)

    def test_a_lock_the_dependency_lock_stopped_recording_is_refused_not_assumed(self):
        # The fail-open this closes: skipping the comparison when the entry is absent means deleting
        # one line from dependencies.lock.json switches the gate off in silence.
        root = self.tree(None)
        with (
            mock.patch.object(RUNTIME, "ROOT", root),
            self.assertRaises(RUNTIME.ProvisionError) as caught,
        ):
            RUNTIME.requirements_lock_digest()
        self.assertIn("records no digest", str(caught.exception))

    def test_a_missing_lock_file_is_refused_before_anything_is_installed(self):
        root = self.tree("sha256:" + "c" * 64)
        (root / RUNTIME.REQUIREMENTS_LOCK).unlink()
        with (
            mock.patch.object(RUNTIME, "ROOT", root),
            self.assertRaises(RUNTIME.ProvisionError),
        ):
            RUNTIME.requirements_lock_digest()


class InstallerCommandTests(unittest.TestCase):
    """What pip is *told* is the whole safety property of a host-side install.

    ``run`` is intercepted rather than executed: the assertion is about the flags, and installing a
    real MLX tree inside a test would neither be faster nor mean anything different.
    """

    PYTHON = Path(sys.executable)
    ENVIRONMENT = Path(tempfile.gettempdir()) / "mlx-env-fixture"

    def _command(self, run: mock.Mock) -> list[str]:
        with mock.patch.object(RUNTIME.subprocess, "run", run) as calls:
            RUNTIME._install_dependencies(self.PYTHON, self.ENVIRONMENT)
        return calls.call_args[0][0]

    def test_the_installer_is_hash_pinned_binary_only_and_from_one_index(self):
        command = self._command(mock.Mock())
        expected = {
            "--require-hashes": None,
            "--only-binary=:all:": None,
            "--no-cache-dir": None,
            "--disable-pip-version-check": None,
            "--index-url": "https://pypi.org/simple",
            "--target": str(self.ENVIRONMENT / "site-packages"),
            "--requirement": str(RUNTIME.ROOT / RUNTIME.REQUIREMENTS_LOCK),
        }
        for flag, value in expected.items():
            self.assertIn(flag, command, f"{flag} is missing from {command}")
            if value is not None:
                self.assertEqual(command[command.index(flag) + 1], value)
        # --no-deps would let a resolver choose versions the lock never pinned, alongside
        # --require-hashes or not.
        self.assertNotIn("--no-deps", command)

    def test_a_failing_install_becomes_a_provision_error_carrying_the_reason(self):
        failure = RUNTIME.subprocess.CalledProcessError(
            1, ["pip"], output="ignored", stderr="THE REAL REASON: hash mismatch"
        )
        with (
            mock.patch.object(RUNTIME.subprocess, "run", side_effect=failure),
            self.assertRaises(RUNTIME.ProvisionError) as caught,
        ):
            RUNTIME._install_dependencies(self.PYTHON, self.ENVIRONMENT)
        self.assertIn("THE REAL REASON", str(caught.exception))

    def test_a_missing_interpreter_is_reported_as_a_provision_failure(self):
        with (
            mock.patch.object(RUNTIME.subprocess, "run", side_effect=OSError("no such file")),
            self.assertRaises(RUNTIME.ProvisionError) as caught,
        ):
            RUNTIME._install_dependencies(self.PYTHON, self.ENVIRONMENT)
        self.assertIn("could not run the installer", str(caught.exception))


class CacheLocationTests(unittest.TestCase):
    """Where the cache lives is the bug this file exists to keep closed.

    ``provision()`` is gated to Apple silicon and cannot be executed here, so the call-site half of
    the fix is asserted structurally. That is deliberate rather than convenient: the fact under test
    is which directory ``provision`` hands the fetcher, and the defect was that the value was a
    subtree ``provision`` deletes two lines earlier - a property of the call site that no amount of
    executing ``_fetch_interpreter`` on its own could observe.
    """

    SOURCE = Path(RUNTIME.__file__).read_text(encoding="utf-8")

    def provision_body(self) -> str:
        body = self.SOURCE.split("def provision(", 1)[1]
        return body.split("\ndef ", 1)[0]

    def test_provision_fetches_into_the_persistent_cache_not_the_staging_tree(self):
        body = self.provision_body()
        match = re.search(r"_fetch_interpreter\((.+?),\s*pin\s*\)", body, re.DOTALL)
        self.assertIsNotNone(match, "provision() no longer calls _fetch_interpreter at all")
        argument = match.group(1).strip()
        self.assertEqual(
            argument,
            "download_cache()",
            "the interpreter is being fetched into "
            f"{argument!r}; anything under the staging tree is deleted at the start of every "
            "attempt, so the cache can never be reused and a successful run publishes a second "
            "copy of the interpreter inside the runtime",
        )
        self.assertNotIn("staging / \"_interpreter\"", body)

    def test_the_cache_is_not_inside_a_published_runtime_or_its_staging_tree(self):
        cache = RUNTIME.download_cache()
        root = RUNTIME.runtime_root()
        self.assertFalse(
            str(cache).startswith(str(root) + "/"),
            "a cache under the runtime root sits beside identity directories that every reader "
            "expects to be runtimes",
        )
        self.assertEqual(cache.parent, root.parent)


if __name__ == "__main__":
    unittest.main()
