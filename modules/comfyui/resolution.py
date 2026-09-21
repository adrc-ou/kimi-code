#!/usr/bin/env python3
"""Work out, and verify, the dependency lock that a chosen ComfyUI release installs with.

The release listing answers *which ComfyUI*. This answers *with what packages*, and it has to be a
separate question because upstream pins them exactly and moves those pins between releases. The
shipped ``requirements-*.lock`` files were compiled from one release's ``requirements.txt`` and are
installed with ``--require-hashes``, so installing them onto a different commit would either fail on
a hash mismatch or, worse, silently succeed against a dependency set nobody reviewed for that
release.

So a lock is keyed by everything that could change its contents:

* the release's own ``requirements.txt``, by digest rather than by version — a digest is what the
  pins actually moved when the version moved, and a moved tag cannot spoof it;
* the platform profile, because ``torch`` resolves to a CUDA wheel on one path and a macOS wheel on
  the other, and the resolver target and Python version are part of the answer;
* the reviewed constraint layer and the custom-node requirements, because those are precisely the
  inputs an operator edits when they want something new in the backend.

Anything downstream that wants a lock asks this module whether it exists, and runs the resolver only
when it does not. Resolution therefore costs real time exactly once per release per platform.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

MODULE = Path(__file__).resolve().parent
ROOT = MODULE.parents[1]
COMPATIBILITY_PATH = MODULE / "backend" / "compatibility.json"
# Every path a profile field holds — ``backend/backend.env``, ``backend/torch-constraints.txt``
# and so on — is relative to this module directory, which is also the spelling used by
# ``dependencies.lock.json`` and ``check_locks.py``. Resolving one anywhere else would silently
# look at a different file, so this is the only base they take.
PROFILE_ROOT = MODULE
# The files whose bytes go into the key, by profile field name. Order matters: the key is a
# digest of this concatenation, so reordering it would invalidate every cached lock for no reason.
KEYED_INPUTS = ("backend_lock", "torch_constraints", "custom_requirements")
PIN_RE = re.compile(r"^([A-Za-z0-9_.-]+)\s*(==|@)\s*(\S+)")
HASH_RE = re.compile(r"--hash=sha256:[0-9a-f]{64}")
# What uv writes above its own header; the resolver puts this first so a lock carries its own proof
# of what it was built from, readable without the profile or the runtime directory.
PROVENANCE_RE = re.compile(r"^# comfyui-requirements-sha256=([0-9a-f]{64})$", re.MULTILINE)


def digest_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def digest_of(text: str) -> str:
    """The digest of requirements held in memory, computed as one read from a file is.

    The picker digests the bytes it fetched and the resolver digests them again before using them;
    those two have to agree by construction rather than by both happening to encode alike.
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def profile(platform: str, path: Path | None = None) -> dict[str, Any]:
    if path is None:
        path = COMPATIBILITY_PATH
    document = json.loads(path.read_text(encoding="utf-8"))
    for entry in document.get("platforms", []):
        if isinstance(entry, dict) and entry.get("platform") == platform:
            return entry
    raise SystemExit(f"No ComfyUI backend profile is declared for {platform!r}")


def lock_key(
    prof: dict[str, Any],
    requirements_sha256: str,
    resolver: str,
    root: Path = PROFILE_ROOT,
) -> str:
    """Identify a dependency set by every input that could have changed it.

    ``requirements_sha256`` is the digest of the release's own ``requirements.txt``. The version
    string is deliberately not in the key: two releases that happen to pin the same requirements
    should share one lock, and that is exactly the case where re-resolving would be wasted work.

    ``resolver`` is the version of uv that produced the lock. Two resolvers can pick two different
    sets from one input, and a key that could not tell them apart would let a lock built by one
    answer for the other — which is a stale dependency set arriving under a name that implies it was
    just reviewed. Naming the resolver makes a version change a cache miss instead of a silent lie.

    The reviewed files in ``KEYED_INPUTS`` go in by digest even though only the constraint layer is
    handed to the resolver, because the release lock and the custom-node lock install into one
    environment, one after the other. A reviewed change to either is a change to what the pair has
    to be consistent with, so it has to invalidate the other.
    """
    if not re.fullmatch(r"[0-9a-f]{64}", requirements_sha256):
        raise SystemExit("ComfyUI requirements digest is malformed")
    if not resolver.strip():
        raise SystemExit("ComfyUI lock key needs the resolver that built it")
    parts = [
        "profile={}|{}|{}|python{}|backend={}".format(
            prof.get("platform", ""),
            prof.get("installer", ""),
            prof.get("resolver_platform", ""),
            prof.get("python", ""),
            prof.get("backend", ""),
        ),
        f"requirements={requirements_sha256}",
        f"resolver={resolver.strip()}",
    ]
    for field in KEYED_INPUTS:
        path = profile_file(prof, field, root)
        parts.append(f"{field}={digest_file(path)}")
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


