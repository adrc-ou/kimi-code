#!/usr/bin/env python3

"""Read one key from a dotenv-style file without evaluating shell code."""

import argparse
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("path", type=Path)
    parser.add_argument("key")
    args = parser.parse_args()

    for raw_line in args.path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip() != args.key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        print(value)
        return
    raise SystemExit(f"{args.key} is not set in {args.path}")


if __name__ == "__main__":
    main()
