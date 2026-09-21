#!/usr/bin/env python3
"""Build the ComfyUI version menu from the releases this host platform can actually install.

The application release list and the dependency set are two different questions, and only the
first is shared between backends:

* ``Comfy-Org/ComfyUI`` publishes one set of tags for every platform, and both installers consume
  a git commit, so the *release* source is one repository for both paths. Its per-release assets
  are Windows portable bundles, which neither path can build from, so they are not the anchor.
* What genuinely differs per platform is where the *dependencies* come from: the containerised
  path resolves manylinux wheels against a CUDA index, the native path resolves macOS arm64 wheels
  from PyPI.

So a profile selects the resolver target, the constraint layer, and the baseline lock, while the
release listing stays common. The launcher's menu is identical either way, which is the point.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
import urllib.parse
from pathlib import Path
from typing import Any

# This runs as a script beside ``versions.py``, so both paths are added by hand exactly as that
# module adds them: ``scripts/`` for the shared release selector, and ``tools/`` for the flat
# ``tui`` import identity.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
from select_versions import (  # noqa: E402  (the paths above are what make this import work)
    SEMVER_RE,
    github_json,
    read_text_url,
    semver_key,
)

MODULE = Path(__file__).resolve().parent
COMPATIBILITY_PATH = MODULE / "backend" / "compatibility.json"
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
RAW_BASE = "https://raw.githubusercontent.com"
# How old a cached release listing may be and still answer without a request. The listing only
# feeds a menu, so a day is long enough to make an unchanged launch free of network use and short
# enough that a newly published release appears on the next day's launch.
LISTING_TTL_SECONDS = 24 * 60 * 60
# How far back to keep the listing. The menu shows a window of it, and an installed release older
# than that window is still offered, but a release this far behind has no lock and no reason to be
# chased.
LISTING_DEPTH = 60


def document(path: Path | None = None) -> dict[str, Any]:
    """The backend profile document, as data rather than as code.

    A module that grew a third backend, or that had to follow a release feed unlike ComfyUI's,
    should need an entry here rather than a new branch in the selector.
    """
    if path is None:
        path = COMPATIBILITY_PATH
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"Unable to read the ComfyUI backend profile: {exc}") from exc
    if not isinstance(loaded.get("platforms"), list) or not loaded["platforms"]:
        raise SystemExit("ComfyUI backend profile declares no platforms")
    return loaded


def profile(platform: str, path: Path | None = None) -> dict[str, Any]:
    """The backend profile this host installs on.

    A profile that cannot name the repository its releases come from is not a profile, and every
    consumer would otherwise discover that later, in a place that reads like a network failure.
    """
    for entry in document(path)["platforms"]:
        if isinstance(entry, dict) and entry.get("platform") == platform:
            if not str(entry.get("repository", "")).strip():
                raise SystemExit(
                    f"The ComfyUI backend profile for {platform!r} names no repository"
                )
            return entry
    raise SystemExit(f"No ComfyUI backend profile is declared for {platform!r}")


def recorded_releases(platform: str, path: Path | None = None) -> list[dict[str, str]]:
    """Releases this harness already names a commit and a requirements digest for.

    The baseline is the release the shipped lock was built from, and it is the only release that
    can be offered when the source is unreachable and nothing is cached. Records added by a
    certification run are offered alongside it, so a release that has aged out of the upstream
    listing does not become uninstallable.
    """
    doc = document(path)
    items: dict[str, dict[str, str]] = {}
    for entry in _records(doc, platform):
        item = _record_item(entry)
        if item is not None:
            items.setdefault(item["version"], item)
    return sorted(items.values(), key=lambda item: semver_key(item["version"]), reverse=True)


def _records(doc: dict[str, Any], platform: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for entry in doc["platforms"]:
        if isinstance(entry, dict) and entry.get("platform") == platform:
            baseline = entry.get("baseline")
            if isinstance(baseline, dict):
                out.append(baseline)
    for entry in doc.get("records", []):
        if isinstance(entry, dict) and entry.get("platform") in {platform, None}:
            out.append(entry)
    return out


def _record_item(entry: dict[str, Any]) -> dict[str, str] | None:
    """One recorded release, or nothing if it cannot be re-verified.

    A record without a 40-hex commit is not installable by either installer, and one without a
    requirements digest cannot be matched to a dependency lock, so an incomplete record is dropped
    rather than offered as a row that would fail on selection.
    """
    if str(entry.get("status", "")) not in {"locked", "tested"}:
        return None
    version = str(entry.get("comfyui_version", ""))
    commit = str(entry.get("comfyui_commit", ""))
    digest = str(entry.get("requirements_sha256", ""))
    if not SEMVER_RE.fullmatch(version) or not COMMIT_RE.fullmatch(commit):
        return None
    if not DIGEST_RE.fullmatch(digest):
        return None
    return {"version": version, "commit": commit, "requirements_sha256": digest}


def catalog_for(
    platform: str,
    *,
    profile_path: Path | None = None,
    cache: Path | None = None,
    now: float | None = None,
) -> tuple[list[dict[str, str]], str]:
    """The releases to offer for one platform, newest first, with the newest of them.

    Rows carry no commit unless this harness already recorded one: resolution is deferred to
    ``resolve`` for the release actually picked, so a launch that keeps its installed version
    spends one request on the listing and none on the rows the user scrolled past.
    """
    prof = profile(platform, profile_path)
    repository = str(prof["repository"])
    recorded = {item["version"]: item for item in recorded_releases(platform, profile_path)}
    try:
        versions, provenance = listing(repository, cache, now)
    except (SystemExit, OSError, ValueError):
        # Nothing reachable and nothing cached, so the only honest offer is the short list of
        # releases this repository itself names, marked as such rather than passed off as current.
        # `from None`: the fetch failure is expected here, and its traceback would bury the reason.
        catalog = [{**item, "provenance": "local"} for item in recorded.values()]
        if not catalog:
            raise SystemExit("No compatible ComfyUI releases were found") from None
        return catalog, catalog[0]["version"]

    catalog = []
    for version in versions:
        item = dict(recorded.pop(version, {}))
        item["version"] = version
        item["provenance"] = provenance
        catalog.append(item)
    # A recorded release that has aged out of the upstream listing stays installable, but it did
    # not come from that listing, so it carries the provenance of where it did come from.
    catalog.extend({**item, "provenance": "local"} for item in recorded.values())
    catalog.sort(key=lambda entry: semver_key(entry["version"]), reverse=True)
    return catalog, catalog[0]["version"]


def resolve(
    item: dict[str, str],
    platform: str,
    *,
    profile_path: Path | None = None,
    cache: Path | None = None,
    now: float | None = None,
) -> dict[str, str]:
    """Fill in the immutable commit and requirements digest of the release a row names.

    Two lookups, both about identity rather than about the menu: the tag's commit, because a tag
    can be moved and both installers check a SHA out, and the digest of that commit's
    ``requirements.txt``, which is what a dependency lock is keyed by. Both are cached, the digest
    forever because it is stored against the commit and so cannot go stale.
    """
    prof = profile(platform, profile_path)
    repository = str(prof["repository"])
    result = dict(item)
    if not COMMIT_RE.fullmatch(result.get("commit", "")):
        result["commit"] = _tag_commit(repository, result["version"])
    if not DIGEST_RE.fullmatch(result.get("requirements_sha256", "")):
        result["requirements_sha256"] = _requirements_digest(
            repository, result["commit"], prof, cache, now
        )
    return result


def _tag_commit(repository: str, version: str) -> str:
    """Resolve a release tag to the one commit it points at, peeling an annotated tag once."""
    quoted = urllib.parse.quote(repository, safe="/")
    ref = github_json(f"/repos/{quoted}/git/refs/tags/{urllib.parse.quote(version, safe='')}")
    obj = ref.get("object") if isinstance(ref, dict) else None
    if not isinstance(obj, dict):
        raise SystemExit(f"Unexpected reference response for {repository} {version}")
    if obj.get("type") == "tag":
        path = str(obj.get("url", "")).removeprefix("https://api.github.com")
        tag = github_json(path)
        obj = tag.get("object") if isinstance(tag, dict) else None
        if not isinstance(obj, dict):
            raise SystemExit(f"Unable to peel the {version} tag of {repository}")
    if obj.get("type") != "commit" or not COMMIT_RE.fullmatch(str(obj.get("sha", ""))):
        raise SystemExit(f"The {version} tag of {repository} does not resolve to a commit")
    return str(obj["sha"])


def _requirements_digest(
    repository: str, commit: str, prof: dict[str, Any], cache: Path | None, now: float | None
) -> str:
    """SHA-256 of ``requirements.txt`` at one commit: the identity of a dependency set.

    Read by commit rather than by tag on purpose. The digest has to describe the tree the
    installer checks out, and only the commit is guaranteed to still name it.
    """
    stored = _read_cache(cache)
    hit = str((stored or {}).get("requirements", {}).get(commit, ""))
    if DIGEST_RE.fullmatch(hit):
        return hit
    try:
        body = read_text_url(f"{RAW_BASE}/{repository}/{commit}/requirements.txt")
    except (SystemExit, OSError, ValueError):
        baseline = prof.get("baseline")
        # The shipped lock was built for one specific tree, so a launch that cannot fetch may still
        # name its digest. That relief is limited to exactly that tree: no other release gets a
        # digest it was not checked against. `from None` because the fetch failure is the expected
        # path offline, and its traceback would bury the message the operator has to act on.
        if isinstance(baseline, dict) and baseline.get("comfyui_commit") == commit:
            recorded = str(baseline.get("requirements_sha256", ""))
            if DIGEST_RE.fullmatch(recorded):
                return recorded
        raise SystemExit(
            f"Unable to read the requirements of ComfyUI {commit[:12]}. A release this harness "
            "holds no certified lock for cannot be installed without its dependency set, so "
            "either restore network access or choose a listed (baseline) release."
        ) from None
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    _remember_requirement(cache, repository, commit, digest, stored, now)
    return digest


def listing(
    repository: str, cache: Path | None = None, now: float | None = None
) -> tuple[list[str], str]:
    """(versions, provenance) for one repository, preferring the source over any cache.

    A cache younger than the TTL answers without a request, which is the reason for writing it. A
    request that fails falls back to whatever is cached however old, and says so in the
    provenance, because a launch must never depend on GitHub being reachable. With nothing cached
    the failure propagates, and the caller has the recorded releases to fall back on.
    """
    stored = _read_cache(cache)
    if stored is not None and _fresh(stored, now) and stored.get("repository") == repository:
        return [str(v) for v in stored["releases"]], "listed"
    try:
        versions = _fetch_releases(repository)
    except (SystemExit, OSError, ValueError):
        if stored is not None and stored.get("repository") == repository:
            return [str(v) for v in stored["releases"]], "stale"
        raise
    _write_cache(cache, repository, versions, {}, now if now is not None else time.time())
    return versions, "listed"


def _fetch_releases(repository: str) -> list[str]:
    """Stable release tags of one repository, newest first, as version strings."""
    quoted = urllib.parse.quote(repository, safe="/")
    releases = github_json(f"/repos/{quoted}/releases?per_page=100")
    if not isinstance(releases, list):
        raise SystemExit(f"Unexpected GitHub response for {repository}")
    versions: list[str] = []
    for release in releases:
        if not isinstance(release, dict) or release.get("draft") or release.get("prerelease"):
            continue
        # A draft or a pre-release is the publisher saying this is not a version to install, and a
        # tag outside stable three-part semver has no ordering this menu could reason about.
        tag = str(release.get("tag_name", ""))
        if SEMVER_RE.fullmatch(tag) and tag not in versions:
            versions.append(tag)
    versions.sort(key=semver_key, reverse=True)
    return versions[:LISTING_DEPTH]


def cache_path() -> Path | None:
    """Where the release cache lives, or nowhere if this run has no instance directory.

    Generated files belong to the instance runtime directory, so a hand-run invocation without one
    simply does not cache rather than writing somewhere unsanctioned.
    """
    runtime = os.environ.get("HARNESS_RUNTIME_DIR", "").strip()
    if not runtime:
        return None
    return Path(runtime) / "comfyui" / "releases.json"


def _read_cache(cache: Path | None) -> dict[str, Any] | None:
    if cache is None or not cache.is_file():
        return None
    try:
        loaded = json.loads(cache.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        # A cache that cannot be read is no answer at all, and the source is authoritative anyway.
        return None
    if not isinstance(loaded, dict) or not isinstance(loaded.get("releases"), list):
        return None
    return loaded


def _fresh(stored: dict[str, Any], now: float | None) -> bool:
    try:
        age = (now if now is not None else time.time()) - float(stored["fetched_at"])
    except (KeyError, TypeError, ValueError):
        return False
    return 0 <= age < LISTING_TTL_SECONDS


def _write_cache(
    cache: Path | None,
    repository: str,
    releases: list[str],
    requirements: dict[str, str],
    now: float,
) -> None:
    if cache is None:
        return
    _atomic(
        cache,
        {
            "repository": repository,
            "fetched_at": now,
            "releases": releases,
            "requirements": requirements,
        },
    )


def _remember_requirement(
    cache: Path | None,
    repository: str,
    commit: str,
    digest: str,
    stored: dict[str, Any] | None,
    now: float | None,
) -> None:
    """Add one commit's digest without changing how old the listing looks.

    Keyed by commit, so the entry stays true even if the tag that named it is later moved, and
    written without refreshing ``fetched_at``, because learning a digest is not a fresh listing.
    """
    if cache is None:
        return
    existing = stored if stored is not None else (_read_cache(cache) or {})
    if existing.get("repository") != repository or not isinstance(existing.get("releases"), list):
        return
    requirements = dict(existing.get("requirements") or {})
    requirements[commit] = digest
    _atomic(
        cache,
        {
            "repository": repository,
            "fetched_at": existing.get("fetched_at", now if now is not None else time.time()),
            "releases": existing["releases"],
            "requirements": requirements,
        },
    )


def _atomic(cache: Path, payload: dict[str, Any]) -> None:
    cache.parent.mkdir(parents=True, exist_ok=True)
    temporary = cache.with_name(f"{cache.name}.tmp.{os.getpid()}")
    temporary.write_text(json.dumps(payload, indent=1, sort_keys=True), encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(cache)
