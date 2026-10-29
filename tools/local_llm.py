#!/usr/bin/env python3
"""Install, verify, and serve the models that run on the operator's own machine.

A remote model is a URL and a key. A local one is a hundred gigabytes of weights, an inference
engine, and a process that has to be alive before the first request arrives - none of which the
launcher's existing machinery knows about. This module owns those three things and nothing else:

``state``   - is the weight set this definition pins actually present, and verified?
``install`` - download it from the pinned revision, digest every file, record the result.
``serve``   - start the engine the provider's endpoint points at, and keep it running.

Nothing here is imported by the proxy, and nothing here runs inside a container. The weights live
under ``models/<provider>/<model>/weights/`` in the harness checkout, which is never mounted into
a session, and the engine runs on the host because that is where the GPU is.

## Why the bytes are digested and the download is hand-rolled

The harness ships no dependency it has not pinned and verified, and a model weight set is the
largest dependency in the system. Each local model therefore carries a tracked ``weights.lock.json``
naming one commit on HuggingFace and the SHA-256 of every file it fetches, and the installer
refuses a byte that does not match. That is also why the download is plain HTTPS through
:mod:`urllib` rather than ``huggingface_hub``: the library installs packages, follows mutable
branches by default, and builds its cache out of symlinks - three things this repository has
rules against, and one of which (:func:`definitions._walk_safe`) would refuse to load the tree.

## Why "Ready" is a claim about verification, not about files

A directory of correctly named files of the right sizes is not evidence. ``Ready`` means the
installer hashed those bytes against the lock in this checkout, and said so in
``weights/install.json``. Weights copied in by hand are reported as not installed until
:command:`install` has read them once - which it will do without re-downloading anything, so the
wait is a hash and not a transfer.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(ROOT / "tools"))
# The shared local-engine vocabulary lives beside the proxy, because the proxy is its other reader.
# Importing across that boundary is deliberate and is the direction the plan names: one file, three
# readers (launcher, proxy, worker), so an outcome means the same thing in all of them.
if str(ROOT / "proxy") not in sys.path:
    sys.path.insert(0, str(ROOT / "proxy"))

import definitions  # noqa: E402
from private_file import write_private_json  # noqa: E402

from local_engine_contract import StreamEvidence, classify  # noqa: E402

#: Written beside the weights once every file has been digested and matched. It is the difference
#: between "the files are there" and "these are the bytes the lock promised", and it is the only
#: record of the second one.
STATE_FILE = "install.json"
READY = "Ready"
MISSING = "Not Installed"

HEX40 = re.compile(r"[0-9a-f]{40}\Z")
HEX64 = re.compile(r"[0-9a-f]{64}\Z")
#: One chunk per read: big enough to keep a 100 GB hash I/O-bound, small enough that a stalled
#: transfer is noticed promptly and a partially-read file never looks complete.
CHUNK = 1024 * 1024

HUB = "https://huggingface.co"


class LocalModelError(ValueError):
    """A local model cannot be installed, verified, or served as defined."""


# --------------------------------------------------------------------------------------------
# the lock
# --------------------------------------------------------------------------------------------


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise LocalModelError(message)


def read_lock(path: Path) -> dict[str, Any]:
    """Read and validate one ``weights.lock.json``.

    Strict about the things that decide whether a byte is trustworthy - the commit, the digest,
    the size, and the file name - and quiet about everything else, because this document is
    written by hand by whoever adds the model.
    """
    _require(path.is_file(), f"{path} is missing; a model with [install] must ship its lock")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise LocalModelError(f"cannot read {path}: {exc}") from exc
    _require(isinstance(document, dict), f"{path} must hold one JSON object")
    schema = document.get("schema_version")
    _require(
        schema in (1, 2),
        f"{path} must set schema_version = 1 or 2",
    )
    source = document.get("source")
    _require(isinstance(source, dict), f"{path} needs a source table")
    revision = str(source.get("revision", ""))
    _require(HEX40.match(revision) is not None, f"{path}.source.revision must be a 40-hex commit")
    base = str(source.get("base") or "")
    if base and not re.fullmatch(r"https?://[^\s/]+", base):
        raise LocalModelError(
            f"{path}.source.base must be an http(s) origin with no path: {base!r}"
        )
    files = document.get("files")
    _require(isinstance(files, list) and bool(files), f"{path}.files must list at least one file")
    seen: set[str] = set()
    for entry in files:
        _require(isinstance(entry, dict), f"{path}.files entries must be objects")
        name = str(entry.get("path", ""))
        _require(
            bool(name) and not name.startswith("/") and ".." not in Path(name).parts,
            f"{path}: file path {name!r} must be relative to the weights directory",
        )
        _require(name not in seen, f"{path}: {name!r} is listed twice")
        seen.add(name)
        _require(
            HEX64.match(str(entry.get("sha256", ""))) is not None,
            f"{path}: {name} needs a 64-hex sha256",
        )
        size = entry.get("size")
        _require(
            isinstance(size, int) and not isinstance(size, bool) and size > 0,
            f"{path}: {name} needs a positive size in bytes",
        )
    if schema == 2:
        # Schema 2 says *what* the bytes are, not only that they hash right. A GGUF and an MLX
        # directory can carry the same model at the same variant, and the loader that must be used
        # is not inferable from a filename any more.
        artifact = document.get("artifact")
        _require(isinstance(artifact, dict), f"{path} schema 2 needs an [artifact] table")
        if set(artifact) - {"format", "variant", "directory"}:
            raise LocalModelError(f"{path}.artifact has unknown key(s)")
        kind = artifact.get("format")
        _require(
            kind in {"gguf", "mlx"},
            f"{path}.artifact.format must be gguf or mlx",
        )
        variant = str(artifact.get("variant", ""))
        _require(bool(variant), f"{path}.artifact.variant must name the quantised build")
        directory = artifact.get("directory", "")
        _require(
            isinstance(directory, str)
            and not directory.startswith("/")
            and ".." not in Path(str(directory)).parts,
            f"{path}.artifact.directory must stay inside the weights directory",
        )
        _require(
            not any(Path(entry["path"]).is_absolute() for entry in files),
            f"{path}: schema 2 file paths stay relative to the weights directory",
        )
    return document


def download_url(source: dict[str, Any], name: str) -> str:
    """The resolve URL for one file at the pinned revision.

    Pinned by commit rather than branch on purpose: ``/main/`` would let the publisher change a
    hundred gigabytes underneath a definition that claims to describe it, and the digest is the
    only thing that would notice - after the download, not before it.

    ``source.base`` redirects the fetch to a mirror of the same repository layout. The digest is
    the invariant and the host is not, so a private mirror is trusted exactly as far as it returns
    bytes that hash right - and not one step further, because the hub token is only ever sent to
    :data:`HUB`.
    """
    base = str(source.get("base") or HUB).rstrip("/")
    return f"{base}/{source['repo']}/resolve/{source['revision']}/{name}"


# --------------------------------------------------------------------------------------------
# state
# --------------------------------------------------------------------------------------------


def weights_dir(model: dict[str, Any]) -> Path:
    """Where this model's verified bytes live.

    Schema-1 locks put the files directly under ``weights/``, which is how the GGUF trees already
    on disk are laid out, so that stays the default. Schema 2 may name a subdirectory, which is what
    lets an MLX artifact and a retained GGUF coexist under one model without either loader
    discovering the other's files.
    """
    base = Path(model["weights_dir"])
    artifact = artifact_of(model)
    directory = str((artifact or {}).get("directory") or "")
    if not directory:
        return base
    path = base / directory
    # Resolve-and-contain, not a string check: the lock is validated on read, but this is the one
    # place a path from it becomes a filesystem root for downloads and hashing.
    root = base.resolve()
    if not path.resolve().is_relative_to(root):
        raise LocalModelError(f"{model['where']}: artifact.directory escapes weights/")
    return path


def artifact_of(model: dict[str, Any]) -> dict[str, Any] | None:
    """The ``[artifact]`` block of this model's lock, or ``None`` for a schema-1 lock.

    Read through :func:`read_lock` rather than cached on the model, so a lock edited after
    discovery cannot disagree with the bytes the installer hashes.
    """
    path = model.get("lock_path")
    if not path:
        return None
    try:
        lock = read_lock(Path(path))
    except LocalModelError:
        # Status reporting treats an unreadable lock as "not installed", and says so; it is not
        # this function's job to decide that twice.
        return None
    artifact = lock.get("artifact")
    return dict(artifact) if isinstance(artifact, dict) else None


def read_state(model: dict[str, Any]) -> dict[str, Any] | None:
    """The verified record written by :command:`install`, or ``None`` if there is none."""
    path = weights_dir(model) / STATE_FILE
    if not path.is_file():
        return None
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # A corrupt marker is an unverified weight set, not a broken launch: the installer will
        # read the files again and write it properly.
        return None
    return document if isinstance(document, dict) else None


def files_present(model: dict[str, Any], lock: dict[str, Any]) -> bool:
    """Whether every locked file exists at its locked size.

    Sizes only, never digests: this answers "should the picker say Ready" in a keystroke, on a
    tree that can hold a hundred gigabytes. The digest is what :command:`install` checks, once.
    """
    directory = weights_dir(model)
    return all(
        (directory / entry["path"]).is_file()
        and (directory / entry["path"]).stat().st_size == entry["size"]
        for entry in lock["files"]
    )


def verified(model: dict[str, Any], lock: dict[str, Any]) -> bool:
    """Whether the marker says these exact bytes were digested and matched.

    Every clause is a different way a stale marker could otherwise lie: a different commit, a
    different quantisation variant, a file list that no longer matches the lock, or a digest that
    stopped matching it.
    """
    state = read_state(model)
    if state is None:
        return False
    install = model.get("install") or {}
    if state.get("revision") != lock["source"]["revision"]:
        return False
    if install and state.get("variant") != install.get("variant"):
        return False
    # The artifact format is compared too. Two formats of one model share a variant name (the
    # publisher's quant label describes the weights, not the container), so without this a marker
    # written for a GGUF could vouch for an MLX directory of the same model at the same quant.
    expected_format = str((lock.get("artifact") or {}).get("format") or "gguf")
    if str(state.get("format") or "gguf") != expected_format:
        return False
    recorded = {
        entry.get("path"): entry.get("sha256")
        for entry in state.get("files", [])
        if isinstance(entry, dict)
    }
    expected = {entry["path"]: entry["sha256"] for entry in lock["files"]}
    return recorded == expected and files_present(model, lock)


def status_text(model: dict[str, Any]) -> str:
    """What the picker's ``Ready?`` column says about one model.

    A model with no ``[install]`` block is served by somebody else's datacenter and is always
    ready - that is not a guess, it is the rule :func:`definitions._require_local_serving`
    enforces by refusing an install block on a remote model in the first place.
    """
    if not model.get("install"):
        return READY
    path = model.get("lock_path")
    if not path:
        return MISSING
    try:
        lock = read_lock(Path(path))
    except LocalModelError:
        return MISSING
    return READY if verified(model, lock) else MISSING


def is_ready(model: dict[str, Any]) -> bool:
    return status_text(model) == READY


def total_bytes(lock: dict[str, Any]) -> int:
    return sum(int(entry["size"]) for entry in lock["files"])


def size_text(amount: int) -> str:
    """Bytes in the one short form a launch line has room for.

    Decimal, not binary: a "17.6 GB" file list is what the publisher and every vendor table on
    the internet call the same number, and a picker that quietly converts to GiB argues with the
    documentation the operator just read.
    """
    stepping = 1000.0
    value = float(amount)
    for unit in ("B", "kB", "MB", "GB", "TB"):
        if abs(value) < stepping or unit == "TB":
            return f"{value:.0f} B" if unit == "B" else f"{value:.1f} {unit}"
        value /= stepping
    return f"{value:.1f} TB"


# --------------------------------------------------------------------------------------------
# catalog
# --------------------------------------------------------------------------------------------


def all_models() -> dict[str, dict[str, Any]]:
    """Every locally-served definition in the tree, keyed by its qualified id."""
    _, models = definitions.load_definitions(ROOT)
    return {model["id"]: model for model in models if model.get("install")}


def selected_models(runtime: Path) -> list[dict[str, Any]]:
    """The local models this launch actually chose, without repeats.

    Read from the resolved plan rather than the selection document, because the plan is what says
    a lane exists at all: a model dropped by the host probe never reaches it, and a model chosen
    for two lanes must be installed and served once.
    """
    path = Path(runtime) / "model-policy.json"
    if not path.is_file():
        raise EngineError("local engine enumeration requires the resolved policy plan")
    try:
        plan = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise EngineError("the resolved policy plan is unreadable") from exc
    if not isinstance(plan, dict) or not isinstance(plan.get("lanes"), dict):
        raise EngineError("the resolved policy plan has no lane bindings")
    catalog = all_models()
    wanted: list[dict[str, Any]] = []
    for entry in (plan.get("lanes") or {}).values():
        model = catalog.get(str(entry.get("model_id", "")))
        if model is not None and model not in wanted:
            wanted.append(model)
    missing = set(plan.get("engines") or {}) - {model["id"] for model in wanted}
    if missing:
        raise EngineError(
            f"resolved local engine definitions are missing: {', '.join(sorted(missing))}"
        )
    return wanted


def lane_sizes(model: dict[str, Any], plan: dict[str, Any]) -> tuple[int, int]:
    """The context and slot count this model's engine has to serve, from the plan that priced it.

    Derived rather than declared on purpose. ``[install]`` states a port and nothing else, and a
    port beside a lane table that disagreed would mean an engine refusing requests the proxy had
    already admitted - the plan is the one document both sides are built from, so the widest lane's
    reservation is the context and the tightest count ceiling is the slot count.
    """
    engine = (plan.get("engines") or {}).get(model["id"])
    if engine is not None:
        if plan.get("schema_version") in (2, 3):
            raise EngineError(
                f"stale schema {plan.get('schema_version')} local policy; "
                "relaunch the harness for the current engine contract"
            )
        context, sequences = int(engine["context_tokens"]), int(engine["sequences"])
        if context <= 0 or sequences <= 0 or engine["total_context_tokens"] != context * sequences:
            raise EngineError("the resolved engine allocation is inconsistent")
        return context * sequences, sequences
    lanes = [
        entry
        for entry in (plan.get("lanes") or {}).values()
        if entry.get("model_id") == model["id"]
    ]
    context = max((int(entry["reservation"]) for entry in lanes), default=0)
    ceiling: int | None = None
    for entry in lanes:
        for counter_id in entry.get("counters") or ():
            counter = (plan.get("counters") or {}).get(counter_id) or {}
            if counter.get("family") == "count" and counter.get("max"):
                ceiling = counter["max"] if ceiling is None else min(ceiling, counter["max"])
    # The legacy static plan also names per-request reservations. llama.cpp's non-unified
    # context is a total shared between slots, so each promised slot needs a full reservation.
    sequences = int(ceiling or 1)
    return context * sequences, sequences


# --------------------------------------------------------------------------------------------
# the engine
# --------------------------------------------------------------------------------------------

#: Pinned host binaries live in the dependency lock beside the container images, under the same
#: "official upstream, digest verified" rule.
DEPENDENCIES_LOCK = "dependencies.lock.json"
ENGINE_NAME = "llama-cpp-macos-arm64"
ENGINE_BINARY = "llama-server"
DEFAULT_BIND = "0.0.0.0"  # noqa: S104 - see the bind note in start.sh: loopback is unreachable from the VM
#: The listener port a model that declares none is served on. One constant because three readers
#: (arg construction, the occupied-listener probe, and the launcher's ``check`` line) have to agree.
DEFAULT_PORT = 8181


def engine_host() -> str:
    """The address to *reach* the engine on, as distinct from the address it binds.

    The server binds every interface because a container cannot reach the host's loopback, but the
    launcher and the proxy connect over localhost. Deriving one from the other in three places is
    how a probe ends up pointed somewhere the engine never listened.
    """
    bind = os.environ.get("LOCAL_MPS_BIND", "").strip()
    return bind if bind and bind != DEFAULT_BIND else "127.0.0.1"


def engine_port(model: dict[str, Any]) -> int:
    return int((model.get("install") or {}).get("port") or DEFAULT_PORT)
REQUEST_TIMEOUT = 60.0


class EngineError(LocalModelError):
    """The inference engine is missing, unverifiable, or unusable."""


def engine_plan() -> dict[str, Any]:
    document = json.loads((ROOT / DEPENDENCIES_LOCK).read_text(encoding="utf-8"))
    entry = (document.get("downloads") or {}).get(ENGINE_NAME)
    if not isinstance(entry, dict) or not entry.get("url") or not entry.get("digest"):
        raise EngineError(f"{DEPENDENCIES_LOCK} names no {ENGINE_NAME} download")
    return entry


def engine_root() -> Path:
    """Where the runtime is staged. Under ``.local``, because it is not project source."""
    return ROOT / ".local" / "runtimes" / "llama-cpp"


def engine_binary(*, install: bool = True) -> Path:
    """The pinned ``llama-server``, extracted and verified, or :class:`EngineError`.

    ``LOCAL_MPS_LLAMA_SERVER`` short-circuits all of it for an operator who already has a build -
    a Homebrew install, or a branch compiled for something this harness does not pin. Trusting that
    path is deliberate: the operator is naming the binary, and the digest rule exists for binaries
    the harness fetches by itself.

    With ``install=False``, inspect existing installations only. Picker eligibility
    must not start a download just to discover whether a choice can fit.
    """
    override = os.environ.get("LOCAL_MPS_LLAMA_SERVER", "").strip()
    if override:
        path = Path(override)
        if not path.is_file() or not os.access(path, os.X_OK):
            raise EngineError(f"LOCAL_MPS_LLAMA_SERVER does not name a runnable file: {path}")
        return path
    entry = engine_plan()
    directory = engine_root() / str(entry["version"])
    found = next(iter(sorted(directory.rglob(ENGINE_BINARY))), None)
    if found is not None:
        return found
    if not install:
        raise EngineError("the pinned inference engine is not installed")
    fetch_engine(entry, directory)
    found = next(iter(sorted(directory.rglob(ENGINE_BINARY))), None)
    if found is None:
        raise EngineError(f"the {ENGINE_NAME} archive contained no {ENGINE_BINARY}")
    return found


def fetch_engine(entry: dict[str, Any], directory: Path) -> None:
    """Download, digest, and unpack the pinned engine archive.

    The digest is compared before anything is executed, so a re-tagged release is noticed rather
    than run. Unpacking goes into a sibling and is moved only once it completes, which is what
    keeps a killed fetch from leaving a half-extracted tree that :func:`engine_binary` would
    happily treat as the installed runtime.
    """
    url = str(entry["url"])
    expected = str(entry["digest"]).removeprefix("sha256:")
    archive = directory.parent / f"{Path(url).name}.{expected[:12]}.tgz"
    print(f"Fetching inference engine: {url}", flush=True)
    if not archive.is_file() or _digest_of(archive) != expected:
        archive.unlink(missing_ok=True)
        directory.parent.mkdir(parents=True, exist_ok=True)
        transfer(url, archive, None, "engine")
        got = _digest_of(archive)
        if got != expected:
            archive.unlink(missing_ok=True)
            raise EngineError(
                f"engine digest mismatch: {url} is {got}, {DEPENDENCIES_LOCK} says {expected}"
            )
    staging = directory.with_name(f".{directory.name}.unpacking")
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    with tarfile.open(archive, "r:gz") as bundle:
        extract_archive(bundle, staging)
    shutil.rmtree(directory, ignore_errors=True)
    staging.replace(directory)
    archive.unlink(missing_ok=True)


def extract_archive(bundle: Any, into: Path) -> None:
    """Unpack a fetched archive, refusing any member that would write outside it.

    Python 3.12 added ``filter="data"`` for exactly this and it is used when the interpreter has
    it - but the interpreter doing the download is the operator's own ``python3`` on the host, not
    the container's, so the name check runs either way. A release archive is third-party input the
    moment it is fetched.
    """
    members = list(bundle.getmembers())
    for member in members:
        name = Path(member.name)
        if name.is_absolute() or ".." in name.parts:
            raise EngineError(f"the engine archive writes outside its directory: {member.name!r}")
        if member.issym() or member.islnk():
            # Versioned dylib symlinks are how a macOS release is laid out - `libllama.0.dylib`
            # points at `libllama.0.7.0.dylib` - so they are kept, but only while they point
            # inside the tree: a link is a path the operator did not choose.
            target = Path(member.linkname)
            if target.is_absolute() or ".." in (name.parent / target).parts:
                raise EngineError(
                    f"the engine archive links out of its directory: "
                    f"{member.name!r} -> {member.linkname!r}"
                )
        elif not (member.isdir() or member.isfile()):
            raise EngineError(f"the engine archive contains an unusable member: {member.name!r}")
    if hasattr(tarfile, "data_filter"):
        bundle.extractall(into, members=members, filter="data")  # noqa: S202
    else:
        bundle.extractall(into, members=members)  # noqa: S202 - vetted above


def _digest_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(CHUNK), b""):
            digest.update(block)
    return digest.hexdigest()


# --------------------------------------------------------------------------------------------
# install
# --------------------------------------------------------------------------------------------


def bootstrap_token(name: str) -> str:
    """One value out of the launcher's resolved environment, read the way the launcher reads it."""
    from env_values import read_env_values

    configured = os.environ.get("HARNESS_RESOLVED_BOOTSTRAP", "")
    path = Path(configured) if configured else ROOT / ".env"
    if not path.is_file():
        return ""
    return read_env_values(path).get(name, "").strip()


