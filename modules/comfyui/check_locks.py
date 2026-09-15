"""Validate ComfyUI backend pins and certification records."""

import hashlib
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PINNED_REQUIREMENT = re.compile(r"^[A-Za-z0-9_.-]+==[^;\s]+(?:\s*;.*)?$")


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    lock = json.loads((ROOT / "dependencies.lock.json").read_text())
    python = lock["downloads"]["macos-python"]
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
    for relative, expected in lock["files"].items():
        if expected != f"sha256:{digest(ROOT / relative)}":
            raise SystemExit(f"locked file digest is stale: {relative}")
    compatibility = json.loads((ROOT / "backend" / "compatibility.json").read_text())
    for entry in compatibility["entries"]:
        if entry["status"] not in {"locked", "tested"}:
            raise SystemExit("compatibility entry is neither locked nor tested")
        if entry["status"] == "tested" and "certified_at" not in entry:
            raise SystemExit("tested compatibility entry lacks certification evidence")
        if entry["backend_lock_sha256"] != digest(ROOT / "backend" / "backend.env"):
            raise SystemExit("compatibility catalog backend lock digest is stale")
        if entry["custom_requirements_sha256"] != digest(
            ROOT / "backend" / "requirements-custom.txt"
        ):
            raise SystemExit("compatibility catalog custom requirements digest is stale")
        platform_lock = (
            "requirements-linux.lock"
            if entry["platform"] == "wsl2-x86_64"
            else "requirements-macos.lock"
        )
        if entry["requirements_lock_sha256"] != digest(ROOT / "backend" / platform_lock):
            raise SystemExit(f"compatibility catalog digest is stale: {platform_lock}")
        if entry["custom_lock_sha256"] != digest(ROOT / "backend" / "requirements-custom.lock"):
            raise SystemExit("compatibility catalog custom lock digest is stale")
    custom_input = ROOT / "backend" / "requirements-custom.txt"
    for number, raw in enumerate(custom_input.read_text().splitlines(), start=1):
        value = raw.strip()
        if value and not value.startswith("#") and not PINNED_REQUIREMENT.fullmatch(value):
            raise SystemExit(
                f"custom requirement must use an exact == pin ({custom_input}:{number})"
            )


if __name__ == "__main__":
    main()
