#!/usr/bin/env python3
"""Approve and prepare immutable project extension snapshots for Kimi."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import subprocess
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

PRIVILEGED = (
    ".kimi-code/agents",
    ".agents/agents",
    ".kimi-code/skills",
    ".agents/skills",
    ".kimi-code/mcp.json",
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def inspect_path(workspace: Path, relative: str) -> dict[str, str]:
    path = workspace / relative
    if not path.exists() and not path.is_symlink():
        return {}
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
    return entries


def workspace_identity(workspace: Path) -> dict[str, str]:
    result = {"path": str(workspace)}
    command = subprocess.run(
        ["git", "-C", str(workspace), "rev-parse", "HEAD"], capture_output=True, text=True
    )
    if command.returncode == 0:
        result["git_head"] = command.stdout.strip()
    remote = subprocess.run(
        ["git", "-C", str(workspace), "config", "--get", "remote.origin.url"],
        capture_output=True,
        text=True,
    )
    if remote.returncode == 0:
        result["git_remote"] = remote.stdout.strip()
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
    if approved.get("extensions") != current["extensions"]:
        raise SystemExit(
            "privileged project extensions changed; stop services, review with "
            "./extensions.sh list, approve with ./extensions.sh approve, then rerun ./start.sh"
        )
    return current


def copy_snapshot(source: Path, target: Path, relative: str) -> tuple[Path, str]:
    origin = source / relative
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


def yaml_quote(value: str) -> str:
    return json.dumps(value)


def prepare(workspace: Path, manifest: Path, state_dir: Path, output: Path) -> None:
    approved = require_approval(workspace, manifest)
    snapshot_root = state_dir / "extension-snapshot.new"
    final_root = state_dir / "extension-snapshot"
    if snapshot_root.exists():
        shutil.rmtree(snapshot_root)
    snapshot_root.mkdir(parents=True, mode=0o700)
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
            expected_copy[relative] = {f"{relative}/": "directory"}
    if source_after_copy != expected or copied != expected_copy:
        raise SystemExit("extension content changed while its snapshot was created")
    if final_root.exists():
        shutil.rmtree(final_root)
    os.replace(snapshot_root, final_root)
    lines = ["services:", "  kimi-agent:", "    volumes:"]
    for source, target, _kind in mounts:
        adjusted = final_root / source.relative_to(snapshot_root)
        lines.extend(
            [
                "      - type: bind",
                f"        source: {yaml_quote(str(adjusted))}",
                f"        target: {yaml_quote(target)}",
                "        read_only: true",
                "        bind:",
                "          create_host_path: false",
            ]
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(".tmp")
    temporary.write_text("\n".join(lines) + "\n")
    os.chmod(temporary, 0o600)
    os.replace(temporary, output)


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
