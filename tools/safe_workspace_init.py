#!/usr/bin/env python3
"""Prepare a project workspace for a session without following workspace-controlled links.

Nothing harness-owned is created in the project any more. The agent's scratch memory used to live
in ``.agent-state/`` inside the workspace, where it outlived the session that wrote it and cluttered
a tree that belongs to somebody else; it is now ``/tmp/agent-state`` in the agent container, staged
by ``tools/register_workspace.py`` and destroyed with the container. What this module still does is
the two things that genuinely belong to a launch - create the directories a selected module
declares, and keep harness droppings out of the project's ``git status`` - plus retire the scratch
directory from workspaces an older revision left it in.
"""

from __future__ import annotations

import argparse
import errno
import os
import secrets
import shutil
import stat
from pathlib import Path, PurePosixPath

if __package__:
    from .git_query import git_text
else:
    from git_query import git_text

#: The scratch directory older revisions created inside the project. Nothing writes it any more,
#: and this is the only place its name still appears in code that runs.
RETIRED_DIRECTORY = ".agent-state"
#: Harness droppings that a project's own `git status` should never have to look at.
EXCLUDES = (".playwright-cli/", ".serena/")
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


def tracked_by_git(root: Path, relative: str) -> bool | None:
    """Whether the project owns this path: yes, no, or none of our business (not a repository).

    The answer decides whether deleting is safe, so the failure modes are not symmetric. Git
    refusing the question returns None and retires nothing; only an empty successful listing is
    evidence the directory is ours.
    """
    output = git_text(root, "ls-files", "--", relative)
    if output is None:
        return None
    return bool(output.splitlines())


def retire_state(root: Path) -> bool:
    """Delete the workspace's scratch directory if this harness, and only this harness, made it.

    Three things must hold: it is a real directory owned by us rather than a link we would follow,
    the repository tracks nothing under that name, and the name is the one this harness used. A
    project that keeps its own directory there - tracked, and therefore somebody's work - is left
    exactly as found, and so is any workspace Git cannot speak about.
    """
    target = root / RETIRED_DIRECTORY
    if not target.exists() and not target.is_symlink():
        return False
    if tracked_by_git(root, RETIRED_DIRECTORY) is not False:
        return False
    expected_uid = os.getuid()
    root_fd = os.open(root, OPEN_DIR)
    try:
        require_directory(root_fd, str(root), expected_uid)
        try:
            fd = open_directory(root_fd, RETIRED_DIRECTORY, expected_uid, create=False)
        except (FileNotFoundError, NotADirectoryError, UnsafeWorkspace):
            # A link, a file, or a directory owned by another account: not ours to remove.
            return False
        os.close(fd)
    finally:
        os.close(root_fd)
    # rmtree never follows a symlink it finds on the way down, so nothing inside can redirect the
    # deletion at a path outside the workspace.
    shutil.rmtree(target)
    return True


def update_git_exclude(root: Path, expected_uid: int) -> None:
    output = git_text(root, "rev-parse", "--path-format=absolute", "--git-path", "info/exclude")
    if not output:
        return
    exclude = Path(output)
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
            temporary = f".exclude.{os.getpid()}.{secrets.token_hex(8)}"
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


def initialize(root: Path, directories=(), retire: bool = True) -> None:
    root.mkdir(parents=True, exist_ok=True)
    root = root.resolve(strict=True)
    if root == Path("/") or "\n" in str(root):
        raise UnsafeWorkspace(f"unsafe workspace root: {root}")
    expected_uid = os.getuid()
    if retire:
        retire_state(root)
    root_fd = os.open(root, OPEN_DIR)
    try:
        require_directory(root_fd, str(root), expected_uid)
        for relative in directories:
            fd = open_directory(root_fd, relative, expected_uid, create=True)
            os.close(fd)
    finally:
        os.close(root_fd)
    update_git_exclude(root, expected_uid)
    print(f"Workspace ready: {root}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("workspace", type=Path)
    parser.add_argument(
        "--retire-only",
        action="store_true",
        help="remove the retired scratch directory and create nothing (the exit trap's question)",
    )
    args = parser.parse_args()
    try:
        if args.retire_only:
            retire_state(args.workspace)
            return
        initialize(args.workspace)
    except (OSError, UnicodeError, UnsafeWorkspace) as exc:
        raise SystemExit(f"workspace initialization refused: {exc}") from exc


if __name__ == "__main__":
    main()
