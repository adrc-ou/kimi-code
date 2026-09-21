"""Validate ComfyUI backend pins, resolver pin, and per-release certification records."""

import hashlib
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PINNED_REQUIREMENT = re.compile(r"^[A-Za-z0-9_.-]+==[^;\s]+(?:\s*;.*)?$")
SEMVER = re.compile(r"v\d+\.\d+\.\d+$")
# Each profile names the files its lock is built from, and each baseline records their digests. The
# field pairs the two halves of that agreement, so a rename in one place fails here rather than
# installing a lock that no longer matches what was reviewed.
BASELINE_DIGESTS = {
    "backend_lock_sha256": "backend_lock",
    "custom_requirements_sha256": "custom_requirements",
    "requirements_lock_sha256": "baseline_lock",
    "custom_lock_sha256": "custom_lock",
}
PROFILE_PATHS = (
    "backend_lock",
    "torch_constraints",
    "baseline_lock",
    "custom_requirements",
    "custom_lock",
)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def check_python(downloads):
    python = downloads["macos-python"]
    if not re.fullmatch(r"3\.12\.\d+", python["version"]):
        raise SystemExit("managed MPS Python must remain on 3.12")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", python["digest"]):
        raise SystemExit("invalid managed Python digest")
    if not re.fullmatch(
        r"https://github.com/astral-sh/python-build-standalone/releases/download/"
        r"\d{8}/cpython-"
        + re.escape(python["version"])
        + r"%2B\d{8}-aarch64-apple-darwin-install_only.tar.gz",
        python["url"],
    ):
        raise SystemExit("managed Python must use a versioned official arm64 release")


def check_resolver(downloads):
    """The resolver is pinned like any other download, because it decides the lock's contents.

    Version and digest have to name the same image: the launcher pulls by digest so a tag cannot be
    retargeted, but it reports the version, and the two agreeing is the only thing that makes the
    reported number true.
    """
    resolver = downloads["uv-resolver"]
    version = resolver["version"]
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise SystemExit("pinned uv resolver must be an exact release version")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", resolver["digest"]):
        raise SystemExit("invalid uv resolver image digest")
    if resolver["image"] != f"ghcr.io/astral-sh/uv:{version}":
        raise SystemExit("uv resolver must be pinned to the official ghcr.io image and its version")


def check_profile(entry):
    platform = entry.get("platform", "?")
    if entry.get("installer") not in {"cuda", "mps"}:
        raise SystemExit(f"{platform}: installer must be cuda or mps")
    if not str(entry.get("repository", "")).strip():
        raise SystemExit(f"{platform}: a profile that names no repository cannot fetch releases")
    for field in PROFILE_PATHS:
        relative = entry.get(field, "")
        if not relative or not (ROOT / relative).is_file():
            raise SystemExit(f"{platform}: {field}={relative!r} is not a shipped file")
    # The version a profile advertises and the version the constraint layer will actually resolve
    # are read by different code, so a mismatch would show up as an install failing a torch check.
    pins = dict(
        line.split("=", 1)
        for line in (ROOT / entry["backend_lock"]).read_text().splitlines()
        if "=" in line and not line.startswith("#")
    )
    for field, variable in (
        ("torch", "TORCH_VERSION"),
        ("torchvision", "TORCHVISION_VERSION"),
        ("torchaudio", "TORCHAUDIO_VERSION"),
    ):
        if entry.get(field) != pins.get(variable):
            raise SystemExit(
                f"{platform}: {field} does not match {variable} in {entry['backend_lock']}"
            )


def check_baseline(entry):
    baseline = entry.get("baseline")
    if not isinstance(baseline, dict):
        raise SystemExit(f"{entry['platform']}: a profile without a baseline has no shipped lock")
    check_release(baseline, entry["platform"])
    for field, path_field in BASELINE_DIGESTS.items():
        if baseline.get(field) != digest(ROOT / entry[path_field]):
            raise SystemExit(
                f"compatibility catalog digest is stale: {entry['platform']} {path_field}"
            )


def check_release(record, platform):
    """One release this file can name without asking the source, and the evidence behind it."""
    status = record.get("status")
    if status not in {"locked", "tested"}:
        raise SystemExit(f"{platform}: compatibility record is neither locked nor tested")
    if status == "tested" and "certified_at" not in record:
        raise SystemExit(f"{platform}: tested compatibility record lacks certification evidence")
    if not SEMVER.fullmatch(str(record.get("comfyui_version", ""))):
        raise SystemExit(f"{platform}: compatibility record needs a v-prefixed version")
    if not re.fullmatch(r"[0-9a-f]{40}", str(record.get("comfyui_commit", ""))):
        raise SystemExit(f"{platform}: compatibility record needs an immutable commit")
    if not re.fullmatch(r"[0-9a-f]{64}", str(record.get("requirements_sha256", ""))):
        raise SystemExit(f"{platform}: compatibility record needs the requirements digest")


def check_records(document):
    """Releases that are known but not shipped, so their lock has to be resolved on demand.

    These carry no digests of repository files, because nothing of theirs is committed here: what
    makes one trustworthy is the commit and the requirements digest, which is exactly what the
    resolver re-verifies before it uses them.
    """
    for record in document.get("records", []):
        check_release(record, record.get("platform", "?"))


def check_custom(custom_input):
    for number, raw in enumerate(custom_input.read_text().splitlines(), start=1):
        value = raw.strip()
        if value and not value.startswith("#") and not PINNED_REQUIREMENT.fullmatch(value):
            raise SystemExit(
                f"custom requirement must use an exact == pin ({custom_input}:{number})"
            )


def main():
    lock = json.loads((ROOT / "dependencies.lock.json").read_text())
    check_python(lock["downloads"])
    check_resolver(lock["downloads"])
    for relative, expected in lock["files"].items():
        if expected != f"sha256:{digest(ROOT / relative)}":
            raise SystemExit(f"locked file digest is stale: {relative}")
    compatibility = json.loads((ROOT / "backend" / "compatibility.json").read_text())
    profiles = compatibility.get("platforms", [])
    if not profiles:
        raise SystemExit("no ComfyUI backend profiles are declared")
    for entry in profiles:
        check_profile(entry)
        check_baseline(entry)
    check_records(compatibility)
    for entry in profiles:
        check_custom(ROOT / entry["custom_requirements"])


if __name__ == "__main__":
    main()