def request(url: str, start: int = 0):
    """Open one download, with the hub token attached when the operator supplied one.

    The token goes to :data:`HUB` and nowhere else, and only as a header: a URL is the form that
    ends up in a log line, a shell history entry, or a failure dump.
    """
    req = urllib.request.Request(url, headers={"User-Agent": "kimi-code-harness/1"})
    if start:
        req.add_header("Range", f"bytes={start}-")
    if url.startswith(HUB):
        token = bootstrap_token("HF_TOKEN")
        if token:
            req.add_header("Authorization", f"Bearer {token}")
    return urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT)  # noqa: S310 - https host from lock


def transfer(url: str, target: Path, total: int | None, label: str) -> None:
    """Stream one URL into ``target``, resuming a ``.part`` that is already partly there.

    Appending after a ``Range`` request and hashing the *finished* file - rather than hashing while
    downloading - is what makes an interrupted transfer safe instead of merely efficient: the
    digest covers whatever ended up on disk, whatever mix of runs produced it.
    """
    part = target.with_name(target.name + ".part")
    part.parent.mkdir(parents=True, exist_ok=True)
    start = part.stat().st_size if part.is_file() else 0
    mode = "ab" if start else "wb"
    with request(url, start if mode == "ab" else 0) as response:
        if mode == "ab" and getattr(response, "status", 200) != 206:
            # The server sent the whole file rather than the tail, so the partial is worthless.
            start, mode = 0, "wb"
        length = total if total is not None else int(response.headers.get("Content-Length") or 0)
        done = start
        step = max(length // 40, 1)
        mark = step
        with part.open(mode) as sink:
            while True:
                block = response.read(CHUNK)
                if not block:
                    break
                sink.write(block)
                done += len(block)
                if length and done >= mark:
                    print(
                        f"  {label}: {size_text(done)} of {size_text(length)}",
                        flush=True,
                    )
                    mark += step
    if total is not None and done != total:
        # Leave the partial where it is: this is the one failure a later run resumes from, and
        # deleting it would turn a dropped connection into a re-download of a hundred gigabytes.
        raise LocalModelError(
            f"{target.name}: transferred {done} bytes, the lock says {total}. The transfer is "
            "incomplete and the file was not accepted."
        )
    # The rename is the statement that a file is *complete*: nothing downstream has to know a
    # partial exists beside it, and a digest is only ever taken of a name the lock could ask for.
    part.replace(target)


def verify(path: Path, expected: str) -> bool:
    return path.is_file() and _digest_of(path) == expected


def install(model: dict[str, Any], *, force: bool = False) -> bool:
    """Fetch and verify one model's weights. Returns whether it is ready afterwards.

    Every path through here ends in a digest comparison and nothing is recorded that has not been
    read once and matched. That is also what makes a hand-copied weight tree work: the file is
    hashed where it stands and only re-fetched if it is not the pinned bytes.
    """
    lock = read_lock(Path(model["lock_path"]))
    directory = weights_dir(model)
    directory.mkdir(parents=True, exist_ok=True)
    print(
        f"{model['where']}: {local_label(model)} - {len(lock['files'])} file(s), "
        f"{size_text(total_bytes(lock))} to verify",
        flush=True,
    )
    for entry in lock["files"]:
        target = directory / entry["path"]
        expected, size = str(entry["sha256"]), int(entry["size"])
        already = target.is_file() and target.stat().st_size == size
        if not force and already and verify(target, expected):
            print(f"  {entry['path']}: already present and verified", flush=True)
            continue
        if target.is_file():
            # Right name, wrong bytes: a truncated download or a re-uploaded file. Either way the
            # pinned digest is the authority and the file goes.
            print(f"  {entry['path']}: digest does not match the lock - refetching", flush=True)
            _discard(target)
        label = Path(entry["path"]).name
        print(f"  {label}: {size_text(size)} from {lock['source']['repo']}", flush=True)
        transfer(download_url(lock["source"], entry["path"]), target, size, label)
        if not verify(target, expected):
            _discard(target)
            raise LocalModelError(
                f"{entry['path']}: SHA-256 does not match {expected}. The download was deleted and "
                "nothing unverified is recorded as installed; check the pin in weights.lock.json "
                "against the repository before retrying."
            )
        _discard_part(target)
    record(model, lock)
    return True


def _discard_part(target: Path) -> None:
    """Remove only the partial twin, once the finished file has taken its name."""
    target.with_name(target.name + ".part").unlink(missing_ok=True)


def _discard(target: Path) -> None:
    """Remove a file and its partial twin.

    Both, because the branch that calls this has decided the bytes are wrong: a completed file at
    exactly the locked size is the one thing a later pass would least expect to be lying, so a bad
    digest leaves nothing behind."""
    target.with_name(target.name + ".part").unlink(missing_ok=True)
    target.unlink(missing_ok=True)


def local_label(model: dict[str, Any]) -> str:
    return f"{model['label']} ({(model.get('install') or {}).get('variant', '?')})"


def record(model: dict[str, Any], lock: dict[str, Any]) -> None:
    """Write the marker :func:`verified` reads: these bytes, this commit, this variant, digested."""
    payload = {
        "schema_version": 1,
        "revision": lock["source"]["revision"],
        "variant": (model.get("install") or {}).get("variant", ""),
        # Recorded so a later format change invalidates this marker instead of inheriting it.
        "format": str((lock.get("artifact") or {}).get("format") or "gguf"),
        "artifact": lock.get("artifact") or {"format": "gguf", "directory": ""},
        "installed_at": int(time.time()),
        "files": [
            {"path": entry["path"], "sha256": entry["sha256"], "size": entry["size"]}
            for entry in lock["files"]
        ],
    }
    path = weights_dir(model) / STATE_FILE
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)
    path.chmod(0o600)


