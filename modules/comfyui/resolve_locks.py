#!/usr/bin/env python3
"""Make sure a hash-pinned dependency lock exists for the ComfyUI release this launch picked.

``resolution.py`` works out what a lock has to be built from and whether one already is; this is the
part that actually obtains it, and it has exactly one job that the installers depend on: hand back a
path whose contents were resolved from the release that was chosen, or refuse.

Resolution runs in a digest-pinned container by default. That is deliberate — uv is a build tool,
and installing one on the operator's machine to answer a question the harness asked would make the
host part of the reproducibility story. Setting ``COMFYUI_UV_BIN`` points the same plan at a
native uv instead, for reviewing a lock without pulling an image; the resolver's version is part of
the lock key either way, so the two can never stand in for each other.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import resolution  # noqa: E402

MODULE = Path(__file__).resolve().parent
ROOT = MODULE.parents[1]
BACKEND = MODULE / "backend"
RAW_BASE = "https://raw.githubusercontent.com"
REPOSITORY = "Comfy-Org/ComfyUI"
UV_VERSION_RE = re.compile(r"uv (\S+)")


def examine(text: str, name: str, requirements_sha256: str, prof: dict[str, Any]) -> list[str]:
    """Everything wrong with a lock body that is about to be installed, header included.

    One function so the resolved lock, the shipped baseline, and a lock under review are all held to
    the same standard rather than to whichever one the author happened to test.
    """
    return resolution.check_text(text, requirements_sha256, name) + resolution.check_torch_text(
        text, resolution.profile_file(prof, "backend_lock"), name
    )


def resolver_pin() -> dict[str, str]:
    """The pinned uv the harness trusts to resolve, from the module's dependency lock."""
    lock = json.loads((MODULE / "dependencies.lock.json").read_text(encoding="utf-8"))
    entry = lock.get("downloads", {}).get("uv-resolver")
    if not isinstance(entry, dict):
        raise SystemExit("dependencies.lock.json does not pin a uv-resolver")
    version = str(entry.get("version", ""))
    digest = str(entry.get("digest", ""))
    image = str(entry.get("image", ""))
    if not version or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise SystemExit("The pinned uv resolver needs a version and a sha256 image digest")
    if not image.startswith("ghcr.io/astral-sh/uv:"):
        raise SystemExit("The uv resolver must come from the official ghcr.io repository")
    # The container is pulled by digest but named to the operator, and into every lock key, by
    # version. One that could name a different release than it fetches would make the key a claim
    # about a resolver that never ran.
    if image != f"ghcr.io/astral-sh/uv:{version}":
        raise SystemExit(
            f"The uv resolver image is tagged for another version: {image} against {version}"
        )
    return {"version": version, "digest": digest, "image": image}


def container_argv(plan: dict[str, Any], pin: dict[str, str]) -> list[str]:
    """Wrap the uv plan in a container run.

    Only the harness's own ComfyUI scratch directory and the reviewed backend directory are mounted,
    both at their host paths, so the one argv built in ``resolution.plan`` is valid inside and
    outside the container and there is no second set of paths to keep in step. The instance root is
    deliberately *not* mounted: it holds the provider key and the proxy token, and resolution needs
    neither. ``--entrypoint`` is named explicitly because the uv image's own entrypoint is not part
    of its published contract.
    """
    mount = Path(plan["mount"])
    return [
        "docker",
        "run",
        "--rm",
        "--entrypoint",
        "uv",
        "-v",
        f"{mount}:{mount}",
        "-v",
        f"{BACKEND}:{BACKEND}:ro",
        "-w",
        str(mount),
        f"ghcr.io/astral-sh/uv@{pin['digest'].removeprefix('sha256:')}",
    ] + [str(part) for part in plan["argv"][1:]]


def native_version(uv: str) -> str:
    try:
        output = subprocess.run(
            [uv, "--version"], capture_output=True, text=True, check=True, timeout=60
        ).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        raise SystemExit(f"Unable to run {uv} --version: {exc}") from exc
    match = UV_VERSION_RE.search(output)
    if not match:
        raise SystemExit(f"{uv} --version did not report a version: {output.strip()!r}")
    return match.group(1)


