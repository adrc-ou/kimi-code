#!/usr/bin/env python3

"""Read one key from Compose-resolved or harness-generated KEY=VALUE output.

Parsing lives in :func:`tools.env_values.read_env_values` and nowhere else: the same file holds the
provider credentials, and a launcher step that re-implements the parser is a second parser that will
drift from the first. This script stays a thin CLI over it -- ask for one name, print its value, and
exit non-zero when the file says nothing about it, because every caller runs inside ``$( )``.
"""

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.env_values import read_env_values  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("path", type=Path)
    parser.add_argument("key")
    args = parser.parse_args()

    values = read_env_values(args.path)
    if args.key not in values:
        raise SystemExit(f"{args.key} is not set in {args.path}")
    print(values[args.key])


if __name__ == "__main__":
    main()
