#!/usr/bin/env python3
"""Record and recheck bind sources immediately before Compose starts."""

from __future__ import annotations

import argparse
import json
import os
import stat
from pathlib import Path

KINDS = ("models", "custom_nodes", "input", "output", "temp", "user")


def ensure_real_directory(path: Path) -> Path:
    if not path.is_absolute():
        raise ValueError(f"bind source must be absolute: {path}")
    if any(character in str(path) for character in ("\n", "\r", "\x00")):
        raise ValueError("bind source contains a forbidden control character")
    current = Path(path.anchor)
    for component in path.parts[1:]:
        current = current / component
        info = current.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise ValueError(f"bind source component is not a real directory: {current}")
    resolved = path.resolve(strict=True)
    if resolved.stat().st_uid != os.getuid():
        raise ValueError(f"bind source is not owned by uid {os.getuid()}: {resolved}")
    return resolved


def sources(workspace: Path) -> dict[str, Path]:
    result = {}
    for kind in KINDS:
        variable = f"COMFYUI_{kind.upper()}_PATH"
        configured = os.environ.get(variable, "").strip()
        result[kind] = Path(configured) if configured else workspace / "comfyui" / kind
    return result


def snapshot(workspace: Path) -> dict[str, dict[str, int | str]]:
    workspace = ensure_real_directory(workspace)
    result = {}
    for name, path in sources(workspace).items():
        path = ensure_real_directory(path)
        info = path.stat()
        result[name] = {
            "path": str(path),
            "device": info.st_dev,
            "inode": info.st_ino,
            "uid": info.st_uid,
        }
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("record", "verify", "emit"))
    parser.add_argument("workspace", type=Path)
    parser.add_argument("manifest", type=Path)
    args = parser.parse_args()
    if args.mode == "emit":
        current = json.loads(args.manifest.read_text())
        for name, item in current.items():
            print(f"COMFYUI_{name.upper()}_PATH={item['path']}")
        return
    current = snapshot(args.workspace)
    if args.mode == "record":
        args.manifest.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.manifest.with_suffix(".tmp")
        temporary.write_text(json.dumps(current, indent=2, sort_keys=True) + "\n")
        os.chmod(temporary, 0o600)
        os.replace(temporary, args.manifest)
        for name, item in current.items():
            print(f"COMFYUI_{name.upper()}_PATH={item['path']}")
        return
    expected = json.loads(args.manifest.read_text())
    if current != expected:
        raise SystemExit("bind source changed after initialization")


if __name__ == "__main__":
    main()
