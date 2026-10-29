#!/usr/bin/env python3
"""Validate immutable dependency and compatibility metadata."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    lock = json.loads((ROOT / "dependencies.lock.json").read_text())
    for value in lock["images"].values():
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", value):
            raise SystemExit(f"invalid image digest: {value}")
    for value in lock["sources"].values():
        if not re.fullmatch(r"[0-9a-f]{40}", value):
            raise SystemExit(f"invalid source commit: {value}")
    for name, entry in lock.get("downloads", {}).items():
        check_download(name, entry)
    for relative, expected in lock.get("files", {}).items():
        if expected != f"sha256:{digest(ROOT / relative)}":
            raise SystemExit(f"locked file digest is stale: {relative}")
    for model in locked_models():
        check_weights_lock(model)
    check_native_ml_lock()
    for module in sorted((ROOT / "modules").glob("*/check_locks.py")):
        subprocess.run(["python3", str(module)], check=True)
    exceptions = json.loads((ROOT / "vulnerability-exceptions.json").read_text())
    required = {"package", "vulnerability", "owner", "reason", "expires"}
    for item in exceptions["exceptions"]:
        if not required.issubset(item):
            raise SystemExit("vulnerability exception is missing required fields")
        if date.fromisoformat(item["expires"]) < date.today():
            raise SystemExit(f"expired vulnerability exception: {item['vulnerability']}")


#: Where a host binary may come from. Official upstream only, and versioned in the URL, because
#: this is the one class of dependency the build never fetches through a package manager.
DOWNLOAD_HOSTS = (
    "https://github.com/",
    "https://huggingface.co/",
)


def check_download(name: str, entry: dict) -> None:
    """One pinned host binary: a version, an official release URL naming it, and a digest."""
    version = str(entry.get("version", ""))
    url = str(entry.get("url", ""))
    value = str(entry.get("digest", ""))
    if not version:
        raise SystemExit(f"download {name} pins no version")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", value):
        raise SystemExit(f"download {name} has no SHA-256 digest: {value}")
    if not url.startswith(DOWNLOAD_HOSTS):
        raise SystemExit(f"download {name} is not from an official upstream host: {url}")
    if "/releases/download/" not in url and "/resolve/" not in url:
        raise SystemExit(f"download {name} is not a versioned release URL: {url}")
    if version not in url:
        raise SystemExit(f"download {name} URL does not name the version it pins: {url}")


def locked_models() -> list:
    """Every shipped model that has weights to install, as discovery sees it.

    Read through :mod:`definitions` rather than by globbing the tree, so a lock this script checks
    is the lock the launcher would load - same refusals, same paths.
    """
    import sys

    if str(ROOT / "tools") not in sys.path:
        sys.path.insert(0, str(ROOT / "tools"))
    import definitions

    try:
        _, models = definitions.load_definitions(ROOT)
    except definitions.DefinitionError as error:
        raise SystemExit(f"model definitions do not load: {error}") from error
    return [model for model in models if model.get("install")]


def check_weights_lock(model) -> None:
    """Validate a weight lock's shape, and that it pins the build its model claims.

    The digests are not recomputed here - the bytes are not in the repository and never will be -
    so this is the check that a hand-edited lock is still *well formed* and still about the
    quantisation the definition says it installs. The digest itself is enforced by the installer,
    which refuses the download on a mismatch.
    """
    import local_llm

    try:
        lock = local_llm.read_lock(model["lock_path"])
    except local_llm.LocalModelError as error:
        raise SystemExit(str(error)) from error
    variant = model["install"]["variant"]
    artifact = lock.get("artifact")
    if isinstance(artifact, dict):
        # A schema-2 lock states its variant, so the old filename-substring match is replaced by
        # the declaration it was always standing in for. An MLX shard is named model-00001-of-
        # 00003.safetensors and carries no quant label at all, so substring matching would reject a
        # correctly pinned artifact.
        if str(artifact.get("variant", "")) != variant:
            raise SystemExit(
                f"{model['where']}: weights.lock.json pins variant "
                f"{artifact.get('variant')!r} but [install].variant says {variant!r}"
            )
    elif not any(variant in entry["path"] for entry in lock["files"]):
        raise SystemExit(
            f"{model['where']}: weights.lock.json pins no file named for variant {variant!r}"
        )
    # The floor has to cover the download, and it is measured in GiB while the lock is in bytes:
    # a model that cannot fit its own weights is a model that fails at 03:00 on a Sunday, so it
    # fails here instead. Headroom above the total is deliberate and not checked - the installer
    # writes a .part beside nothing, so peak usage is one copy plus the engine's own mapping.
    total = local_llm.total_bytes(lock)
    floor = model["requires"]["disk_gib"] * 1024**3
    if floor and total > floor:
        raise SystemExit(
            f"{model['where']}: [requires].disk_gib={model['requires']['disk_gib']} does not "
            f"cover the {total} bytes its lock pins"
        )


#: The native runtime a managed MLX engine is provisioned into, and the lock that pins it.
MLX_LOCK_FILE = "providers/local_mps/requirements-mlx.lock"
MLX_REQUIREMENTS_FILE = "providers/local_mps/requirements-mlx.in"


def check_native_ml_lock() -> None:
    """The MLX dependency lock must be present, hash-pinned, and complete for its own inputs.

    This is the one dependency set the build never resolves: it is installed on the operator's host
    by ``tools/local_mlx_runtime.py``, outside any image and any package manager CI has. A lock that
    drifted from its ``.in`` would therefore install cleanly and be wrong.
    """
    import re

    lock_path = ROOT / MLX_LOCK_FILE
    if not lock_path.is_file():
        raise SystemExit(f"{MLX_LOCK_FILE} is missing; an MLX profile cannot be provisioned")
    text = lock_path.read_text(encoding="utf-8")
    # Split on the top-level pin lines rather than matching across them: a pin owns every
    # ``--hash=`` line that follows it until the next pin.
    blocks = re.split(r"\n(?=[A-Za-z0-9._-]+==)", text)
    pins: dict[str, tuple[str, set[str]]] = {}
    for block in blocks:
        head = re.match(r"([A-Za-z0-9._-]+)==([^\s]+)", block)
        if head:
            pins[head.group(1).lower()] = (
                head.group(2),
                set(re.findall(r"--hash=sha256:([0-9a-f]{64})", block)),
            )
    if not pins:
        raise SystemExit(f"{MLX_LOCK_FILE} pins no packages")
    for name, (version, digests) in pins.items():
        if not digests:
            raise SystemExit(f"{MLX_LOCK_FILE}: {name}=={version} is not hash-pinned")
    for raw in (ROOT / MLX_REQUIREMENTS_FILE).read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        name, _, version = line.partition("==")
        pinned = pins.get(name.strip().lower())
        if pinned is None or pinned[0] != version.strip():
            raise SystemExit(
                f"{MLX_LOCK_FILE} does not pin {line} from {MLX_REQUIREMENTS_FILE}; the lock was "
                "compiled from different input than the requirements file describes"
            )


if __name__ == "__main__":
    main()
