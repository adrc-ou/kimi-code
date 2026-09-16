#!/usr/bin/env python3
"""Rebuild the agent's config.toml from the launcher baseline plus user-owned settings.

Kimi stores every setting in one file and saves it by renaming a temporary copy over
``$KIMI_CODE_HOME/config.toml``. That file therefore has to be writable by the agent, which
would also let the agent rewrite harness policy. The merge below keeps the file writable while
making every policy key authoritative from the host template: keys named in the config policy
are mirrored from the stored file, and everything else is taken from the freshly rendered
baseline, so pinned edits and injected tables disappear at the next launch.
"""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import json
import re
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
BARE_KEY = re.compile(r"[A-Za-z0-9_-]+\Z")


class ConfigPolicyError(ValueError):
    """The pinned/user-owned policy document is missing or malformed."""


class ConfigMergeError(ValueError):
    """A config document cannot be represented faithfully; the caller must fall back."""


def load_policy(path: Path) -> frozenset[str]:
    """Return the top-level config keys the agent is allowed to own."""
    try:
        document = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise ConfigPolicyError(f"unreadable config policy {path}: {exc}") from exc
    if not isinstance(document, dict):
        raise ConfigPolicyError("config policy must be a JSON object")
    if document.get("schema_version") != SCHEMA_VERSION:
        raise ConfigPolicyError(f"config policy schema_version must be {SCHEMA_VERSION}")
    user_owned = document.get("user_owned")
    if not isinstance(user_owned, list) or not all(
        isinstance(key, str) and BARE_KEY.match(key) for key in user_owned
    ):
        raise ConfigPolicyError("config policy user_owned must list bare TOML keys")
    return frozenset(user_owned)


def parse_config(text: str) -> dict[str, Any]:
    """Parse a config document, reporting TOML errors as ConfigMergeError."""
    try:
        return tomllib.loads(text)
    except (tomllib.TOMLDecodeError, ValueError) as exc:
        raise ConfigMergeError(f"invalid TOML: {exc}") from exc


def merge(
    baseline: Mapping[str, Any],
    user_owned: frozenset[str],
    current: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Overlay the user-owned keys found in ``current`` on a copy of ``baseline``.

    Only keys present in ``current`` are honoured. Removing a section from the stored file does
    not delete it from the baseline, because a Kimi rewrite that drops a key it does not model
    must never cost the deployment its configured value.
    """
    merged = copy.deepcopy(dict(baseline))
    if current is None:
        return merged
    for key in user_owned:
        if key in current:
            merged[key] = copy.deepcopy(current[key])
    return merged


def _format_key(key: str) -> str:
    if not key:
        raise ConfigMergeError("empty TOML key")
    return key if BARE_KEY.match(key) else json.dumps(key, ensure_ascii=False)


def _format_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise ConfigMergeError("TOML cannot represent NaN or infinity here")
        return repr(value)
    if isinstance(value, dt.datetime | dt.date | dt.time):
        return value.isoformat()
    if isinstance(value, list):
        if any(isinstance(item, Mapping) for item in value):
            raise ConfigMergeError("arrays of tables are not supported by this emitter")
        return "[" + ", ".join(_format_value(item) for item in value) + "]"
    raise ConfigMergeError(f"unsupported TOML value type: {type(value).__name__}")


def _split(table: Mapping[str, Any]) -> tuple[list[str], list[str]]:
    scalars: list[str] = []
    children: list[str] = []
    for key, value in table.items():
        if isinstance(value, Mapping):
            children.append(key)
        else:
            scalars.append(key)
    return scalars, children


def _emit_table(path: tuple[str, ...], table: Mapping[str, Any], out: list[str]) -> None:
    scalars, children = _split(table)
    # A table that only groups children needs no header of its own.
    if path and (scalars or not children):
        out.append(f"[{'.'.join(_format_key(part) for part in path)}]")
    for key in scalars:
        out.append(f"{_format_key(key)} = {_format_value(table[key])}")
    if scalars or not children:
        out.append("")
    for key in children:
        _emit_table((*path, key), table[key], out)


def emit(document: Mapping[str, Any]) -> str:
    """Render a parsed config back to TOML that ``tomllib`` re-reads identically."""
    lines: list[str] = []
    _emit_table((), document, lines)
    return "\n".join(lines).rstrip("\n") + "\n"


def render_merged(
    baseline: Mapping[str, Any],
    user_owned: frozenset[str],
    current: Mapping[str, Any] | None,
) -> str:
    """Merge parsed documents and emit TOML, verifying the result round-trips."""
    merged = merge(baseline, user_owned, current)
    text = emit(merged)
    if parse_config(text) != merged:
        raise ConfigMergeError("emitted config does not round-trip")
    return text


def merge_text(baseline_text: str, user_owned: frozenset[str], current_text: str | None) -> str:
    """Merge two config documents supplied as text."""
    current = parse_config(current_text) if current_text is not None else None
    return render_merged(parse_config(baseline_text), user_owned, current)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--current", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    current_text = args.current.read_text() if args.current and args.current.is_file() else None
    text = merge_text(
        args.baseline.read_text(),
        load_policy(args.policy),
        current_text,
    )
    temporary = args.output.with_name(f"{args.output.name}.tmp")
    temporary.write_text(text)
    temporary.replace(args.output)
    print(f"merged_config_bytes={len(text.encode())}")


if __name__ == "__main__":
    main()