def profile_file(prof: dict[str, Any], field: str, root: Path = PROFILE_ROOT) -> Path:
    """One file a profile names, resolved against the module and required to exist.

    A profile pointing at a file that is not shipped is a misconfiguration that would otherwise
    first show up as a pip failure halfway through an install, so it is stopped here instead.
    """
    relative = str(prof.get(field, ""))
    if not relative:
        raise SystemExit(f"ComfyUI profile {prof.get('platform', '?')} omits {field}")
    path = root / relative
    if not path.is_file():
        raise SystemExit(
            f"ComfyUI profile {prof.get('platform', '?')} names {relative}, which is not shipped"
        )
    return path


def locks_dir(runtime_dir: Path) -> Path:
    return runtime_dir / "comfyui" / "locks"


def lock_path(runtime_dir: Path, key: str) -> Path:
    return locks_dir(runtime_dir) / f"{key}.lock"


def provenance_header(
    *, platform: str, version: str, commit: str, requirements_sha256: str, key: str
) -> str:
    """The block the resolver writes above the lock, naming exactly what it was built for."""
    return "".join(
        f"# {line}\n"
        for line in (
            "Generated by the harness: reviewed inputs, resolved per release. Do not edit.",
            f"comfyui-platform={platform}",
            f"comfyui-version={version}",
            f"comfyui-commit={commit}",
            f"comfyui-requirements-sha256={requirements_sha256}",
            f"comfyui-lock-key={key}",
        )
    )


def plan(
    platform: str,
    version: str,
    commit: str,
    requirements_sha256: str,
    runtime_dir: Path,
    resolver: str,
    *,
    profile_path: Path | None = None,
    root: Path = PROFILE_ROOT,
    uv: str = "uv",
) -> dict[str, Any]:
    """What resolving this release means: where the lock goes and the command that builds it.

    The requirements file is staged into the instance runtime directory rather than handed to uv
    through a pipe, because the resolver runs in a container and a container mounts a directory, not
    a string. The constraint layer is deliberately *not* staged: it is read from the repository, so
    the bytes the lock was built from are the reviewed bytes rather than a copy that can drift.
    """
    prof = profile(platform, profile_path)
    key = lock_key(prof, requirements_sha256, resolver, root)
    constraints = profile_file(prof, "torch_constraints", root)
    # Absolute before anything is derived from it, so the one set of paths is valid for a native uv
    # and for the resolver container's bind mount, which cannot take a relative source.
    runtime_dir = Path(runtime_dir).resolve()
    stage = runtime_dir / "comfyui" / "resolve"
    source = stage / f"{requirements_sha256[:16]}.txt"
    output = lock_path(runtime_dir, key)
    # uv's cache goes under the instance directory so that re-resolving is quick without the
    # harness ever writing build state outside the disposable runtime tree.
    cache = runtime_dir / "comfyui" / "uv-cache"
    # Everything uv reads or writes sits under this one directory, which is therefore the only part
    # of the instance directory a resolver container ever sees. The instance root also holds the
    # provider key and the proxy token, and a build tool has no reason to be handed those.
    mount = runtime_dir / "comfyui"
    argv = [
        uv,
        "pip",
        "compile",
        str(source),
        "--constraint",
        str(constraints),
        "--python-version",
        str(prof["python"]),
        "--python-platform",
        str(prof["resolver_platform"]),
        "--generate-hashes",
        "--no-annotate",
        "--no-header",
        "--cache-dir",
        str(cache),
        "--output-file",
        str(output),
    ]
    return {
        "key": key,
        "lock": output,
        "source": source,
        "stage": stage,
        "cache": cache,
        "mount": mount,
        "argv": argv,
        "header": provenance_header(
            platform=platform,
            version=version,
            commit=commit,
            requirements_sha256=requirements_sha256,
            key=key,
        ),
        "profile": prof,
    }


