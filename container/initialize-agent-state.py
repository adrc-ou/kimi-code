#!/usr/bin/env python3
"""Stage launcher-owned agent state and repair private volume ownership.

This runs once as root before the agent container starts. The agent home is a writable volume, so
the launcher-owned files inside it are root-owned *and* carry the ext4 immutable flag: unlinking an
entry in a directory you own is allowed whatever the entry's owner is, and the sticky bit does not
change that, so ownership alone cannot protect them. The agent cannot clear the flag because its
container drops every capability.
"""

from __future__ import annotations

import argparse
import array
import contextlib
import fcntl
import importlib.util
import os
import shutil
import stat
import time
from pathlib import Path

FS_IOC_GETFLAGS = 0x80086601
FS_IOC_SETFLAGS = 0x40086602
FS_IMMUTABLE_FL = 0x00000010

# Target name in the agent home -> file inside the read-only launcher stage directory.
MANAGED_FILES = {"AGENTS.md": "AGENTS.md", "SYSTEM.md": "SYSTEM.md", "mcp.json": "assets/mcp.json"}
SUPPRESSED_DIRECTORIES = ("agents", "skills", "plugins")

# Staged content is root-owned and only ever read by the agent, which reaches it through its
# primary group. Directories need the group execute bit to be traversable and helper tools need it
# to stay runnable; both make bandit-style linters report a "permissive mask" even though nothing
# here is writable by the agent.
STAGED_DIRECTORY_MODE = 0o550  # noqa: S103
STAGED_FILE_MODE = 0o440
STAGED_EXECUTABLE_MODE = 0o550  # noqa: S103


class StagingError(RuntimeError):
    """A staging step cannot be completed safely."""


def _read_flags(fd: int) -> int:
    buffer = array.array("l", [0])
    fcntl.ioctl(fd, FS_IOC_GETFLAGS, buffer, True)
    return buffer[0]


def _write_flags(fd: int, flags: int) -> None:
    fcntl.ioctl(fd, FS_IOC_SETFLAGS, array.array("l", [flags]), True)


def clear_immutable(path: Path) -> None:
    """Best-effort flag removal so a previous session's file can be replaced."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return
    try:
        with contextlib.suppress(OSError):
            flags = _read_flags(fd)
            if flags & FS_IMMUTABLE_FL:
                _write_flags(fd, flags & ~FS_IMMUTABLE_FL)
    finally:
        os.close(fd)


def require_immutable(path: Path) -> None:
    """Set the immutable flag and confirm the volume filesystem actually honoured it."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as exc:
        raise StagingError(f"cannot open {path.name} to flag it: {exc}") from exc
    try:
        _write_flags(fd, _read_flags(fd) | FS_IMMUTABLE_FL)
        if not _read_flags(fd) & FS_IMMUTABLE_FL:
            raise StagingError(f"{path.name} is not immutable after being flagged")
    except OSError as exc:
        raise StagingError(
            f"{path} needs a state volume on a filesystem supporting immutable flags: {exc}"
        ) from exc
    finally:
        os.close(fd)


def repair_tree(fd: int, uid: int, gid: int) -> None:
    """Hand agent state back to the agent identity without following stored symlinks."""
    os.fchown(fd, uid, gid)
    for name in os.listdir(fd):
        info = os.stat(name, dir_fd=fd, follow_symlinks=False)
        if stat.S_ISDIR(info.st_mode):
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            try:
                repair_tree(child, uid, gid)
            finally:
                os.close(child)
        else:
            # Links, sockets and fifos are only ever re-owned, never traversed.
            os.chown(name, uid, gid, dir_fd=fd, follow_symlinks=False)


def write_private(path: Path, data: bytes, *, uid: int, gid: int, mode: int) -> None:
    """Replace a path atomically with the given owner and mode."""
    clear_immutable(path)
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)
    temporary = path.with_name(f"{path.name}.staging")
    clear_immutable(temporary)
    with contextlib.suppress(FileNotFoundError):
        temporary.unlink()
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.chown(temporary, uid, gid)
    os.chmod(temporary, mode)
    os.replace(temporary, path)


def stage_managed_files(stage: Path, home: Path, gid: int) -> None:
    """Publish launcher-owned files as read-only content the agent can read by group."""
    for name, source in MANAGED_FILES.items():
        origin = stage / source
        if not origin.is_file():
            raise StagingError(f"missing staged source: {source}")
        write_private(home / name, origin.read_bytes(), uid=0, gid=gid, mode=STAGED_FILE_MODE)
        require_immutable(home / name)


def clear_contents(path: Path) -> None:
    """Empty a directory but keep the directory itself, which is a volume mount point."""
    for directory, names, files in os.walk(path, topdown=False):
        for entry in (*files, *names):
            clear_immutable(Path(directory) / entry)
    for entry in path.iterdir():
        if entry.is_symlink() or entry.is_file():
            entry.unlink()
        else:
            shutil.rmtree(entry)