def require_model(selector: str) -> dict[str, Any]:
    catalog = all_models()
    model = catalog.get(selector) or catalog.get(f"local_mps/{selector}")
    if model is None:
        raise LocalModelError(
            f"{selector!r} is not a locally served model; defined: {', '.join(sorted(catalog))}"
        )
    return model


def cmd_install(argv: list[str]) -> int:
    runtime = Path(os.environ.get("HARNESS_RUNTIME_DIR", ""))
    if not argv and not runtime.is_dir():
        raise LocalModelError("install needs HARNESS_RUNTIME_DIR, or a model id to install")
    models = [require_model(item) for item in argv] if argv else selected_models(runtime)
    if not models:
        # Nothing local was selected. Silent and zero: this runs on every launch, and a host that
        # chose cloud models should hear nothing about weights.
        return 0
    pending = [model for model in models if not is_ready(model)]
    if not pending:
        for model in models:
            print(f"{model['where']}: {READY}")
        return 0
    failed = 0
    for model in pending:
        try:
            install(model)
            print(f"{model['where']}: installed", flush=True)
        except (LocalModelError, OSError, urllib.error.URLError) as error:
            # One line per failure and keep going: a launch that picked two local models should
            # still get the one that downloaded, and the log is where the reason lives.
            print(f"{model['where']}: install failed: {error}", flush=True)
            failed += 1
    if not failed:
        try:
            engine_binary()
        except EngineError as error:
            print(f"inference engine unavailable: {error}", flush=True)
            failed += 1
    return 1 if failed else 0