def shipped_lock(prof: dict[str, Any], requirements_sha256: str) -> Path | None:
    """The lock committed beside this module, when this release is the one it was built from.

    The baseline is the only release the shipped file can answer for, and the digest is what proves
    that: it is the same test the resolver would otherwise redo, already passed once and recorded.
    Serving it without uv is what lets the default launch resolve nothing at all.
    """
    baseline = prof.get("baseline")
    if not isinstance(baseline, dict):
        return None
    if str(baseline.get("requirements_sha256", "")) != requirements_sha256:
        return None
    # Through ``profile_file`` rather than a bare join: this is the release that lock was built for,
    # so a declaration pointing at a file that is not here is a broken checkout, not a reason to
    # fall through and resolve a replacement for a reviewed artifact.
    return profile_file(prof, "baseline_lock")


def check(path: Path, requirements_sha256: str) -> list[str]:
    if not path.is_file():
        return [f"{path} does not exist"]
    return check_text(
        path.read_text(encoding="utf-8", errors="replace"), requirements_sha256, path.name
    )


def check_text(text: str, requirements_sha256: str, name: str) -> list[str]:
    """Every way a lock can be unfit to install, as messages rather than exceptions.

    Returning the problems is what lets one call report all of them: a lock that is both missing its
    provenance and short a hash is two findings, and a resolver that stopped at the first would make
    the operator fix and re-run twice. Taking text as well as a path is what lets the shipped
    baseline lock be examined before it is adopted rather than after it is installed.
    """
    problems: list[str] = []
    found = PROVENANCE_RE.search(text)
    if found is None:
        problems.append(f"{name} does not say which requirements it was resolved from")
    elif found.group(1) != requirements_sha256:
        problems.append(
            f"{name} was resolved from {found.group(1)[:12]}, not {requirements_sha256[:12]}"
        )
    if not _pins(text):
        problems.append(f"{name} is empty")
        return problems
    problems.extend(_missing_hashes(text, name))
    return problems


def _pins(text: str) -> list[re.Match[str]]:
    """The requirements a lock states, ignoring headers, blanks and indented continuation lines.

    A lock whose every line is a comment pins nothing and installs nothing, and neither the hash
    audit nor the torch audit below has anything to say about it, so emptiness is judged here, on
    the same skip rule those audits use.
    """
    found = []
    for line in text.splitlines():
        if not line or line[0] in "# \t":
            continue
        pin = PIN_RE.match(line)
        if pin is not None:
            found.append(pin)
    return found


def _resolved_versions(text: str) -> dict[str, str]:
    """What version a lock resolved each package to, in either spelling a resolver may choose.

    ``torch==2.11.0`` names it directly. A direct-URL pin, which is how every CUDA torch wheel
    arrives, hides it inside the wheel filename, where the local separator is percent-encoded, so
    ``2.11.0+cu130`` is read back out of ``torch-2.11.0%2Bcu130-cp312-...whl``. Both are answered
    by the release alone: which local build a platform wants is that platform's own constraint file,
    and the CUDA image asserts its ``+cu130`` flavour where that claim is load-bearing.
    """
    versions = {}
    for pin in _pins(text):
        name = _canonical(pin.group(1))
        if pin.group(2) == "==":
            versions[name] = pin.group(3).rstrip("\\").split("+")[0]
            continue
        # A wheel filename separates its fields with hyphens and spells its name with underscores,
        # so `torch` and `torch-2.11.0%2Bcu130-cp312-...whl` are the same package.
        fields = pin.group(3).rsplit("/", 1)[-1].split("#", 1)[0].split("-")
        if len(fields) > 1 and _canonical(fields[0]) == name:
            versions[name] = fields[1].replace("%2B", "+").split("+")[0]
    return versions


