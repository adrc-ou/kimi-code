#!/usr/bin/env python3
"""Select only backend-certified, immutable ComfyUI releases."""

import json
import os
import re
import sys
from pathlib import Path

# This runs as a script, so both of the paths it reads from are added by hand: ``scripts/`` for the
# shared release selector, and ``tools/`` for the modal engine, under the same flat identity that
# ``tools/*.py`` itself uses when it is run as a program.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
from select_versions import (
    SEMVER_RE,
    choose,
    load_state,
    running_flow,
    semver_key,
    write_environment,
)
from tui import flow
from tui.app import View

# What this module contributes to the session file, so that a replay can tell a settled answer from
# a half-written one.
MODULE_KEYS = ("COMFYUI_VERSION", "COMFYUI_COMMIT")


def comfy_catalog(platform_key: str, path: Path | None = None) -> tuple[list[dict[str, str]], str]:
    """The certified backends for one platform, newest first, with the newest version.

    ``path`` is the compatibility document; it defaults to the one shipped beside this module,
    and a caller may point it at another so the ordering and the refusals can be exercised
    without waiting for the shipped file to contain a second release.
    """
    if path is None:
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


def main() -> None:
    catalog, latest = comfy_catalog(os.environ["COMFYUI_PLATFORM"])
    path = Path(os.environ["HARNESS_SESSION_FILE"])
    override = os.environ.get("COMFYUI_VERSION", "").strip()
    non_interactive = os.environ["MODULE_NON_INTERACTIVE"] == "true"
    launch = running_flow()
    if launch and not launch.should_render(flow.MODULE_VERSION):
        # A replay: this pass renders no screen because a previous one already asked and the answer
        # is on disk. The pass that asked certified what it wrote and everything downstream
        # re-checks the compatibility document anyway, so there is nothing left to decide — and
        # re-deriving the default here would overwrite the version the user chose.
        carried = load_state(path)
        if all(carried.get(key, "").strip() for key in MODULE_KEYS):
            return
        # An answer the file cannot show is not an answer. Forgetting it makes this pass ask again,
        # which is the only way to end up with a complete one.
        launch.forget(flow.MODULE_VERSION)

    # Whether this step would put a screen on the terminal. The flow has to know before the menu
    # exists, because a step that answers itself has no screen in it and belongs to neither the
    # count nor the sequence.
    asking = not override and not non_interactive
    rendering = launch.plan(flow.MODULE_VERSION, 1 if asking else 0) if launch else asking
    view = None
    if rendering:
        position, total = launch.rail(flow.MODULE_VERSION) if launch else (1, 1)
        view = View(
            position=position,
            total=total,
            can_go_back=bool(launch and launch.previous(flow.MODULE_VERSION)),
        )
    try:
        chosen = choose(
            "ComfyUI",
            os.environ["HARNESS_PLATFORM_LABEL"],
            catalog,
            latest,
            os.environ.get("COMFYUI_INSTALLED", ""),
            override,
            non_interactive,
            view,
        )
    except flow.BackRequested:
        # This menu is one screen with nothing earlier inside it, so Back means the previous step of
        # the launch, and the pass has to be restarted from the top to get there.
        flow.back_from(launch, flow.MODULE_VERSION)
    values = load_state(path)
    values.update(COMFYUI_VERSION=chosen["version"], COMFYUI_COMMIT=chosen["commit"])
    write_environment(path, values)
    if launch and asking:
        launch.commit(flow.MODULE_VERSION, chosen["version"])


if __name__ == "__main__":
    main()