def stage_assets_tree(stage: Path, target: Path, gid: int) -> int:
    """Copy the assembled runtime assets into their volume as read-only group-readable content."""
    source = stage / "assets"
    if not source.is_dir():
        raise StagingError("missing staged assets directory")
    for directory, names, files in os.walk(source):
        for entry in (*names, *files):
            if (Path(directory) / entry).is_symlink():
                raise StagingError(f"refusing symlinked runtime asset: {entry}")
    if not target.is_dir():
        raise StagingError("assets volume is not mounted")
    clear_contents(target)
    shutil.copytree(source, target, dirs_exist_ok=True)
    copied = 0
    for directory, names, files in os.walk(target):
        here = Path(directory)
        os.chown(here, 0, gid)
        os.chmod(here, STAGED_DIRECTORY_MODE)
        for entry in names:
            os.chown(here / entry, 0, gid)
            os.chmod(here / entry, STAGED_DIRECTORY_MODE)
        for entry in files:
            relative = here.relative_to(target) if here != target else Path()
            executable = (source / relative / entry).stat().st_mode & stat.S_IXUSR
            os.chown(here / entry, 0, gid)
            os.chmod(here / entry, STAGED_EXECUTABLE_MODE if executable else STAGED_FILE_MODE)
            copied += 1
    return copied


def stage_suppressions(managed: Path, gid: int) -> None:
    """Keep home-scoped agents, skills and plugins present, empty, and read-only."""
    for name in SUPPRESSED_DIRECTORIES:
        path = managed / name
        if not path.is_dir():
            raise StagingError(f"suppression volume is missing: {name}")
        clear_contents(path)
        os.chown(path, 0, gid)
        os.chmod(path, STAGED_DIRECTORY_MODE)


def load_merge_module(path: Path):
    spec = importlib.util.spec_from_file_location("kimi_config_merge", path)
    if spec is None or spec.loader is None:
        raise StagingError("config merge module is unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def merge_config(stage: Path, home: Path, merge_module: Path, uid: int, gid: int) -> str:
    """Rebuild the agent's writable config from the baseline plus its user-owned keys."""
    module = load_merge_module(merge_module)
    try:
        baseline = module.parse_config((stage / "kimi-config.toml").read_text())
        policy = module.load_policy(stage / "config-policy.json")
    except (OSError, module.ConfigMergeError, module.ConfigPolicyError) as exc:
        raise StagingError(f"launcher config baseline is unusable: {exc}") from exc
    target = home / "config.toml"
    current = None
    outcome = "baseline"
    if target.is_file():
        try:
            current = module.parse_config(target.read_text())
            outcome = "merged"
        except (OSError, module.ConfigMergeError):
            stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
            clear_immutable(target)
            target.replace(home / f"config.toml.unreadable-{stamp}")
    try:
        text = module.render_merged(baseline, policy, current)
    except module.ConfigMergeError as exc:
        raise StagingError(f"config merge failed: {exc}") from exc
    write_private(target, text.encode(), uid=uid, gid=gid, mode=0o600)
    return outcome


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--uid", type=int, required=True)
    parser.add_argument("--gid", type=int, required=True)
    parser.add_argument("--stage", type=Path, default=Path("/stage"))
    parser.add_argument("--kimi-home", type=Path, default=Path("/state/kimi"))
    parser.add_argument("--serena-home", type=Path, default=Path("/state/serena"))
    parser.add_argument("--assets", type=Path, default=Path("/state/assets"))
    parser.add_argument("--managed", type=Path, default=Path("/state/managed"))
    parser.add_argument("--merge-module", type=Path, default=Path("/stage/kimi_config_merge.py"))
    args = parser.parse_args()
    if args.uid == 0 or args.gid == 0:
        raise SystemExit("Agent state requires a non-root UID and GID")
    if not args.stage.is_dir():
        raise SystemExit("Launcher stage directory is not mounted")

    for name in (*MANAGED_FILES, "config.toml"):
        clear_immutable(args.kimi_home / name)
    for path in (args.kimi_home, args.serena_home):
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            repair_tree(fd, args.uid, args.gid)
            os.fchmod(fd, 0o700)
        finally:
            os.close(fd)
    stage_managed_files(args.stage, args.kimi_home, args.gid)
    assets = stage_assets_tree(args.stage, args.assets, args.gid)
    stage_suppressions(args.managed, args.gid)
    outcome = merge_config(args.stage, args.kimi_home, args.merge_module, args.uid, args.gid)
    print(
        f"Agent state ready for {args.uid}:{args.gid} "
        f"config={outcome} assets={assets} managed={len(MANAGED_FILES)}"
    )


if __name__ == "__main__":
    try:
        main()
    except StagingError as exc:
        raise SystemExit(f"Agent state staging failed: {exc}") from exc