# --------------------------------------------------------------------------------------------
# serve
# --------------------------------------------------------------------------------------------


def api_token(model: dict[str, Any], plan: dict[str, Any] | None = None) -> str:
    """The session token for this model's provider, read from the file the launcher wrote.

    The same file the proxy mounts as a Docker secret, so the engine and the proxy cannot be told
    different values. Read here rather than passed through the shell because a secret in an argv
    list is visible to every process on the machine.
    """
    runtime = os.environ.get("HARNESS_RUNTIME_DIR", "")
    if not runtime:
        raise LocalModelError("serving needs HARNESS_RUNTIME_DIR to name the instance directory")
    secret = f"{model['provider']}__{model['credential']}"
    if plan is not None:
        bindings = {
            (entry["provider"], entry["credential"])
            for entry in plan["lanes"].values()
            if entry["model_id"] == model["id"]
        }
        if len(bindings) != 1:
            raise EngineError("local engine lanes disagree on credential identity")
        provider, credential = next(iter(bindings))
        secret = plan["providers"][provider]["credentials"][credential]["secret_name"]
    path = Path(runtime) / "credentials" / secret
    if not path.is_file():
        raise LocalModelError(f"no credential file for {model['provider']}; run models.py resolve")
    return path.read_text(encoding="utf-8").strip()


