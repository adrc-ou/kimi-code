#!/usr/bin/env python3

"""Fetch trusted release metadata and interactively select runtime versions."""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import ssl
import sys
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

# The selectors are run as scripts from the repository root, so ``tools/`` has to be on the path by
# hand, exactly as ``modules/comfyui/versions.py`` does for ``scripts/``. The engine is imported
# flat under that identity, the same one ``tools/*.py`` uses when it is run as a program.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from tui import flow
from tui.app import View, run
from tui.menu import SINGLE, Choice, ListStep

GITHUB_API = "https://api.github.com"
KIMI_REPOSITORY = "MoonshotAI/kimi-code"
MAX_METADATA_BYTES = 4 * 1024 * 1024
SEMVER_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")
KIMI_TAG_RE = re.compile(r"^@moonshot-ai/kimi-code@(\d+\.\d+\.\d+)$")
# The whole of what this selector contributes to the session file, and therefore the whole of what
# has to be present for a previous pass to count as having answered the question.
KIMI_KEYS = ("KIMI_CODE_VERSION", "KIMI_CODE_ASSET_URL", "KIMI_CODE_ASSET_SHA256")


def release_ssl_context() -> ssl.SSLContext:
    """Use macOS's CA bundle when Python has no default trust anchors."""
    context = ssl.create_default_context()
    if (
        sys.platform == "darwin"
        and not os.environ.get("SSL_CERT_FILE")
        and not os.environ.get("SSL_CERT_DIR")
        and context.cert_store_stats()["x509_ca"] == 0
    ):
        system_bundle = Path("/etc/ssl/cert.pem")
        if system_bundle.is_file():
            context.load_verify_locations(cafile=str(system_bundle))
    return context


def github_json(path: str) -> Any:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "adrc-kimi-harness-version-selector",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = os.environ.get("GITHUB_RELEASES_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"

    request = urllib.request.Request(f"{GITHUB_API}{path}", headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=30, context=release_ssl_context()) as response:
            body = response.read(MAX_METADATA_BYTES + 1)
            if len(body) > MAX_METADATA_BYTES:
                raise ValueError("response exceeds 4 MiB")
            return json.loads(body)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise SystemExit(f"Unable to fetch GitHub release metadata: {exc}") from exc


def read_text_url(url: str) -> str:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "adrc-kimi-harness-version-selector"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30, context=release_ssl_context()) as response:
            body = response.read(64 * 1024 + 1)
            if len(body) > 64 * 1024:
                raise ValueError("checksum response exceeds 64 KiB")
            return body.decode("utf-8")
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise SystemExit(f"Unable to fetch release checksum: {exc}") from exc


def semver_key(version: str) -> tuple[int, int, int]:
    match = SEMVER_RE.fullmatch(version)
    if not match:
        raise ValueError(f"Not a stable semantic version: {version}")
    return tuple(int(part) for part in match.groups())


