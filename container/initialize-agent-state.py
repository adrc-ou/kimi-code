#!/usr/bin/env python3
"""Repair private state-volume ownership without following stored symlinks."""

import os
import stat
import sys


def repair_directory(fd: int, uid: int, gid: int) -> None:
    os.fchown(fd, uid, gid)
    for name in os.listdir(fd):
        info = os.stat(name, dir_fd=fd, follow_symlinks=False)
        if stat.S_ISDIR(info.st_mode):
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            try:
                repair_directory(child, uid, gid)
            finally:
                os.close(child)
        else:
            os.chown(name, uid, gid, dir_fd=fd, follow_symlinks=False)


def main() -> None:
    if len(sys.argv) != 3 or not all(value.isdecimal() for value in sys.argv[1:]):
        raise SystemExit("usage: initialize-agent-state.py UID GID")
    uid, gid = map(int, sys.argv[1:])
    if uid == 0 or gid == 0:
        raise SystemExit("Agent state requires a non-root UID and GID")
    for path in ("/state/kimi", "/state/serena"):
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            repair_directory(fd, uid, gid)
            os.fchmod(fd, 0o700)
        finally:
            os.close(fd)
    print(f"Agent state volumes ready for {uid}:{gid}")


if __name__ == "__main__":
    main()