def engine_environment() -> dict[str, str]:
    """Do not let ambient llama.cpp overrides compete with the resolved allocation contract."""
    return {key: value for key, value in os.environ.items() if not key.startswith("LLAMA_ARG_")}


def primary_weights_path(model: dict[str, Any]) -> Path:
    lock = read_lock(Path(model["lock_path"]))
    paths = [weights_dir(model) / entry["path"] for entry in lock["files"]]
    return next((path for path in paths if "-00001-of-" in path.name), paths[0]).resolve()


def allocation_args(
    profile: dict[str, Any],
    total_context: int,
    sequences: int,
    *,
    server: bool,
) -> list[str]:
    """The projection and serving commands share every backend allocation setting."""
    args = [
        "--offline",
        "--fit",
        "off",
        "--ctx-size",
        str(total_context),
        "--parallel",
        str(sequences),
        "--n-gpu-layers",
        str(profile["gpu_layers"]),
        "--batch-size",
        str(profile["batch_tokens"]),
        "--ubatch-size",
        str(profile["microbatch_tokens"]),
        "--cache-type-k",
        profile["cache_type_k"],
        "--cache-type-v",
        profile["cache_type_v"],
        "--flash-attn",
        profile["flash_attention"],
        "--load-mode",
        profile["load_mode"],
        "--lazy-mode",
        profile["lazy_mode"],
    ]
    if server:
        args += [
            "--kv-unified" if profile["kv_unified"] else "--no-kv-unified",
            "--cache-ram",
            str(profile["cache_ram_mib"]),
            "--ctx-checkpoints",
            str(profile["context_checkpoints"]),
        ]
    return args


def engine_args(
    model: dict[str, Any],
    context: int,
    parallel: int,
    token: str,
    profile: dict[str, Any] | None = None,
) -> list[str]:
    """The command line one engine process runs with.

    Every number is derived: the port from the definition, the context and slot count from the plan
    the proxy enforces, the sampler from the vendor's published defaults. ``--alias`` is what makes
    the wire name the proxy rewrites every request to resolve to this process rather than 404.
    """
    install_block = model["install"]
    primary = primary_weights_path(model)
    sampling = install_block.get("sampling") or {}
    args = [
        str(engine_binary()),
        "--model",
        str(primary),
        "--alias",
        model["model"],
        "--host",
        os.environ.get("LOCAL_MPS_BIND", "").strip() or DEFAULT_BIND,
        "--port",
        str(int(install_block.get("port") or 0) or DEFAULT_PORT),
        "--api-key",
        token,
        "--no-webui",
    ]
    if profile is None:
        args += [
            "--ctx-size",
            str(context),
            "--parallel",
            str(parallel),
            "--n-gpu-layers",
            "all",
            "--no-kv-unified",
        ]
    else:
        args += allocation_args(profile, context, parallel, server=True)
        args += ["--log-jsonl"]
    flags = {
        "temperature": "--temp",
        "top_p": "--top-p",
        "top_k": "--top-k",
        "min_p": "--min-p",
        "presence_penalty": "--presence-penalty",
        "repetition_penalty": "--repeat-penalty",
    }
    for key, flag in flags.items():
        if key in sampling:
            value = sampling[key]
            args += [flag, str(int(value)) if float(value).is_integer() else str(value)]
    # Tool calling is served by the checkpoint's own chat template, and llama.cpp only applies it
    # when asked: without this the server accepts a `tools` array, parses with its legacy built-in
    # parser, and the declared capability is a claim nothing enforces. It is added here rather than
    # in allocation_args because that list is hashed into the engine fingerprint and shared with the
    # offline projection, which has no template to apply.
    #
    # This makes an engine that cannot load the embedded template fail at readiness instead of
    # failing at the first tool call, because the canary below is asked to prove the capability.
    if "tool_use" in (model.get("capabilities") or []):
        args += ["--jinja"]
    return args


#: The descriptor number an engine is told to read its session credential from. Agreed with the
#: frontend's own ``--token-fd`` argument rather than discovered, and documented in both places.
_TOKEN_DESCRIPTOR = 3


def _wants_token_descriptor(args: list[str]) -> bool:
    """Whether this engine was told to read its credential from a descriptor."""
    return "--token-fd" in args


