#!/usr/bin/env python3
"""Create harness workspace state without following workspace-controlled links."""

from __future__ import annotations

import argparse
import errno
import os
import stat
import subprocess
import tempfile
from pathlib import Path, PurePosixPath

DIRECTORIES = (".agent-state/logs",)
FILES = (
    ".agent-state/STATE.md",
    ".agent-state/DEBUG_LEDGER.md",
    ".agent-state/TENSOR_CONTRACTS.md",
    ".agent-state/UPSTREAM_SOURCES.md",
    ".agent-state/BENCHMARKS.jsonl",
)
STATE_TEMPLATE = """# Agent State

## Objective

## Current failure / work item

## Important requirements

## Last known-good state

## Minimal reproduction

## Current evidence

## Active hypothesis

## Eliminated hypotheses

## Important files

## Upstream references

## Commands/tests already run

## Next three experiments
1.
2.
3.
"""
EXCLUDES = (".agent-state/", ".playwright-cli/", ".serena/")
OPEN_DIR = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)


class UnsafeWorkspace(RuntimeError):
    pass


def components(relative: str) -> tuple[str, ...]:
    path = PurePosixPath(relative)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise UnsafeWorkspace(f"unsafe relative path: {relative!r}")
    return path.parts


def require_directory(fd: int, label: str, expected_uid: int) -> os.stat_result:
    info = os.fstat(fd)
    if not stat.S_ISDIR(info.st_mode):
        raise UnsafeWorkspace(f"{label} is not a directory")
    if info.st_uid != expected_uid:
        raise UnsafeWorkspace(f"{label} is owned by uid {info.st_uid}, expected {expected_uid}")
    return info


def open_directory(root_fd: int, relative: str, expected_uid: int, create: bool) -> int:
    current = os.dup(root_fd)
    try:
        for part in components(relative):
            try:
                child = os.open(part, OPEN_DIR, dir_fd=current)
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(part, mode=0o700, dir_fd=current)
                child = os.open(part, OPEN_DIR, dir_fd=current)
            except OSError as exc:
                if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                    raise UnsafeWorkspace(
                        f"workspace path is not a real directory: {relative}"
                    ) from exc
                raise
            os.close(current)
            current = child
            require_directory(current, relative, expected_uid)
        return current
    except BaseException:
        os.close(current)
        raise


def ensure_file(root_fd: int, relative: str, expected_uid: int) -> None:
    parts = components(relative)
    parent = open_directory(root_fd, "/".join(parts[:-1]), expected_uid, create=True)
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(parts[-1], flags, 0o600, dir_fd=parent)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != expected_uid or info.st_nlink != 1:
                raise UnsafeWorkspace(f"unsafe workspace file: {relative}")
            if relative == ".agent-state/STATE.md" and info.st_size == 0:
                os.write(fd, STATE_TEMPLATE.encode())
                os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        os.close(parent)


def update_git_exclude(root: Path, expected_uid: int) -> None:
    result = subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "rev-parse",
            "--path-format=absolute",
            "--git-path",
            "info/exclude",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode:
        return
    exclude = Path(result.stdout.strip())
    try:
        relative = exclude.resolve(strict=False).relative_to(root)
    except ValueError:
        print("warning: not modifying Git exclude outside the workspace", flush=True)
        return

    root_fd = os.open(root, OPEN_DIR)
    try:
        parent = open_directory(root_fd, str(relative.parent), expected_uid, create=True)
        try:
            existing = ""
            try:
                fd = os.open(
                    relative.name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent
                )
            except FileNotFoundError:
                pass
            else:
                try:
                    info = os.fstat(fd)
                    if (
                        not stat.S_ISREG(info.st_mode)
                        or info.st_uid != expected_uid
                        or info.st_nlink != 1
                    ):
                        raise UnsafeWorkspace("unsafe Git exclude file")
                    data = os.read(fd, 1024 * 1024 + 1)
                    if len(data) > 1024 * 1024:
                        raise UnsafeWorkspace("Git exclude file exceeds 1 MiB")
                    existing = data.decode("utf-8")
                finally:
                    os.close(fd)
            lines = existing.splitlines()
            for entry in EXCLUDES:
                if entry not in lines:
                    lines.append(entry)
            content = ("\n".join(lines).rstrip() + "\n").encode()
            temporary = f".exclude.{os.getpid()}.{next(tempfile._get_candidate_names())}"
            fd = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=parent,
            )
            try:
                os.write(fd, content)
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(temporary, relative.name, src_dir_fd=parent, dst_dir_fd=parent)
            os.fsync(parent)
        finally:
            os.close(parent)
    finally:
        os.close(root_fd)


def initialize(root: Path, directories=()) -> None:
    root.mkdir(parents=True, exist_ok=True)
    root = root.resolve(strict=True)
    if root == Path("/") or "\n" in str(root):
        raise UnsafeWorkspace(f"unsafe workspace root: {root}")
    expected_uid = os.getuid()
    root_fd = os.open(root, OPEN_DIR)
    try:
        require_directory(root_fd, str(root), expected_uid)
        for relative in (*DIRECTORIES, *directories):
            fd = open_directory(root_fd, relative, expected_uid, create=True)
            os.close(fd)
        for relative in FILES:
            ensure_file(root_fd, relative, expected_uid)
    finally:
        os.close(root_fd)
    update_git_exclude(root, expected_uid)
    print(f"Initialized agent state in {root}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("workspace", type=Path)
    args = parser.parse_args()
    try:
        initialize(args.workspace)
    except (OSError, UnicodeError, UnsafeWorkspace) as exc:
        raise SystemExit(f"workspace initialization refused: {exc}") from exc


if __name__ == "__main__":
    main()
