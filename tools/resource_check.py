#!/usr/bin/env python3
"""Report host capacity and warn without blocking supported workloads."""

from __future__ import annotations

import argparse
import os
import shutil
from pathlib import Path


def gib(value: int) -> float:
    return value / 1024**3


def _threshold(name: str, default: int) -> int:
    """A GiB threshold from the environment, falling back to the documented default."""
    raw = os.environ.get(name, "")
    try:
        return int(raw) if raw.strip() else default
    except ValueError:
        print(f"warning: {name}={raw!r} is not a number, using {default}")
        return default


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("workspace", type=Path)
    args = parser.parse_args()
    # The workspace can be unstatable on a launch that is otherwise entirely supportable: it is
    # created by an earlier step, so a mount that has not landed yet, or a directory this user may
    # not stat, says nothing about the host. Say what is unknown instead of dying on it, because
    # the launcher treats a non-zero exit here as a reason to stop.
    unreadable = ""
    try:
        disk = shutil.disk_usage(args.workspace)
    except OSError as exc:
        disk = None
        unreadable = exc.strerror or type(exc).__name__
    memory = (
        os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") if hasattr(os, "sysconf") else 0
    )
    free = f"{gib(disk.free):.1f}GiB" if disk is not None else "unknown"
    print(f"Host capacity: free_disk={free} total_memory={gib(memory):.1f}GiB")
    # A typo in a threshold is a reason to use the default, not a reason to abort the launch:
    # this script's whole contract is that it warns and exits zero.
    disk_warning = _threshold("KIMI_WARN_FREE_DISK_GIB", 20)
    memory_warning = _threshold("KIMI_WARN_TOTAL_MEMORY_GIB", 8)
    if disk is None:
        # Named as its own verdict rather than folded into the low-space one: a threshold cannot
        # be compared against a measurement that does not exist, and "less than 20 GiB free"
        # would be a claim about a filesystem this process never read.
        print(f"warning: free disk for {args.workspace} is unknown: {unreadable}")
    elif disk.free < disk_warning * 1024**3:
        print(f"warning: workspace filesystem has less than {disk_warning} GiB free")
    if memory and memory < memory_warning * 1024**3:
        print(f"warning: host has less than {memory_warning} GiB total memory")


if __name__ == "__main__":
    main()