def fetch_requirements(commit: str) -> str:
    """``requirements.txt`` at one commit, read by commit so the tree cannot change under it."""
    url = f"{RAW_BASE}/{REPOSITORY}/{commit}/requirements.txt"
    request = urllib.request.Request(url, headers={"User-Agent": "adrc-kimi-harness-lock-resolver"})
    try:
        with urllib.request.urlopen(request, timeout=60, context=None) as response:
            body = response.read(64 * 1024 + 1)
    except OSError as exc:
        raise SystemExit(
            f"Unable to fetch ComfyUI requirements at {commit[:12]}: {exc}\n"
            "A release this harness holds no lock for cannot be installed without its dependency "
            "set, and that set can only come from the release itself."
        ) from exc
    if len(body) > 64 * 1024:
        raise SystemExit(f"ComfyUI requirements at {commit[:12]} exceed 64 KiB")
    return body.decode("utf-8")


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    temporary.write_text(text, encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(path)


def already_resolved(plan: dict[str, Any], requirements_sha256: str) -> Path | None:
    """A lock this instance already built and verified, if it is still fit to install."""
    lock: Path = plan["lock"]
    if not lock.is_file():
        return None
    problems = examine(
        lock.read_text(encoding="utf-8"),
        lock.name,
        requirements_sha256,
        plan["profile"],
    )
    if problems:
        # A lock that stopped fitting is deleted rather than silently reinstalled: it is the record
        # of one resolution attempt, and re-running is the only way to get a new one.
        print(f"Discarding unfit lock {lock.name}:", file=sys.stderr)
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        lock.unlink(missing_ok=True)
        return None
    return lock


def materialise_baseline(plan: dict[str, Any], requirements_sha256: str) -> Path | None:
    """Record the shipped lock as this release's lock, when this release is the baseline.

    Copying rather than pointing at the file keeps one rule for the installers — a lock always lives
    under the instance directory and always carries the header naming what it was built from — and
    it means the default launch resolves nothing, which is the difference between an instant start
    and a network round trip on every boot.
    """
    prof = plan["profile"]
    shipped = resolution.shipped_lock(prof, requirements_sha256)
    if shipped is None or not shipped.is_file():
        return None
    # The header is added before the check, because the claim it carries is the whole point of the
    # check: this file is being adopted as the lock for these exact requirements.
    combined = plan["header"] + shipped.read_text(encoding="utf-8")
    problems = examine(combined, str(shipped.relative_to(ROOT)), requirements_sha256, prof)
    if problems:
        print("The shipped baseline lock does not validate:", file=sys.stderr)
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        raise SystemExit(f"The reviewed lock {shipped} is unfit to install")
    write(Path(plan["lock"]), combined)
    return plan["lock"]


def ensure(args: argparse.Namespace) -> Path:
    """The lock this launch installs with, resolved if necessary."""
    pin = resolver_pin()
    if args.native_uv:
        resolver = f"uv@{native_version(args.native_uv)}"
    else:
        resolver = f"uv@{pin['version']}"
    plan = resolution.plan(
        args.platform,
        args.version,
        args.commit,
        args.requirements_sha256,
        args.runtime_dir,
        resolver,
        uv=args.native_uv or "uv",
    )
    existing = already_resolved(plan, args.requirements_sha256)
    if existing is not None:
        print(f"lock     {existing} (already resolved)")
        return existing

    baseline = materialise_baseline(plan, args.requirements_sha256)
    if baseline is not None:
        print(f"lock     {baseline} (the reviewed baseline lock)")
        return baseline

    body = args.requirements_file.read_text(encoding="utf-8") if args.requirements_file else (
        fetch_requirements(args.commit)
    )
    actual = resolution.digest_of(body)
    if actual != args.requirements_sha256:
        # This is the binding between a lock and a release. If the file the resolver is about to
        # read is not the file the picker verified, every hash in the output describes some other
        # release's dependency set, and nothing downstream would be able to tell.
        raise SystemExit(
            f"ComfyUI {args.version} requirements changed since the picker verified them:\n"
            f"  expected {args.requirements_sha256}\n  got      {actual}\n"
            "Re-run the launcher so the version is confirmed against the current release."
        )
    write(Path(plan["source"]), body)
    argv = (
        [str(part) for part in plan["argv"]]
        if args.native_uv
        else container_argv(plan, pin)
    )
    print(f"resolving {args.platform} lock for {args.version} with {resolver}")
    completed = subprocess.run(argv, capture_output=True, text=True)
    if completed.returncode != 0:
        sys.stderr.write(completed.stdout)
        sys.stderr.write(completed.stderr)
        raise SystemExit(
            f"Dependency resolution failed for ComfyUI {args.version} on {args.platform} "
            f"(exit {completed.returncode})."
        )
    if not Path(plan["lock"]).is_file():
        raise SystemExit("The resolver reported success but wrote no lock file")

    resolved = Path(plan["lock"]).read_text(encoding="utf-8")
    # The header is what makes a lock self-describing later, when the profile that produced it may
    # have moved on; uv's own header records only the command line.
    final = plan["header"] + resolved
    write(Path(plan["lock"]), final)
    problems = examine(final, Path(plan["lock"]).name, args.requirements_sha256, plan["profile"])
    if problems:
        Path(plan["lock"]).unlink(missing_ok=True)
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        raise SystemExit(
            f"The lock resolved for ComfyUI {args.version} is not installable and was discarded."
        )
    print(f"lock     {plan['lock']}")
    return Path(plan["lock"])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--requirements-sha256", required=True)
    parser.add_argument("--runtime-dir", type=Path, required=True)
    parser.add_argument(
        "--requirements-file",
        type=Path,
        help="Use this requirements.txt instead of fetching it, to review a lock offline.",
    )
    parser.add_argument(
        "--native-uv",
        default=os.environ.get("COMFYUI_UV_BIN", "").strip(),
        help="Run this uv instead of the pinned container.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Write the resulting lock path here, for a caller that needs the answer verbatim.",
    )
    args = parser.parse_args()
    for name, value in (("commit", args.commit), ("requirements-sha256", args.requirements_sha256)):
        if not re.fullmatch(r"[0-9a-f]{40}" if name == "commit" else r"[0-9a-f]{64}", value):
            raise SystemExit(f"--{name} must be a lowercase hex digest")
    lock = ensure(args)
    if args.output is not None:
        # A path the caller will read back has to be absolute: the resolver's own output is relative
        # to wherever the launcher happened to start, and a build context cannot be a maybe-path.
        args.output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        args.output.write_text(f"{lock.resolve()}\n", encoding="utf-8")


if __name__ == "__main__":
    main()
