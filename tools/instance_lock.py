#!/usr/bin/env python3

"""Ask whether a launcher lock is currently free, without taking it from anyone.

The launcher holds ``launcher.lock`` for the life of a launch (``tools/runtime.sh``), which makes
the lock the only trustworthy answer to "is this instance directory in use?". The ``pid=`` line
inside it is not: a process killed outright leaves its number behind, and after a reboot some
unrelated process owns it. An advisory lock, by contrast, is dropped by the kernel however its
holder died, so this probe takes it non-blockingly and reports what it found.

Exit 0 means free, and includes the case of no lock file at all, since a launch creates the file
before it acquires it. Anything this cannot settle - a held lock, a file it cannot open, a name
that is not a file - exits 1, so a caller treating non-zero as "leave it alone" fails closed.
"""

import argparse
import fcntl
import os
import sys
from pathlib import Path


def is_free(path: Path) -> bool:
    """Whether nothing holds the lock at `path`, judged by trying to hold it ourselves."""
    if not path.exists():
        return True
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    os.close(fd)
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("path", type=Path, help="path to a launcher.lock file")
    parser.add_argument(
        "--free",
        action="store_true",
        help="exit 0 if the lock is held by nobody, 1 otherwise",
    )
    args = parser.parse_args()
    if not args.free:
        parser.error("nothing to answer without --free")
    return 0 if is_free(args.path) else 1


if __name__ == "__main__":
    sys.exit(main())
