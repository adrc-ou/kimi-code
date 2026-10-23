#!/usr/bin/env python3
"""Approve and prepare immutable project extension snapshots for Kimi."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import tarfile
import tempfile
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

if __package__:
    from . import extension_volume
    from .git_query import git_text
    from .safe_workspace_init import OPEN_DIR, UnsafeWorkspace, components, open_directory
else:
    import extension_volume
    from git_query import git_text
    from safe_workspace_init import OPEN_DIR, UnsafeWorkspace, components, open_directory

PRIVILEGED = extension_volume.ROOTS


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def require_real_ancestors(workspace: Path, relative: str) -> None:
    """Refuse a privileged path whose parent chain leaves the workspace.

    Stating the leaf is not enough: POSIX resolves every intermediate component, so a
    workspace-authored link at .kimi-code or .agents would be read and snapshotted as if
    it were project content.
    """
    try:
        parents = components(relative)[:-1]
    except UnsafeWorkspace as exc:
        raise ValueError(f"unsafe extension path: {relative}") from exc
    current = workspace
    for part in parents:
        current = current / part
        try:
            info = current.lstat()
        except OSError as exc:
            raise ValueError(f"unreadable extension ancestor: {relative}") from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise ValueError(f"extension ancestor is not a real directory: {relative}")


def inspect_path(workspace: Path, relative: str) -> dict[str, str]:
    path = workspace / relative
    if not path.exists() and not path.is_symlink():
        return {}
    require_real_ancestors(workspace, relative)
    entries: dict[str, str] = {}
    paths = [path] if path.is_file() or path.is_symlink() else [path, *sorted(path.rglob("*"))]
    seen_case: set[str] = set()
    for item in paths:
        rel = item.relative_to(workspace).as_posix()
        key = rel.casefold()
        if key in seen_case:
            raise ValueError(f"case-colliding extension path: {rel}")
        seen_case.add(key)
        info = item.lstat()
        if stat.S_ISLNK(info.st_mode) or (stat.S_ISREG(info.st_mode) and info.st_nlink != 1):
            raise ValueError(f"linked extension object is not allowed: {rel}")
        if stat.S_ISDIR(info.st_mode):
            entries[f"{rel}/"] = "directory"
        elif stat.S_ISREG(info.st_mode):
            # Docker may leave an empty file behind at a nested bind target.
            # It declares no MCP servers; prepare replaces it with valid empty JSON.
            if relative == ".kimi-code/mcp.json" and info.st_size == 0:
                continue
            entries[rel] = sha256(item)
        else:
            raise ValueError(f"special extension object is not allowed: {rel}")
    # An empty root directory, like an empty MCP file, may just be a mountpoint. Creating
    # that placeholder grants no executable content and must not invalidate an approval.
    if entries == {f"{relative}/": "directory"}:
        return {}
    return entries


def workspace_identity(workspace: Path) -> dict[str, str]:
    result = {"path": str(workspace)}
    if head := git_text(workspace, "rev-parse", "HEAD"):
        result["git_head"] = head
    if remote := git_text(workspace, "config", "--get", "remote.origin.url"):
        result["git_remote"] = remote
    return result


def scan(workspace: Path) -> dict[str, object]:
    workspace = workspace.resolve(strict=True)
    return {
        "workspace": workspace_identity(workspace),
        "extensions": {relative: inspect_path(workspace, relative) for relative in PRIVILEGED},
    }


def atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def approve(workspace: Path, manifest: Path) -> None:
    value = scan(workspace)
    value["approved_at"] = datetime.now(UTC).isoformat()
    atomic_json(manifest, value)
    print(json.dumps(value, indent=2, sort_keys=True))


def require_approval(workspace: Path, manifest: Path) -> dict[str, object]:
    current = scan(workspace)
    if not manifest.is_file():
        if any(
            digest != "directory"
            for entries in current["extensions"].values()
            for digest in entries.values()
        ):
            raise SystemExit(
                "privileged project extensions require approval; review with "
                "./extensions.sh list, approve with ./extensions.sh approve, then rerun ./start.sh"
            )
        return current
    approved = json.loads(manifest.read_text())
    if approved.get("workspace", {}).get("path") != current["workspace"]["path"]:
        raise SystemExit("extension approval belongs to a different workspace")
    entries = approved.get("extensions")
    if isinstance(entries, dict):
        # Read older approvals that recorded empty root mountpoints without rewriting them.
        entries = {relative: {} if value == {f"{relative}/": "directory"} else value
                   for relative, value in entries.items()}
    if entries != current["extensions"]:
        raise SystemExit(
            "privileged project extensions changed; stop services, review with "
            "./extensions.sh list, approve with ./extensions.sh approve, then rerun ./start.sh"
        )
    return current


def copy_snapshot(source: Path, target: Path, relative: str) -> tuple[Path, str]:
    origin = source / relative
    if origin.exists() or origin.is_symlink():
        require_real_ancestors(source, relative)
    destination = target / relative
    if origin.is_file() and not (relative == ".kimi-code/mcp.json" and origin.stat().st_size == 0):
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(origin, destination, follow_symlinks=False)
        os.chmod(destination, 0o600)
        return destination, "file"
    if relative.endswith(".json"):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text("{}\n")
        os.chmod(destination, 0o600)
        return destination, "file"
    destination.mkdir(parents=True, exist_ok=True)
    if origin.is_dir():
        for item in sorted(origin.rglob("*")):
            rel = item.relative_to(origin)
            copied = destination / rel
            if item.is_dir():
                copied.mkdir(mode=0o700)
            else:
                copied.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(item, copied, follow_symlinks=False)
                os.chmod(copied, 0o600)
    os.chmod(destination, 0o700)
    return destination, "directory"


def prepare_mountpoints(workspace: Path) -> None:
    """Reserve empty targets without rewriting project content or following links.

    Docker-created directories can have the wrong host owner, and runc can reject a missing
    nested file target on Docker Desktop. The empty placeholders carry no configuration; the
    approved content stays in the snapshot mounted over them.
    """
    root_fd = os.open(workspace, OPEN_DIR)
    try:
        for relative in PRIVILEGED:
            if not relative.endswith(".json"):
                target = open_directory(root_fd, relative, os.getuid(), create=True)
                os.close(target)
        parent = open_directory(root_fd, ".kimi-code", os.getuid(), create=True)
        try:
            try:
                descriptor = os.open("mcp.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL
                                     | getattr(os, "O_NOFOLLOW", 0), 0o600, dir_fd=parent)
            except FileExistsError:
                # Approval already inspected it; never truncate or rewrite an existing file.
                info = os.stat("mcp.json", dir_fd=parent, follow_symlinks=False)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise ValueError("MCP mountpoint is not a single-link regular file") from None
                return
            os.close(descriptor)
        finally:
            os.close(parent)
    finally:
        os.close(root_fd)


def prepare(workspace: Path, manifest: Path, state_dir: Path, output: Path) -> Path:
    approved = require_approval(workspace, manifest)
    snapshots = state_dir / "extension-snapshot"
    snapshots.mkdir(parents=True, mode=0o700, exist_ok=True)
    # Keep the reviewed bytes separate while verifying the copy. Docker receives an archive
    # over stdin; none of these host paths becomes a container mount source.
    snapshot_root = Path(tempfile.mkdtemp(prefix="snapshot-", dir=snapshots))
    mounts = []
    for relative in PRIVILEGED:
        source, kind = copy_snapshot(workspace, snapshot_root, relative)
        mounts.append((source, f"/workspace/{PurePosixPath(relative)}", kind))
    copied = {relative: inspect_path(snapshot_root, relative) for relative in PRIVILEGED}
    source_after_copy = {relative: inspect_path(workspace, relative) for relative in PRIVILEGED}
    expected = approved["extensions"]
    expected_copy = {}
    for relative in PRIVILEGED:
        if expected[relative]:
            expected_copy[relative] = expected[relative]
        elif relative.endswith(".json"):
            expected_copy[relative] = {relative: hashlib.sha256(b"{}\n").hexdigest()}
        else:
            expected_copy[relative] = {}
    if source_after_copy != expected or copied != expected_copy:
        raise SystemExit("extension content changed while its snapshot was created")
    prepare_mountpoints(workspace)
    archive_path = state_dir / "extension-snapshot.tar"
    descriptor = os.open(archive_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        os.fchmod(stream.fileno(), 0o600)
        with tarfile.open(fileobj=stream, mode="w") as archive:
            for relative in PRIVILEGED:
                archive.add(snapshot_root / relative, arcname=relative)
    volume = extension_volume.VOLUME
    fragment = {
        "services": {
            "kimi-agent": {"volumes": [
                {"type": "volume", "source": volume, "target": target, "read_only": True,
                 "volume": {"nocopy": True, "subpath": relative}}
                for relative, (_, target, _) in zip(PRIVILEGED, mounts, strict=True)
            ]},
            extension_volume.SERVICE: {
                "image": "adrc-kimi-agent:${HARNESS_IMAGE_SUFFIX:?Generated by start.sh}",
                "platform": "${HARNESS_DOCKER_PLATFORM:?Detected by the launcher}",
                "profiles": ["extension-staging"], "user": "0:0", "network_mode": "none",
                "read_only": True, "cap_drop": ["ALL"],
                "cap_add": ["CHOWN", "DAC_OVERRIDE", "FOWNER"],
                "security_opt": ["no-new-privileges:true"], "pids_limit": 64,
                "entrypoint": ["python3", "-c", Path(extension_volume.__file__).read_text()],
                "command": ["--gid", "${LOCAL_GID:-1000}"],
                "volumes": [{"type": "volume", "source": volume, "target": "/extensions",
                             "volume": {"nocopy": True}}],
            },
        },
        "volumes": {volume: {}},
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(".tmp")
    temporary.write_text(json.dumps(fragment, indent=2) + "\n")
    os.chmod(temporary, 0o600)
    os.replace(temporary, output)
    return snapshot_root


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("list", "approve", "revoke", "prepare"))
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.command == "list":
        print(json.dumps(scan(args.workspace), indent=2, sort_keys=True))
    elif args.command == "approve":
        approve(args.workspace, args.manifest)
    elif args.command == "revoke":
        args.manifest.unlink(missing_ok=True)
    else:
        if args.state_dir is None or args.output is None:
            parser.error("prepare requires --state-dir and --output")
        prepare(args.workspace, args.manifest, args.state_dir, args.output)


if __name__ == "__main__":
    main()