def load_state(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    if not path.is_file():
        return result

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        result[key] = value.strip().strip("'\"")
    return result


def release_list(repository: str) -> tuple[list[dict[str, Any]], str]:
    quoted = urllib.parse.quote(repository, safe="/")
    releases = github_json(f"/repos/{quoted}/releases?per_page=100")
    latest = github_json(f"/repos/{quoted}/releases/latest")
    if not isinstance(releases, list) or not isinstance(latest, dict):
        raise SystemExit(f"Unexpected GitHub response for {repository}")
    return releases, str(latest.get("tag_name", ""))


def asset_checksum_metadata(release: dict[str, Any], asset: dict[str, Any]) -> tuple[str, str]:
    digest = str(asset.get("digest") or "")
    if digest.startswith("sha256:"):
        checksum = digest.removeprefix("sha256:")
        if re.fullmatch(r"[0-9a-fA-F]{64}", checksum):
            return checksum.lower(), ""

    checksum_name = f"{asset['name']}.sha256"
    checksum_asset = next(
        (
            candidate
            for candidate in release.get("assets", [])
            if candidate.get("name") == checksum_name
        ),
        None,
    )
    if not checksum_asset:
        raise SystemExit(f"Release asset {asset['name']} has no SHA-256 digest")
    return "", str(checksum_asset["browser_download_url"])


def fetch_asset_checksum(item: dict[str, str]) -> str:
    if item.get("sha256"):
        return item["sha256"]
    checksum_text = read_text_url(item["checksum_url"])
    match = re.search(r"\b([0-9a-fA-F]{64})\b", checksum_text)
    if not match:
        raise SystemExit(f"Invalid SHA-256 file for {item['version']}")
    return match.group(1).lower()


def kimi_catalog(asset_name: str) -> tuple[list[dict[str, str]], str]:
    releases, latest_tag = release_list(KIMI_REPOSITORY)
    catalog: list[dict[str, str]] = []

    for release in releases:
        if release.get("draft") or release.get("prerelease"):
            continue
        tag = str(release.get("tag_name", ""))
        match = KIMI_TAG_RE.fullmatch(tag)
        if not match:
            continue
        asset = next(
            (
                candidate
                for candidate in release.get("assets", [])
                if candidate.get("name") == asset_name
            ),
            None,
        )
        if not asset:
            continue
        asset_url = str(asset["browser_download_url"])
        expected_prefix = "https://github.com/MoonshotAI/kimi-code/releases/download/"
        if not asset_url.startswith(expected_prefix):
            raise SystemExit(f"Unexpected Kimi release URL: {asset_url}")
        checksum, checksum_url = asset_checksum_metadata(release, asset)
        catalog.append(
            {
                "version": match.group(1),
                "url": asset_url,
                "sha256": checksum,
                "checksum_url": checksum_url,
            }
        )

    catalog.sort(key=lambda item: semver_key(item["version"]), reverse=True)
    latest_match = KIMI_TAG_RE.fullmatch(latest_tag)
    latest = latest_match.group(1) if latest_match else ""
    if catalog and latest not in {item["version"] for item in catalog}:
        latest = catalog[0]["version"]
    return catalog, latest


def add_installed_entry(
    catalog: list[dict[str, str]], installed: str, state: dict[str, str]
) -> list[dict[str, str]]:
    """Offer the version already on disk, but only while its provenance is still provable.

    An entry without a url and a digest cannot be re-verified, and every later consumer indexes
    ``item["url"]`` unconditionally, so an unverifiable install is left out of the menu rather
    than offered as a choice that would crash on selection.
    """
    if not installed or any(item["version"] == installed for item in catalog):
        return catalog

    url = state.get("KIMI_CODE_ASSET_URL", "")
    checksum = state.get("KIMI_CODE_ASSET_SHA256", "")
    if not url or not re.fullmatch(r"[0-9a-f]{64}", checksum):
        return catalog
    return [*catalog, {"version": installed, "url": url, "sha256": checksum}]


def visible_choices(catalog: list[dict[str, str]], installed: str) -> list[dict[str, str]]:
    catalog = sorted(
        catalog,
        key=lambda item: semver_key(item["version"]),
        reverse=True,
    )
    first_ten = catalog[:10]
    if not installed or any(item["version"] == installed for item in first_ten):
        return first_ten

    installed_item = next(
        (item for item in catalog if item["version"] == installed),
        None,
    )
    if installed_item is None:
        return first_ten
    return sorted(
        [*catalog[:9], installed_item],
        key=lambda item: semver_key(item["version"]),
        reverse=True,
    )


def choose(
    product: str,
    platform_label: str,
    catalog: list[dict[str, str]],
    latest: str,
    installed: str,
    override: str,
    non_interactive: bool,
    view: View | None = None,
) -> dict[str, str]:
    """Pick one release, in a modal step like every other menu in the launcher.

    ``view`` is where this step sits in the launch sequence, which only ``start.sh`` knows. Left
    out, the step renders alone and offers no Back, which is the honest default for a process that
    cannot reach another process's earlier step.
    """
    by_version = {item["version"]: item for item in catalog}
    if override:
        try:
            semver_key(override)
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
        if override not in by_version:
            raise SystemExit(
                f"Requested {product} version {override} is not available for {platform_label}"
            )
        return by_version[override]

    choices = visible_choices(catalog, installed)
    if not choices:
        raise SystemExit(f"No compatible {product} releases were found")
    default_version = installed if installed in by_version else latest
    if default_version not in {item["version"] for item in choices}:
        default_version = choices[0]["version"]

    if non_interactive:
        return next(item for item in choices if item["version"] == default_version)
    if not sys.stdin.isatty():
        raise SystemExit(
            f"Cannot select {product} without a terminal; set explicit version "
            "environment variables or use --non-interactive"
        )

    step = ListStep(
        title=f"{product} version",
        prompt=f"Select {product} for {platform_label}",
        mode=SINGLE,
        choices=[_release(item, latest, installed) for item in choices],
        previous=[default_version],
    )
    result = run(step, view if view is not None else View())
    if result.status == flow.GO_BACK:
        # This selector is its own process, so the previous step is not in here to be reached. The
        # caller turns this into the status the launcher's loop reads and tells the flow where to
        # land, because only the caller knows which step of the launch this menu is.
        raise flow.BackRequested
    if not result.accepted:
        raise SystemExit(result.status)
    return by_version[str(result.value)]


def _release(item: dict[str, str], latest: str, installed: str) -> Choice:
    """One release row, with whatever the launcher already knows about it in the hint column.

    Both marks can appear on one row, which is why they are joined rather than chosen between: the
    newest release is usually also the installed one, and saying only half of that would be wrong.
    """
    labels = []
    if item["version"] == latest:
        labels.append("latest")
    if item["version"] == installed:
        labels.append("installed")
    return Choice(
        id=item["version"],
        label=item["version"],
        hint=f"({') ('.join(labels)})" if labels else "",
    )


def write_environment(path: Path, values: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    content = "".join(f"{key}={shlex.quote(value)}\n" for key, value in values.items())
    temporary.write_text(content, encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(path)


def running_flow() -> flow.Flow | None:
    """The launch this selector is one step of, when the launcher said where that lives.

    There is no ``--runtime-dir`` here because this selector predates the flow and is also run by
    hand; the environment variable the launcher already exports for every other step carries it
    instead. Absent is not an error: a bare invocation has no earlier step to go back to, so it
    numbers itself alone, exactly as it did before there was a flow.
    """
    directory = os.environ.get("HARNESS_RUNTIME_DIR", "").strip()
    return flow.running(directory) if directory else None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--platform-label", required=True)
    parser.add_argument("--kimi-asset", required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--installed-kimi", default="")
    parser.add_argument("--non-interactive", action="store_true")
    args = parser.parse_args()

    launch = running_flow()
    override = os.environ.get("KIMI_CODE_VERSION", "").strip()
    if launch and not launch.should_render(flow.KIMI_VERSION):
        # A replay: this pass renders no screen because a previous one already asked and the answer
        # is in the file this step owns.
        carried = load_state(args.output)
        if all(carried.get(key, "").strip() for key in KIMI_KEYS):
            # Nothing to do, and nothing to fetch: the digest in that file was verified on the pass
            # that wrote it, and the operator confirmed the version it belongs to. Asking the API
            # again to re-derive a settled answer would also risk offering a release published in
            # the meantime, and re-writing the file would overwrite the answer with a default.
            return
        # A half-written file is not an answer. Forgetting the step makes this pass ask again,
        # which is the only way to end up with a complete one.
        launch.forget(flow.KIMI_VERSION)

    recorded = load_state(args.state)
    kimi, latest_kimi = kimi_catalog(args.kimi_asset)
    kimi = add_installed_entry(kimi, args.installed_kimi, recorded)

    # Whether this step would put a screen on the terminal, which is the question the flow needs
    # answered before it can number the steps: a version named in the environment or a launch that
    # was told not to ask has no screen in it, and counting one would leave a hole in the rail.
    asking = not override and not args.non_interactive
    rendering = launch.plan(flow.KIMI_VERSION, 1 if asking else 0) if launch else asking
    view = None
    if rendering:
        position, total = launch.rail(flow.KIMI_VERSION) if launch else (1, 1)
        view = View(
            position=position,
            total=total,
            can_go_back=bool(launch and launch.previous(flow.KIMI_VERSION)),
        )
    try:
        selected_kimi = choose(
            "Kimi Code",
            args.platform_label,
            kimi,
            latest_kimi,
            args.installed_kimi,
            override,
            args.non_interactive,
            view,
        )
    except flow.BackRequested:
        # This menu is one screen with nothing earlier inside it, so Back means the previous step of
        # the launch, and the pass has to be restarted from the top to get there.
        flow.back_from(launch, flow.KIMI_VERSION)
    selected_kimi["sha256"] = fetch_asset_checksum(selected_kimi)

    # The session file belongs to the launch, not to this step: the module selector writes its own
    # keys to the same path afterwards. Keeping the rest of it is what lets a launch that went
    # backwards and came forward again still have the answers it is not going to ask for twice.
    values = load_state(args.output)
    values.update(
        {
            "KIMI_CODE_VERSION": selected_kimi["version"],
            "KIMI_CODE_ASSET_URL": selected_kimi["url"],
            "KIMI_CODE_ASSET_SHA256": selected_kimi["sha256"],
        }
    )
    write_environment(args.output, values)

    if launch and asking:
        # Only a step that could have been asked commits, so that walking back to it re-opens a
        # question rather than replaying a value nobody confirmed on this pass.
        launch.commit(flow.KIMI_VERSION, selected_kimi["version"])


if __name__ == "__main__":
    main()
