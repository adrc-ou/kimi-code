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
    if not any(variant in entry["path"] for entry in lock["files"]):
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


if __name__ == "__main__":
    main()
