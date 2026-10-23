"""Root-only streaming receiver for approved extensions; embedded in the staging service."""

from __future__ import annotations

import argparse
import os
import shutil
import stat
import sys
import tarfile
from pathlib import Path, PurePosixPath

ROOTS = (".kimi-code/agents", ".agents/agents", ".kimi-code/skills", ".agents/skills",
         ".kimi-code/mcp.json")
VOLUME = "approved-extensions"
SERVICE = "extension-state-init"


def clear(root: Path) -> None:
    os.chmod(root, 0o700)
    for directory, _, _ in os.walk(root, followlinks=False):
        os.chmod(directory, 0o700)
    for path in root.iterdir():
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink()


def receive(root: Path, stream, gid: int) -> None:
    """Extract only ordinary approved files, without tar links or metadata restoration."""
    clear(root)
    with tarfile.open(fileobj=stream, mode="r|") as archive:
        for member in archive:
            path = PurePosixPath(member.name)
            name = path.as_posix()
            allowed = name in ROOTS or any(
                name.startswith(relative + "/") for relative in ROOTS[:-1]
            )
            if path.is_absolute() or ".." in path.parts or not allowed:
                raise ValueError("unexpected path in approved extension archive")
            expected_file = name == ROOTS[-1]
            if ((not member.isdir() and not member.isfile())
                    or (expected_file and not member.isfile())):
                raise ValueError("linked or special approved extension object")
            target = root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            if member.isdir():
                target.mkdir(exist_ok=True)
            else:
                descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                     0o400)
                with os.fdopen(descriptor, "wb") as output, archive.extractfile(member) as source:
                    shutil.copyfileobj(source, output)
                os.chmod(target, 0o550 if member.mode & stat.S_IXUSR else 0o440)  # noqa: S103
    for relative in ROOTS:
        target = root / relative
        if not (target.is_file() if relative.endswith(".json") else target.is_dir()):
            raise ValueError("incomplete approved extension archive")
    for directory, _, files in os.walk(root):
        os.chown(directory, 0, gid)
        os.chmod(directory, 0o550)  # noqa: S103
        for name in files:
            os.chown(Path(directory) / name, 0, gid)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gid", required=True, type=int)
    parser.add_argument("--clear", action="store_true")
    args = parser.parse_args()
    if os.geteuid() != 0 or args.gid <= 0:
        raise SystemExit("Extension staging requires root and a non-root agent GID")
    root = Path("/extensions")
    if args.clear:
        clear(root)
    else:
        receive(root, sys.stdin.buffer, args.gid)
    print("Approved extension volume cleared." if args.clear else "Approved extensions staged.")


if __name__ == "__main__":
    main()
