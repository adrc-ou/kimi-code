#!/usr/bin/env python3
"""Select only backend-certified, immutable ComfyUI releases."""

import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from select_versions import SEMVER_RE, choose, load_state, semver_key, write_environment


def comfy_catalog(platform_key: str) -> tuple[list[dict[str, str]], str]:
    path = Path(__file__).resolve().parent / "backend" / "compatibility.json"
    document = json.loads(path.read_text())
    entries = document.get("entries", [])
    if not isinstance(entries, list):
        raise SystemExit("Invalid ComfyUI compatibility catalog")
    catalog = []
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("platform") != platform_key:
            continue
        status = entry.get("status")
        if status not in {"locked", "tested"}:
            continue
        version = str(entry.get("comfyui_version", ""))
        commit = str(entry.get("comfyui_commit", ""))
        if not SEMVER_RE.fullmatch(version) or not re.fullmatch(r"[0-9a-f]{40}", commit):
            raise SystemExit("Invalid ComfyUI compatibility entry")
        catalog.append({"version": version, "commit": commit, "status": status})
    catalog.sort(key=lambda item: semver_key(item["version"]), reverse=True)
    return catalog, catalog[0]["version"] if catalog else ""


if __name__ == "__main__":
    catalog, latest = comfy_catalog(os.environ["COMFYUI_PLATFORM"])
    chosen = choose(
        "ComfyUI",
        os.environ["HARNESS_PLATFORM_LABEL"],
        catalog,
        latest,
        os.environ.get("COMFYUI_INSTALLED", ""),
        os.environ.get("COMFYUI_VERSION", ""),
        os.environ["MODULE_NON_INTERACTIVE"] == "true",
    )
    path = Path(os.environ["HARNESS_SESSION_FILE"])
    values = load_state(path)
    values.update(COMFYUI_VERSION=chosen["version"], COMFYUI_COMMIT=chosen["commit"])
    write_environment(path, values)
