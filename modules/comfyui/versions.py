#!/usr/bin/env python3
"""Select the ComfyUI release to launch, from the releases this platform can install.

Which releases exist is a question for the upstream repository; which of them can be installed
here, and with what dependencies, is a question for ``releases.py`` and the backend profile beside
this module. This file is only the launcher's step: it assembles a catalog, asks, and writes down
what was answered.
"""

import os
import sys
from pathlib import Path

# This runs as a script, so both of the paths it reads from are added by hand: ``scripts/`` for the
# shared release selector, and ``tools/`` for the modal engine, under the same flat identity that
# ``tools/*.py`` itself uses when it is run as a program. ``releases`` is imported flat for the same
# reason it lives in this directory.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import releases  # noqa: E402  (the path above is what makes this import work)
from select_versions import (  # noqa: E402
    choose,
    load_state,
    running_flow,
    write_environment,
)
from tui import flow
from tui.app import View

# What this module contributes to the session file, so that a replay can tell a settled answer from
# a half-written one. The requirements digest is part of the answer rather than something the
# installers re-derive, because the dependency lock has to be keyed by the digest that was verified
# when the operator confirmed the version — not by whatever the file happens to contain later.
MODULE_KEYS = ("COMFYUI_VERSION", "COMFYUI_COMMIT", "COMFYUI_REQUIREMENTS_SHA256")


def comfy_catalog(
    platform_key: str,
    path: Path | None = None,
    *,
    cache: Path | None = None,
    now: float | None = None,
) -> tuple[list[dict[str, str]], str]:
    """The installable releases for one platform, newest first, with the newest of them.

    ``path`` is the backend profile document, defaulting to the one shipped beside this module, and
    ``cache`` is where the fetched listing is kept. A caller may point either at another so the
    ordering, the provenance marks and the offline refusals can be exercised without touching the
    network or waiting for the shipped file to change.
    """
    return releases.catalog_for(platform_key, profile_path=path, cache=cache, now=now)


def main() -> None:
    platform = os.environ["COMFYUI_PLATFORM"]
    cache = releases.cache_path()
    catalog, latest = comfy_catalog(platform, cache=cache)
    path = Path(os.environ["HARNESS_SESSION_FILE"])
    override = os.environ.get("COMFYUI_VERSION", "").strip()
    non_interactive = os.environ["MODULE_NON_INTERACTIVE"] == "true"
    launch = running_flow()
    if launch and not launch.should_render(flow.MODULE_VERSION):
        # A replay: this pass renders no screen because a previous one already asked and the answer
        # is on disk. The pass that asked resolved what it wrote and everything downstream re-checks
        # the backend profile anyway, so there is nothing left to decide — and re-deriving the
        # default here would overwrite the version the user chose.
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

    # Only the row that was picked gets its commit and requirements digest looked up. The catalog
    # deliberately carries neither for the rows nobody chose, so scrolling past nine releases does
    # not cost nineteen requests.
    chosen = releases.resolve(chosen, platform, cache=cache)
    values = load_state(path)
    values.update(
        COMFYUI_VERSION=chosen["version"],
        COMFYUI_COMMIT=chosen["commit"],
        COMFYUI_REQUIREMENTS_SHA256=chosen["requirements_sha256"],
    )
    write_environment(path, values)
    if launch and asking:
        launch.commit(flow.MODULE_VERSION, chosen["version"])


if __name__ == "__main__":
    main()
