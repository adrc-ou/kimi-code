#!/usr/bin/env python3

"""Fetch trusted release metadata and interactively select runtime versions."""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


GITHUB_API = "https://api.github.com"
KIMI_REPOSITORY = "MoonshotAI/kimi-code"
COMFY_REPOSITORY = "Comfy-Org/ComfyUI"
SEMVER_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")
KIMI_TAG_RE = re.compile(r"^@moonshot-ai/kimi-code@(\d+\.\d+\.\d+)$")


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
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    except (urllib.error.URLError, json.JSONDecodeError) as exc:
        raise SystemExit(f"Unable to fetch GitHub release metadata: {exc}") from exc


def read_text_url(url: str) -> str:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "adrc-kimi-harness-version-selector"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.read().decode("utf-8")
    except (urllib.error.URLError, UnicodeDecodeError) as exc:
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


def asset_checksum_metadata(
    release: dict[str, Any], asset: dict[str, Any]
) -> tuple[str, str]:
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
        expected_prefix = (
            "https://github.com/MoonshotAI/kimi-code/releases/download/"
        )
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


def comfy_catalog() -> tuple[list[dict[str, str]], str]:
    releases, latest = release_list(COMFY_REPOSITORY)
    catalog: list[dict[str, str]] = []
    for release in releases:
        if release.get("draft") or release.get("prerelease"):
            continue
        tag = str(release.get("tag_name", ""))
        if SEMVER_RE.fullmatch(tag):
            catalog.append({"version": tag})
    catalog.sort(key=lambda item: semver_key(item["version"]), reverse=True)
    if catalog and latest not in {item["version"] for item in catalog}:
        latest = catalog[0]["version"]
    return catalog, latest


def add_installed_entry(
    catalog: list[dict[str, str]],
    installed: str,
    state: dict[str, str],
    product: str,
) -> list[dict[str, str]]:
    if not installed or any(item["version"] == installed for item in catalog):
        return catalog

    if product == "Kimi Code":
        url = state.get("KIMI_CODE_ASSET_URL", "")
        checksum = state.get("KIMI_CODE_ASSET_SHA256", "")
        if not url or not re.fullmatch(r"[0-9a-f]{64}", checksum):
            return catalog
        return [*catalog, {"version": installed, "url": url, "sha256": checksum}]
    return [*catalog, {"version": installed}]


def visible_choices(
    catalog: list[dict[str, str]], installed: str
) -> list[dict[str, str]]:
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
) -> dict[str, str]:
    by_version = {item["version"]: item for item in catalog}
    if override:
        try:
            semver_key(override)
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
        if override not in by_version:
            raise SystemExit(
                f"Requested {product} version {override} is not available "
                f"for {platform_label}"
            )
        return by_version[override]

    choices = visible_choices(catalog, installed)
    if not choices:
        raise SystemExit(f"No compatible {product} releases were found")
    default_version = installed if installed in by_version else latest
    if default_version not in {item["version"] for item in choices}:
        default_version = choices[0]["version"]
    default_index = next(
        index
        for index, item in enumerate(choices, start=1)
        if item["version"] == default_version
    )

    if non_interactive:
        return choices[default_index - 1]
    if not sys.stdin.isatty():
        raise SystemExit(
            f"Cannot select {product} without a terminal; set explicit version "
            "environment variables or use --non-interactive"
        )

    print(f"\nSelect {product} for {platform_label}:\n")
    for index, item in enumerate(choices, start=1):
        labels = []
        if item["version"] == latest:
            labels.append("latest")
        if item["version"] == installed:
            labels.append("installed")
        suffix = f" ({') ('.join(labels)})" if labels else ""
        print(f"  {index:2}) {item['version']}{suffix}")

    while True:
        answer = input(f"\nChoice [{default_index}]: ").strip()
        if not answer:
            return choices[default_index - 1]
        if answer.isdigit() and 1 <= int(answer) <= len(choices):
            return choices[int(answer) - 1]
        print(f"Enter a number from 1 to {len(choices)}.", file=sys.stderr)


def resolve_comfy_commit(version: str) -> str:
    result = subprocess.run(
        [
            "git",
            "ls-remote",
            "https://github.com/Comfy-Org/ComfyUI.git",
            f"refs/tags/{version}",
            f"refs/tags/{version}^{{}}",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    direct = ""
    peeled = ""
    for line in result.stdout.splitlines():
        commit, ref = line.split("\t", 1)
        if ref.endswith("^{}"):
            peeled = commit
        else:
            direct = commit
    commit = peeled or direct
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise SystemExit(f"Could not resolve ComfyUI tag {version}")
    return commit


def write_environment(path: Path, values: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    content = "".join(f"{key}={shlex.quote(value)}\n" for key, value in values.items())
    temporary.write_text(content, encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(path)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--platform-key", required=True)
    parser.add_argument("--platform-label", required=True)
    parser.add_argument("--kimi-asset", required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--installed-kimi", default="")
    parser.add_argument("--installed-comfy", default="")
    parser.add_argument("--non-interactive", action="store_true")
    args = parser.parse_args()

    state = load_state(args.state)
    kimi, latest_kimi = kimi_catalog(args.kimi_asset)
    comfy, latest_comfy = comfy_catalog()
    kimi = add_installed_entry(
        kimi, args.installed_kimi, state, "Kimi Code"
    )
    comfy = add_installed_entry(
        comfy, args.installed_comfy, state, "ComfyUI"
    )

    selected_kimi = choose(
        "Kimi Code",
        args.platform_label,
        kimi,
        latest_kimi,
        args.installed_kimi,
        os.environ.get("KIMI_CODE_VERSION", "").strip(),
        args.non_interactive,
    )
    selected_comfy = choose(
        "ComfyUI",
        args.platform_label,
        comfy,
        latest_comfy,
        args.installed_comfy,
        os.environ.get("COMFYUI_VERSION", "").strip(),
        args.non_interactive,
    )
    selected_kimi["sha256"] = fetch_asset_checksum(selected_kimi)
    commit = resolve_comfy_commit(selected_comfy["version"])

    write_environment(
        args.output,
        {
            "PLATFORM_KEY": args.platform_key,
            "KIMI_CODE_VERSION": selected_kimi["version"],
            "KIMI_CODE_ASSET_URL": selected_kimi["url"],
            "KIMI_CODE_ASSET_SHA256": selected_kimi["sha256"],
            "COMFYUI_VERSION": selected_comfy["version"],
            "COMFYUI_COMMIT": commit,
        },
    )


if __name__ == "__main__":
    main()