def _canonical(name: str) -> str:
    """A package name as both a requirement line and a wheel filename write it."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _missing_hashes(text: str, name: str) -> list[str]:
    """Flag any pinned requirement whose block carries no sha256 hash.

    ``pip --require-hashes`` needs a hash on every requirement, and it fails on the *first* one that
    lacks it, mid-install, after the wheels are downloaded. Finding that here costs one read.
    """
    problems = []
    lines = text.splitlines()
    for number, line in enumerate(lines):
        if not line or line[0] in "# \t":
            continue
        pin = PIN_RE.match(line)
        if pin is None:
            problems.append(f"{name}:{number + 1}: unparsable requirement {line!r}")
            continue
        block = [line]
        following = number + 1
        while following < len(lines) and lines[following].startswith(" "):
            block.append(lines[following])
            following += 1
        # A direct-URL pin carries its digest in the fragment rather than as a --hash line, and pip
        # accepts that form under --require-hashes, so it is not a missing hash.
        if pin.group(2) == "@" and "#sha256=" in line:
            continue
        if not any(HASH_RE.search(entry) for entry in block):
            problems.append(f"{name}:{number + 1}: {pin.group(1)} has no sha256 hash")
    return problems


def check_torch(path: Path, backend_env: Path) -> list[str]:
    if not path.is_file():
        return [f"{path} does not exist"]
    return check_torch_text(
        path.read_text(encoding="utf-8", errors="replace"), backend_env, path.name
    )


def check_torch_text(text: str, backend_env: Path, name: str) -> list[str]:
    """The reviewed torch pins have to be the ones the lock actually resolved.

    A constraint layer is only a request; a resolver that quietly picked a different torch would
    change GPU support, which is the single thing an operator cannot recover from after the image is
    built. The existing CUDA build did this with three greps that only understood a ``+cu130`` wheel
    URL; this covers the PyPI form the macOS lock uses too.
    """
    pins = {}
    for line in backend_env.read_text(encoding="utf-8").splitlines():
        key, _, value = line.partition("=")
        if key.strip() in {"TORCH_VERSION", "TORCHVISION_VERSION", "TORCHAUDIO_VERSION"}:
            pins[key.strip()] = value.strip()
    resolved = _resolved_versions(text)
    problems = []
    for package, variable in (
        ("torch", "TORCH_VERSION"),
        ("torchvision", "TORCHVISION_VERSION"),
        ("torchaudio", "TORCHAUDIO_VERSION"),
    ):
        version = pins.get(variable, "")
        if not version:
            problems.append(f"{backend_env} does not pin {variable}")
            continue
        # Read each package's own pin rather than searching the text for the version: torchvision
        # and torchaudio share torch's number, so a substring would let one answer for another, and
        # a lock missing a package entirely would pass.
        if resolved.get(_canonical(package), "") != version.split("+")[0]:
            problems.append(f"{name} did not resolve {package} to the reviewed {version}")
    return problems


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform", required=True)
    parser.add_argument("--requirements-sha256", required=True)
    parser.add_argument("--resolver", default="")
    parser.add_argument("--check", type=Path)
    args = parser.parse_args()
    found = profile(args.platform)
    print(f"profile   {found['installer']} resolver {found['resolver_platform']}")
    if args.resolver:
        print(f"lock key  {lock_key(found, args.requirements_sha256, args.resolver)}")
    if args.check:
        problems = check(args.check, args.requirements_sha256) + check_torch(
            args.check, PROFILE_ROOT / found["backend_lock"]
        )
        for problem in problems:
            print(f"problem   {problem}")
        raise SystemExit(1 if problems else 0)