def cmd_serve(argv: list[str]) -> int:
    """Start one model's engine and stay alive as its supervisor.

    The launcher backgrounds this process and kills it on shutdown, so the signal handler is what
    keeps the engine from outliving the session: a wrapper that died quietly would leave a
    hundred-gigabyte model resident with nothing pointing at it.
    """
    if len(argv) != 1:
        raise LocalModelError("serve takes exactly one model id")
    model = require_model(argv[0])
    runtime = Path(os.environ.get("HARNESS_RUNTIME_DIR", ""))
    plan = json.loads((runtime / "model-policy.json").read_text(encoding="utf-8"))
    if not is_ready(model):
        raise EngineError(f"{model['id']}: selected weights are not verified")
    context, parallel = lane_sizes(model, plan)
    if not context:
        print(f"{model['where']}: not part of this launch's plan; nothing to serve")
        return 0

    engine = (plan.get("engines") or {}).get(model["id"])
    if engine:
        validate_launch(model, engine)
    token = api_token(model, plan)
    if engine:
        # Command construction belongs to the adapter: llama.cpp is a downloaded binary taking
        # GGML flags, MLX is an interpreter running a frontend that supervises a worker. Everything
        # after this point - the listener probe, the signal-forwarding supervisor, the receipt - is
        # identical for both, which is the reason the split is worth having.
        adapter = capacity_adapter(engine["engine_kind"])
        artifact = adapter.validate_artifact(model, read_lock(Path(model["lock_path"])))
        runtime_description = adapter.provision(install=True)
        launch = adapter.launch(model, artifact, runtime_description, engine, token)
        args = list(launch.argv)
        environment = dict(launch.environment)
        host, port = launch.host, launch.port
        secret_indices = set(launch.secret_argument_indices)
    else:
        context, parallel = lane_sizes(model, plan)
        args = engine_args(
            model, context, parallel, token, engine["allocation"] if engine else None
        )
        environment = engine_environment()
        host, port = engine_host(), engine_port(model)
        secret_indices = {args.index("--api-key") + 1} if "--api-key" in args else set()
    print(
        f"{model['where']}: serving {size_text(total_bytes(read_lock(Path(model['lock_path']))))} "
        f"on port {port} (ctx={context}, slots={parallel})",
        flush=True,
    )
    receipt = readiness_path(runtime, model["id"])
    receipt.unlink(missing_ok=True)
    if probe_port(port, host):
        raise EngineError(
            f"{model['id']}: engine listener is already occupied; stop that listener before launch"
        )
    # Engines that name a token descriptor get one; llama.cpp only understands --api-key, so the
    # existing behaviour is preserved there rather than silently changed under a working engine.
    credential_pipe: tuple[int, int] | None = None
    if _wants_token_descriptor(args):
        credential_pipe = os.pipe()
        os.set_inheritable(credential_pipe[0], True)
        with os.fdopen(credential_pipe[1], "w", closefd=True) as sink:
            sink.write(token + "\n")
        # The write end is closed here, so the child reads the value and then EOF: a credential
        # that stays open on a pipe would keep the child waiting on a descriptor the parent has
        # finished with.
        token = ""
    if credential_pipe is not None:
        read_fd = credential_pipe[0]

        def _move_credential() -> None:  # pragma: no cover - runs between fork and exec
            # preexec_fn executes in the child only. Renumbering here rather than in the parent is
            # the difference between handing the engine fd 3 and hijacking the supervisor's own.
            os.dup2(read_fd, _TOKEN_DESCRIPTOR)
            if read_fd != _TOKEN_DESCRIPTOR:
                os.close(read_fd)

        child = subprocess.Popen(
            args, env=environment, pass_fds=(read_fd,), preexec_fn=_move_credential
        )
        os.close(read_fd)
    else:
        child = subprocess.Popen(args, env=environment)

    def forward(signum, _frame):
        if child.poll() is None:
            child.send_signal(signum)

    for signal_number in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(signal_number, forward)
    try:
        if engine:
            startup = wait_engine_ready(child, model, engine, token)
            receipt.parent.mkdir(parents=True, exist_ok=True)
            document = {
                "supervisor_pid": os.getpid(),
                "engine_pid": child.pid,
                "fingerprint": engine["fingerprint"],
                "snapshot_id": engine["snapshot_id"],
                "startup": startup,
                # The exact argv, redacted where the adapter said a secret sits. A receipt that
                # replayed a credential would be a credential on disk.
                "allocation_args": [
                    "<redacted>" if index in secret_indices else value
                    for index, value in enumerate(args)
                ],
                "serving_profile_id": engine["serving_profile_id"],
            }
            write_private_json(receipt, document)
            print(
                f"{model['where']}: engine identity and native context verified, ready", flush=True
            )
        return child.wait()
    except BaseException:
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
        raise
    finally:
        receipt.unlink(missing_ok=True)


def readiness_path(runtime: Path, model_id: str) -> Path:
    if not re.fullmatch(r"[a-zA-Z0-9_-]+/[a-zA-Z0-9_-]+", model_id):
        raise EngineError("invalid local engine identifier")
    return runtime / "local-engines" / f"{model_id.replace('/', '__')}.json"


def capacity_adapter(kind: str):
    """The adapter for one engine kind, reached through the resolver's registry.

    Imported inside the function because :mod:`capacity` imports this module at module scope; a
    top-level import here would be a cycle, and the cycle is the price of one shared registry.
    """
    if __package__:
        from . import capacity
    else:
        import capacity
    return capacity.adapter_for(kind)


def capacity_identity(binary: Path) -> dict[str, Any]:
    """The pinned engine's self-reported identity, for the adapter and the resolver alike.

    Reached through :mod:`capacity` on purpose: parsing ``--version`` output is the resolver's
    existing job, and an adapter that re-implemented the regex would be a second opinion on what
    "same build" means.
    """
    if __package__:
        from . import capacity
    else:
        import capacity
    return capacity.engine_identity(binary)


def validate_launch(model: dict[str, Any], engine: dict[str, Any]) -> None:
    """Reject source, weights or binary drift before loading a model into physical memory."""
    if __package__:
        from . import capacity
    else:
        import capacity
    providers, _ = definitions.load_definitions(ROOT)
    profile = providers[model["provider"]]["execution"]["profile"]
    checks = {
        "model_alias": model["model"],
        "model_path": str(primary_weights_path(model)),
        "model_lock_digest": hashlib.sha256(Path(model["lock_path"]).read_bytes()).hexdigest(),
        "model_manifest_digest": hashlib.sha256(
            (Path(model["path"]) / "model.toml").read_bytes()
        ).hexdigest(),
        "provider_manifest_digest": hashlib.sha256(
            (Path(providers[model["provider"]]["path"]) / "provider.toml").read_bytes()
        ).hexdigest(),
        "allocation": profile,
        "capacity_strategy": "native_context",
        "memory_management": "os_managed",
        "native_context_tokens": model["native_tokens"],
        "context_tokens": model["native_tokens"],
        "sequences": providers[model["provider"]]["capacity"]["sequence_slots"],
        "total_context_tokens": model["native_tokens"]
        * providers[model["provider"]]["capacity"]["sequence_slots"],
    }
    if any(engine.get(key) != value for key, value in checks.items()):
        raise EngineError("model or serving profile changed after capacity resolution")
    try:
        identity = capacity.engine_identity(engine_binary())
    except capacity.CapacityError as exc:
        raise EngineError(str(exc)) from exc
    if any(engine.get(key) != value for key, value in identity.items()):
        raise EngineError("engine binary changed after capacity resolution")


def validate_properties(props: dict[str, Any], engine: dict[str, Any]) -> None:
    """Attest the actual server's read-only properties against its resolved allocation."""
    settings = props.get("default_generation_settings") if isinstance(props, dict) else None
    if (
        not isinstance(props, dict)
        or not isinstance(settings, dict)
        or props.get("model_alias") != engine["model_alias"]
        or props.get("model_path") != engine["model_path"]
        or type(props.get("total_slots")) is not int
        or props.get("total_slots") != engine["sequences"]
        or type(settings.get("n_ctx")) is not int
        or settings.get("n_ctx") != engine["context_tokens"]
        or engine["build_commit"] not in str(props.get("build_info") or "")
        or props.get("is_sleeping") is not False
    ):
        raise EngineError("the running engine differs from its resolved allocation")


#: What a ``/props`` body may weigh. This used to be a bare 65536 repeated at the read and the
#: comparison, and it is too small to be safe: ``/props`` embeds the model's entire chat template,
#: so a template large enough to cross this bound makes a perfectly healthy engine report
#: "exceeds the supported metadata size" and refuse to start. Raised well clear of any real
#: template, still bounded so a runaway response cannot exhaust the launcher's memory.
LOCAL_PROPS_BYTES = 1024 * 1024


