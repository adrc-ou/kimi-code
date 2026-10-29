#!/usr/bin/env python3
"""Provision the private native runtime an MLX engine runs in.

The harness has never installed a Python package before. It downloads a *binary* — the pinned
``llama-server`` release — verifies one SHA-256, and executes it. An MLX worker is different: it is
Python code that imports MLX, so it needs an interpreter and a site-packages tree on the operator's
Mac, and neither the operator's own ``python3`` (which must not be touched) nor a Linux container
(which has no Metal) can supply them.

So this module assembles one, under ``${HARNESS_ROOT}/.local/runtimes/mlx/<identity>/``, and the
identity is a digest of exactly the three things that determine what a correct runtime is: the
interpreter version, the platform, and the dependency lock. A directory by that name either is
complete and import-validated, or does not exist. There is no third state, because a half-installed
environment that reports itself ready is the failure this file exists to make impossible.

Atomicity is the whole design:

* the runtime is built in a sibling ``.<identity>.provisioning`` directory and moved into place
  only after it imports, so a killed download leaves nothing that looks installed;
* ``--require-hashes`` is passed to the installer, so a dependency is accepted only by the digest
  this repository committed, not by whatever the index served today;
* an existing complete runtime is never modified in place — a new lock means a new directory, and
  the old one survives for the launch that still names it.

Nothing here imports MLX or touches a model. Weights are a separate concern, digested by
:mod:`local_llm`, and a runtime with no weights is still a valid runtime.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(ROOT / "tools"))

DEPENDENCIES_LOCK = "dependencies.lock.json"
REQUIREMENTS_LOCK = "providers/local_mps/requirements-mlx.lock"
INTERPRETER_DOWNLOAD = "cpython-macos-aarch64"
#: Marker written inside a completed runtime. Its presence *and* its recorded identity are the only
#: evidence that an environment is usable; a directory full of site-packages proves less.
COMPLETE_FILE = "provisioned.json"
CHUNK = 1024 * 1024


class ProvisionError(RuntimeError):
    """A native runtime could not be assembled, or cannot be proven to have been."""


def runtime_root() -> Path:
    """Where completed runtimes live. Under ``.local``, because this is not project source."""
    return ROOT / ".local" / "runtimes" / "mlx"


def _digest_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(CHUNK), b""):
            digest.update(block)
    return digest.hexdigest()


def _dependencies() -> dict[str, Any]:
    try:
        return json.loads((ROOT / DEPENDENCIES_LOCK).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ProvisionError(f"cannot read {DEPENDENCIES_LOCK}: {exc}") from exc


def interpreter_pin() -> dict[str, str]:
    entry = (_dependencies().get("downloads") or {}).get(INTERPRETER_DOWNLOAD)
    if not isinstance(entry, dict) or not entry.get("url") or not entry.get("digest"):
        raise ProvisionError(f"{DEPENDENCIES_LOCK} names no {INTERPRETER_DOWNLOAD} download")
    return {
        "url": str(entry["url"]),
        "digest": str(entry["digest"]).removeprefix("sha256:"),
        "version": str(entry.get("version") or ""),
    }


def requirements_lock_digest() -> str:
    path = ROOT / REQUIREMENTS_LOCK
    if not path.is_file():
        raise ProvisionError(f"{REQUIREMENTS_LOCK} is missing")
    # Cross-checked against the committed digest before use: provisioning from a lock an editor
    # changed without updating the dependency lock would install bytes the repository does not
    # describe, and do so successfully.
    expected = (_dependencies().get("files") or {}).get(REQUIREMENTS_LOCK, "")
    digest = "sha256:" + _digest_of(path)
    if expected and digest != expected:
        raise ProvisionError(
            f"{REQUIREMENTS_LOCK} is {digest}, {DEPENDENCIES_LOCK} says {expected}. "
            "Re-run the uv command at the top of the lock, or revert the edit."
        )
    return digest


def runtime_identity(python_version: str, lock_digest: str) -> str:
    """The name of a runtime, computed from everything that decides its contents.

    Platform is in the digest on purpose. Two operators on different machines must never share a
    directory whose name implies interchangeable contents, and an arm64 macOS tree is not
    interchangeable with anything else.
    """
    material = json.dumps(
        {
            "python": python_version,
            "lock": lock_digest,
            "platform": f"{platform.system()}-{platform.machine()}",
        },
        sort_keys=True,
    ).encode()
    return hashlib.sha256(material).hexdigest()[:16]


def current_identity() -> tuple[str, str, str]:
    """(identity, interpreter version, lock digest) for this host and this lock."""
    pin = interpreter_pin()
    lock_digest = requirements_lock_digest()
    return runtime_identity(pin["version"], lock_digest), pin["version"], lock_digest


def staged(identity: str) -> dict[str, Any] | None:
    """The record of a completed runtime, or ``None`` when there is not one.

    A directory without the marker, or with a marker naming a different identity, is treated as
    absent. That is stricter than "does site-packages exist" and it is the point: an interrupted
    provision leaves a plausible-looking tree, and a plausible-looking tree must not be trusted.
    """
    directory = runtime_root() / identity
    marker = directory / COMPLETE_FILE
    if not marker.is_file():
        return None
    try:
        record = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict) or record.get("identity") != identity:
        return None
    if not Path(str(record.get("executable", ""))).is_file():
        return None
    return record


def _fetch_interpreter(cache: Path, pin: dict[str, str]) -> Path:
    """Download and verify the standalone build, then extract its interpreter tree."""
    archive = cache / f"cpython-{pin['version']}-aarch64-apple-darwin.tar.gz"
    archive.parent.mkdir(parents=True, exist_ok=True)
    if not archive.is_file() or _digest_of(archive) != pin["digest"]:
        archive.unlink(missing_ok=True)
        print(f"Fetching native interpreter: {pin['url']}", flush=True)
        request = urllib.request.Request(pin["url"], headers={"User-Agent": "kimi-code-harness/1"})
        with urllib.request.urlopen(request, timeout=300) as response:  # noqa: S310 - pinned host
            with archive.open("wb") as sink:
                shutil.copyfileobj(response, sink, CHUNK)
        if _digest_of(archive) != pin["digest"]:
            archive.unlink(missing_ok=True)
            raise ProvisionError(
                "native interpreter digest does not match the pinned release; the download was "
                "deleted and nothing was installed"
            )
    target = cache / "interpreter"
    if not (target / "bin" / "python3").is_file():
        with tarfile.open(archive, "r:gz") as bundle:
            _extract_safely(bundle, target)
    return target


def _extract_safely(bundle: tarfile.TarFile, into: Path) -> None:
    """Unpack an archive that this harness did not build, refusing members that write outside it."""
    members = list(bundle.getmembers())
    for member in members:
        name = Path(member.name)
        if name.is_absolute() or ".." in name.parts:
            raise ProvisionError(f"the interpreter archive writes outside its directory: {name}")
        if not (member.isdir() or member.isfile() or member.issym() or member.islnk()):
            raise ProvisionError(f"the interpreter archive holds an unusable member: {name}")
        if member.issym() or member.islnk():
            link = Path(member.linkname)
            if link.is_absolute() or ".." in (name.parent / link).parts:
                raise ProvisionError(
                    f"the interpreter archive links out of its directory: {name} -> {link}"
                )
    if hasattr(tarfile, "data_filter"):
        bundle.extractall(into, members=members, filter="data")  # noqa: S202 - vetted above
    else:
        bundle.extractall(into, members=members)  # noqa: S202 - vetted above


def _install_dependencies(python: Path, environment: Path) -> None:
    """Resolve the committed lock into the environment, by hash or not at all."""
    lock = ROOT / REQUIREMENTS_LOCK
    command = [
        str(python),
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        "--no-cache-dir",
        "--require-hashes",
        "--requirement",
        str(lock),
        "--target",
        str(environment / "site-packages"),
        "--only-binary=:all:",
        "--index-url",
        "https://pypi.org/simple",
    ]
    try:
        subprocess.run(command, check=True, capture_output=True, text=True, timeout=3600)
    except subprocess.CalledProcessError as exc:
        tail = (exc.stderr or exc.stdout or "")[-4000:]
        raise ProvisionError(
            f"dependency installation failed for {lock.name}: {tail}"
        ) from exc
    except (OSError, subprocess.SubprocessError) as exc:
        raise ProvisionError(f"could not run the installer: {exc}") from exc


def _validate_imports(python: Path, environment: Path) -> dict[str, str]:
    """Prove the tree loads, rather than that files were written.

    An ABI mismatch, a wheel for the wrong architecture, or a partially installed namespace all
    produce a directory that ``ls`` describes as complete. Importing the engine inside the
    interpreter that will run it is the cheapest check that cannot be satisfied by a good-looking
    failure.
    """
    probe = (
        "import json, os, sys;"
        "sys.path.insert(0, os.environ['MLX_PROVISION_SITE']);"
        "import mlx.core as mx, mlx_lm;"
        "print(json.dumps({"
        "'mlx': getattr(mx, '__version__', ''), "
        "'mlx_lm': getattr(mlx_lm, '__version__', ''), "
        "'metal': bool(mx.metal.is_available())}))"
    )
    environment_variables = dict(os.environ, MLX_PROVISION_SITE=str(environment / "site-packages"))
    try:
        result = subprocess.run(
            [str(python), "-c", probe],
            capture_output=True,
            text=True,
            check=True,
            timeout=120,
            env=environment_variables,
        )
    except subprocess.CalledProcessError as exc:
        raise ProvisionError(
            f"the provisioned runtime cannot import MLX: {(exc.stderr or '')[-2000:]}"
        ) from exc
    except (OSError, subprocess.SubprocessError) as exc:
        raise ProvisionError(f"the provisioned runtime could not be probed: {exc}") from exc
    try:
        return json.loads(result.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError) as exc:
        raise ProvisionError("the MLX import probe returned an unreadable response") from exc


def provision(*, install: bool = True) -> dict[str, Any]:
    """Return a usable runtime description, provisioning it first if ``install``.

    ``install=False`` answers the picker's question — could this be served on this host — without
    downloading a hundred megabytes to find out, and raises when the answer is "not yet".
    """
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise ProvisionError(
            "the MLX runtime can only be provisioned on Apple silicon macOS; this host is "
            f"{platform.system()}-{platform.machine()}"
        )
    identity, version, lock_digest = current_identity()
    existing = staged(identity)
    if existing is not None:
        return existing
    if not install:
        raise ProvisionError("the MLX runtime for this dependency lock is not provisioned")

    destination = runtime_root() / identity
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = destination.parent / f".{destination.name}.provisioning"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    try:
        pin = interpreter_pin()
        interpreter_tree = _fetch_interpreter(staging / "_interpreter", pin)
        environment = staging / "environment"
        environment.mkdir()
        python = environment / "bin" / "python3"
        shutil.copy2(interpreter_tree / "bin" / "python3", python)
        # The standalone build's lib/ is addressed by the binary's runpath, so the tree travels
        # together or the interpreter does not start.
        shutil.copytree(interpreter_tree / "lib", environment / "lib", dirs_exist_ok=True)
        _install_dependencies(python, environment)
        imports = _validate_imports(python, environment)
        if not imports.get("metal"):
            # Not fatal on a build host without a GPU, but it must be recorded: a runtime that
            # cannot see Metal will never answer the compute canary, and the reason belongs in the
            # marker rather than being discovered as an unexplained launch failure.
            print("warning: provisioned MLX runtime reports no Metal device", file=sys.stderr)
        record = {
            "identity": identity,
            "kind": "mlx_lm",
            "python_version": version,
            "lock_digest": lock_digest,
            "platform": f"{platform.system()}-{platform.machine()}",
            "executable": str((destination / "environment" / "bin" / "python3").resolve()),
            "directory": str(destination.resolve()),
            "imports": imports,
        }
        (staging / COMPLETE_FILE).write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
        (staging / COMPLETE_FILE).chmod(0o600)
        # The move is the commit point. Until this line nothing downstream can see any of it.
        if destination.exists():
            raise ProvisionError(f"runtime {identity} appeared during provisioning")
        staging.replace(destination)
    except BaseException:
        # An interrupted provision must not be recognisable as an installed runtime on a later
        # launch, so the staging tree is removed and the attempt is simply repeatable.
        shutil.rmtree(staging, ignore_errors=True)
        raise
    committed = staged(identity)
    if committed is None:
        raise ProvisionError(
            f"runtime {identity} was moved into place but is not marked complete"
        )
    return committed


def main(argv: list[str] | None = None) -> int:
    args = argv or sys.argv[1:]
    action = args[0] if args else "provision"
    if action == "identity":
        identity, version, lock_digest = current_identity()
        print(json.dumps({"identity": identity, "python": version, "lock": lock_digest}))
        return 0
    if action == "status":
        identity, _version, _lock = current_identity()
        record = staged(identity)
        print(
            json.dumps(
                {
                    "identity": identity,
                    "provisioned": record is not None,
                    "imports": (record or {}).get("imports"),
                }
            )
        )
        return 0 if record else 1
    record = provision(install=action != "check")
    print(json.dumps({"identity": record["identity"], "imports": record["imports"]}))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ProvisionError, OSError) as error:
        raise SystemExit(f"MLX runtime refused: {error}") from error