class StreamingCanaryScanner:
    """The minimum SSE reader the canary needs, reporting through the shared vocabulary.

    Deliberately not a copy of the proxy's ``UsageScanner``: the launcher has no aiohttp and must
    not gain one. What the two share is the *classification*, which is the entire point of
    ``local_engine_contract`` - a canary that invented its own idea of "usable" would reproduce the
    drift this change set exists to remove.
    """

    def __init__(self) -> None:
        self.buffer = bytearray()
        self.content_chars = 0
        self.reasoning_chars = 0
        self.tool_calls = 0
        self.terminated = False
        self.saw_error_object = False
        self.saw_framing_failure = False
        self.overflowed = False

    def feed(self, chunk: bytes) -> None:
        self.buffer += chunk
        if len(self.buffer) > LOCAL_PROPS_BYTES:
            self.buffer.clear()
            self.overflowed = True
            self.saw_framing_failure = True
            return
        while True:
            raw, separator, rest = bytes(self.buffer).partition(b"\n\n")
            if not separator:
                return
            self.buffer = bytearray(rest)
            self._event(raw)

    def finish(self) -> None:
        if not self.overflowed and self.buffer:
            self._event(bytes(self.buffer))
        self.buffer.clear()

    def _event(self, raw: bytes) -> None:
        for line in raw.split(b"\n"):
            line = line.strip()
            if not line.startswith(b"data:"):
                continue
            data = line[5:].strip()
            if not data:
                continue
            if data == b"[DONE]":
                self.terminated = True
                continue
            try:
                event = json.loads(data)
            except (json.JSONDecodeError, UnicodeDecodeError):
                self.saw_framing_failure = True
                continue
            if not isinstance(event, dict):
                self.saw_framing_failure = True
                continue
            if event.get("error") is not None:
                self.saw_error_object = True
            for choice in event.get("choices") or []:
                if not isinstance(choice, dict):
                    continue
                delta = choice.get("delta")
                source = delta if isinstance(delta, dict) else choice.get("message")
                if not isinstance(source, dict):
                    continue
                content = source.get("content")
                if isinstance(content, str):
                    self.content_chars += len(content)
                reasoning = source.get("reasoning_content")
                if isinstance(reasoning, str):
                    self.reasoning_chars += len(reasoning)
                calls = source.get("tool_calls")
                if isinstance(calls, list):
                    self.tool_calls += sum(1 for item in calls if isinstance(item, dict))

    def outcome(self) -> str:
        return str(
            classify(
                StreamEvidence(
                    content_chars=self.content_chars,
                    reasoning_chars=self.reasoning_chars,
                    tool_calls=self.tool_calls,
                    terminated=self.terminated,
                    saw_error_object=self.saw_error_object,
                    saw_framing_failure=self.saw_framing_failure,
                )
            )
        )


#: A synthetic prompt for the compute canary. Public and synthetic on purpose: it has to be the same
#: bytes on every launch so a pass means "this engine computed", not "this prompt happened to work".
CANARY_PROMPT = "Reply with exactly the word: ready"
CANARY_OUTPUT_TOKENS = 8
#: How long the canary may take before the launch is treated as failed. Loading a multi-gigabyte
#: model and running one short generation on unified memory that may already be oversubscribed is
#: slow work, so this bounds a hang rather than a slow start.
CANARY_TIMEOUT_SECONDS = float(os.environ.get("LOCAL_MPS_CANARY_TIMEOUT_SECONDS", "120"))


class LocalModelCanary:
    """One authenticated generation, run before readiness is announced.

    Metadata readiness was the bug. ``/props`` answers from the HTTP layer, so a backend whose Metal
    allocations had already failed still reported a model, a context length, and a slot count - and
    the launcher published a receipt, Kimi staged against it, and every subsequent request failed
    while the session retried forever. Nothing short of asking the model to produce a token
    distinguishes those two states.

    The canary deliberately does not assert an answer string. What is being tested is that compute
    works, not that the quant reproduces a particular completion, and a pinned expected string
    would fail launches for reasons that have nothing to do with the engine.
    """

    def __init__(self, host: str, port: int, token: str, model: dict[str, Any]) -> None:
        self.host = host
        self.port = port
        self.token = token
        self.model = model

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}/v1/chat/completions"

    @property
    def declares_tools(self) -> bool:
        return "tool_use" in (self.model.get("capabilities") or [])

    def payload(self) -> dict[str, Any]:
        sampling = (self.model.get("install") or {}).get("sampling") or {}
        body: dict[str, Any] = {
            "model": self.model["model"],
            "messages": [{"role": "user", "content": CANARY_PROMPT}],
            "max_tokens": CANARY_OUTPUT_TOKENS,
            "stream": True,
            "temperature": sampling.get("temperature", 0.0),
            # Deterministic where the engine supports it: the point is a token out of the model,
            # and a creative sampler can decline to produce one on a run that is otherwise fine.
            "seed": 0,
        }
        if "top_p" in sampling:
            body["top_p"] = sampling["top_p"]
        # Thinking is disabled through the mechanism the pinned engine documents rather than by
        # ignoring the capability: a reasoning block with no answer behind it classifies as
        # reasoning_only, which is unusable, so a canary that left thinking on could fail a healthy
        # engine. The field is the one llama.cpp's server honours for chat templates that gate it.
        body["chat_template_kwargs"] = {"enable_thinking": False}
        return body

    def request(self, payload: dict[str, Any] | None = None) -> urllib.request.Request:
        body = json.dumps(
            payload or self.payload(), separators=(",", ":"), allow_nan=False
        ).encode()
        return urllib.request.Request(
            self.url,
            data=body,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
            },
        )

    def _generate(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Run one streaming completion and classify what came back."""
        scanner = StreamingCanaryScanner()
        with urllib.request.urlopen(
            self.request(payload), timeout=CANARY_TIMEOUT_SECONDS
        ) as response:
            if response.status != 200:
                raise EngineError(f"canary generation returned HTTP {response.status}")
            for chunk in iter(lambda: response.read(4096), b""):
                scanner.feed(chunk)
        scanner.finish()
        return {"outcome": scanner.outcome(), "content_chars": scanner.content_chars}

    def _tool_payload(self) -> dict[str, Any]:
        body = self.payload()
        body["messages"] = [
            {"role": "user", "content": "What is 26 minus 19? Use the calculator tool."}
        ]
        body["tools"] = [
            {
                "type": "function",
                "function": {
                    "name": "calculator",
                    "description": "Evaluate an arithmetic expression.",
                    "parameters": {
                        "type": "object",
                        "properties": {"expression": {"type": "string"}},
                        "required": ["expression"],
                    },
                },
            }
        ]
        # Forcing the call takes the model's discretion out of it, so a miss here is about the
        # template and the parser rather than about whether this quant felt like calling a tool.
        body["tool_choice"] = "required"
        body["max_tokens"] = 64
        return body

    def run(self) -> dict[str, Any]:
        """Prove the engine can compute, and that it can do so in every mode it claims.

        Returns a record rather than raising on an unusable answer where possible, so the caller
        attaches the reason to a failed launch instead of losing it to a traceback.
        """
        record = self._generate(self.payload())
        if record["outcome"] != "content":
            raise EngineError(
                "the engine answered metadata but its compute canary produced "
                f"{record['outcome']!r}"
            )
        if self.declares_tools:
            # The only thing that makes a declared tool_use capability mean anything. A server
            # without the checkpoint's template applied still accepts a `tools` array and returns
            # prose, which at the transport layer is a perfectly successful completion.
            tool_record = self._generate(self._tool_payload())
            if tool_record["outcome"] != "tool_only":
                raise EngineError(
                    "the model declares tool_use but the engine returned "
                    f"{tool_record['outcome']!r} for a forced tool call, so the chat template is "
                    "not being applied"
                )
            record["tool_canary"] = tool_record["outcome"]
        return record


def wait_engine_ready(
    child: Any, model: dict[str, Any], engine: dict[str, Any], token: str
) -> dict[str, int | None]:
    """Do not stage Kimi until this owned child has loaded *and* actually computed.

    Metadata validation is a precondition, never the verdict: see :class:`LocalModelCanary`.
    """
    if __package__:
        from . import host_caps
    else:
        import host_caps
    host = engine_host()
    req = urllib.request.Request(
        f"http://{host}:{engine_port(model)}/props",
        headers={"Authorization": f"Bearer {token}"},
    )
    while child.poll() is None:
        try:
            with urllib.request.urlopen(req, timeout=5) as response:
                data = response.read(LOCAL_PROPS_BYTES + 1)
                if len(data) > LOCAL_PROPS_BYTES:
                    raise EngineError("engine properties exceed the supported metadata size")
                props = json.loads(data)
        except (OSError, ValueError):
            time.sleep(0.1)
            continue
        validate_properties(props, engine)
        # The engine has now proven it is the process the plan describes. Ask it to compute.
        try:
            canary = LocalModelCanary(host, engine_port(model), token, model).run()
        except EngineError as exc:
            # A canary failure is a failed launch, not a degraded start: terminating only this
            # attempt's child is what keeps the occupied-listener protections meaningful and stops
            # a receipt ever reaching the launcher for a backend that cannot serve.
            raise EngineError(f"{model['id']}: {exc}") from exc
        except (OSError, urllib.error.URLError) as exc:
            raise EngineError(
                f"{model['id']}: the compute canary could not reach the engine ({exc})"
            ) from exc
        if child.poll() is not None:
            raise EngineError("the owned engine exited during readiness validation")
        try:
            footprint = host_caps.process_memory_bytes(child.pid) or None
            available = host_caps.available_memory_bytes() or None
        except (OSError, ValueError):
            footprint, available = None, None
        ready = {
            "resident_bytes": footprint,
            "available_host_bytes": available,
            "context_tokens": engine["context_tokens"],
            "sequences": engine["sequences"],
            "canary_outcome": canary["outcome"],
            "canary_content_chars": canary["content_chars"],
        }
        # Recorded only when the model claimed the capability, so an absent key and a failed probe
        # are different facts in the receipt rather than the same-looking one.
        if "tool_canary" in canary:
            ready["tool_canary"] = canary["tool_canary"]
        return ready
    raise EngineError(f"{model['id']}: the engine exited before readiness")


def cmd_await_ready(argv: list[str], supervisor_pid: int) -> int:
    """Wait for a readiness receipt from the exact supervisor this launcher started."""
    if len(argv) != 1 or supervisor_pid <= 0:
        raise EngineError("await-ready needs one model id and --supervisor-pid")
    runtime = Path(os.environ.get("HARNESS_RUNTIME_DIR", ""))
    plan = json.loads((runtime / "model-policy.json").read_text(encoding="utf-8"))
    engine = (plan.get("engines") or {}).get(argv[0])
    if engine is None:
        return 0
    path = readiness_path(runtime, argv[0])
    while True:
        try:
            os.kill(supervisor_pid, 0)
        except OSError as exc:
            raise EngineError(f"{argv[0]}: its owned supervisor exited before readiness") from exc
        try:
            receipt = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            receipt = {}
        if (
            receipt.get("supervisor_pid") == supervisor_pid
            and receipt.get("fingerprint") == engine["fingerprint"]
            and receipt.get("snapshot_id") == engine["snapshot_id"]
        ):
            return 0
        time.sleep(0.1)


def cmd_provision(argv: list[str]) -> int:
    """Install what is missing, then serve. One background job per selected local model.

    Retained for direct callers; managed launches verify installation and resolve
    capacity before serving. Serving is the same supervisor :command:`serve` runs,
    so its caller tracks one process whether or not a download happened.
    """
    if len(argv) != 1:
        raise LocalModelError("provision takes exactly one model id")
    model = require_model(argv[0])
    if not is_ready(model):
        try:
            install(model)
            engine_binary()
        except (LocalModelError, OSError, urllib.error.URLError) as error:
            print(f"{model['where']}: provisioning failed: {error}", flush=True)
            return 1
        print(f"{model['where']}: installed", flush=True)
    return cmd_serve(argv)


def cmd_check(argv: list[str]) -> int:
    """One line the launcher can print about a lane whose model lives on this host.

    Informational, so it answers rather than judges: exit status 0 in every case, because a
    launch is not failed by a model that is still downloading.
    """
    if len(argv) != 1:
        raise LocalModelError("check takes exactly one model id")
    model = require_model(argv[0])
    state = status_text(model)
    note = ""
    if state == READY:
        # A verified weight set and a dead process are different answers, and only the second
        # one means the operator's first request is going to fail.
        port = engine_port(model)
        note = ", engine listening" if probe_port(port) else ", engine not up yet"
    print(f"{model['where']}: {state}{note}")
    return 0


def probe_port(port: int, host: str = "127.0.0.1") -> bool:
    """Whether something accepts TCP on the port the engine was told to use.

    A connect test rather than an HTTP one: the token lives in a file the launcher has no reason
    to read for a yes-or-no question, and a process still loading a hundred gigabytes is exactly
    the case where an authenticated request would be the wrong probe.
    """
    import socket

    try:
        with socket.create_connection((host, port), timeout=2.0):
            return True
    except OSError:
        return False


def cmd_pending(argv: list[str]) -> int:
    """Print the selected local models still needing installation, one id per line."""
    runtime = Path(os.environ.get("HARNESS_RUNTIME_DIR", ""))
    pending = [model["id"] for model in selected_models(runtime) if not is_ready(model)]
    print("\n".join(pending))
    return 0 if pending else 1


def cmd_selected(argv: list[str]) -> int:
    """Every local model this launch's plan uses, one tab-separated id and state per line.

    The launcher reads this to decide what to start before Compose comes up and what to fetch
    after it does, without reimplementing the plan or the verification rule in shell.
    """
    runtime = Path(os.environ.get("HARNESS_RUNTIME_DIR", ""))
    for model in selected_models(runtime):
        print(f"{model['id']}\t{status_text(model)}")
    return 0


def cmd_status(argv: list[str]) -> int:
    catalog = all_models()
    if argv:
        catalog = {key: value for key, value in catalog.items() if value["provider"] == argv[0]}
    if len(argv) > 1 and argv[1] in catalog:
        catalog = {argv[1]: catalog[argv[1]]}
    for model_id, model in sorted(catalog.items()):
        print(f"{model_id}: {status_text(model)}")
    return 0


# --------------------------------------------------------------------------------------------
# command line
# --------------------------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        choices=[
            "status",
            "install",
            "serve",
            "provision",
            "pending",
            "check",
            "selected",
            "await-ready",
        ],
    )
    parser.add_argument("selector", nargs="*", default=[])
    parser.add_argument("--supervisor-pid", type=int, default=0)
    args = parser.parse_args(argv)
    actions = {
        "status": cmd_status,
        "install": cmd_install,
        "serve": cmd_serve,
        "provision": cmd_provision,
        "pending": cmd_pending,
        "check": cmd_check,
        "selected": cmd_selected,
        "await-ready": lambda selectors: cmd_await_ready(selectors, args.supervisor_pid),
    }
    return actions[args.action](list(args.selector))


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (LocalModelError, definitions.DefinitionError) as error:
        raise SystemExit(f"Local model setup refused: {error}") from error
