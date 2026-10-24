#!/usr/bin/env python3
"""Offline MCP interface contracts; discovery and tool calls belong to check_services."""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import UTC, date, datetime
from pathlib import Path

MAX_TOOLS = 2048
MAX_PAGES = 50
MAX_CATALOG_BYTES = 8 * 1024 * 1024
MAX_TOOL_BYTES = 256 * 1024
MAX_DEPTH = 40
MAX_NODES = 100000
NAME = re.compile(r"[A-Za-z0-9_.-]{1,128}\Z")
DIGEST = re.compile(r"[0-9a-f]{64}\Z")
FINGERPRINT_KEYS = {"input_schema_sha256", "output_schema_sha256", "metadata_sha256"}


class ContractError(ValueError):
    """Invalid local contract; exception text never contains server payloads."""


class CatalogError(ValueError):
    """An incomplete, malformed or oversized catalog cannot prove compatibility."""


def valid_name(value):
    return isinstance(value, str) and NAME.fullmatch(value) is not None


def canonical(value, maximum=MAX_CATALOG_BYTES):
    """Bound already-decoded SDK data before encoding; never follow schema references."""
    pending = [(value, 0)]
    nodes = size = 0
    while pending:
        item, depth = pending.pop()
        nodes += 1
        if depth > MAX_DEPTH or nodes > MAX_NODES:
            raise CatalogError("catalog nesting or node limit exceeded")
        if isinstance(item, dict):
            if len(item) > MAX_NODES:
                raise CatalogError("catalog node limit exceeded")
            size += 2 + len(item) * 4
            for key, child in item.items():
                if not isinstance(key, str):
                    raise CatalogError("catalog object keys must be strings")
                if len(key) > maximum:
                    raise CatalogError("catalog size limit exceeded")
                size += len(key.encode("utf-8"))
                pending.append((child, depth + 1))
        elif isinstance(item, list):
            if len(item) > MAX_NODES:
                raise CatalogError("catalog node limit exceeded")
            size += 2 + len(item)
            pending.extend((child, depth + 1) for child in item)
        elif isinstance(item, str):
            # UTF-8 allocation is bounded by rejecting long strings first.
            if len(item) > maximum:
                raise CatalogError("catalog size limit exceeded")
            size += len(item.encode("utf-8")) + 2
        elif item is None or isinstance(item, (bool, int, float)):
            if isinstance(item, float) and not math.isfinite(item):
                raise CatalogError("non-JSON number in catalog")
            size += 24
        else:
            raise CatalogError("non-JSON value in catalog")
        if size > maximum or len(pending) > MAX_NODES:
            raise CatalogError("catalog size limit exceeded")
    try:
        encoded = json.dumps(value, sort_keys=True, ensure_ascii=False,
                             separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        raise CatalogError("invalid JSON catalog") from exc
    if len(encoded) > maximum:
        raise CatalogError("catalog size limit exceeded")
    return encoded


def fingerprint(value):
    return hashlib.sha256(canonical(value, MAX_TOOL_BYTES)).hexdigest()


def auth_profile(config):
    if config.get("bearerTokenEnvVar"):
        return "configured-bearer"
    if config.get("headers"):
        return "configured-headers"
    return "anonymous"


def selected_names(config, names):
    allow = config.get("enabledTools")
    return (set(names) if allow is None else set(allow) & set(names)) - set(
        config.get("disabledTools", [])
    )


class Catalog:
    """Incremental page accounting: failures never yield a partial snapshot."""

    def __init__(self):
        self.tools = {}
        self.bytes = 0
        self.cursors = set()
        self.pages = 0

    def add_page(self, tools, cursor=None):
        self.pages += 1
        if self.pages > MAX_PAGES:
            raise CatalogError("catalog pagination limit exceeded")
        if cursor is not None:
            if not isinstance(cursor, str) or len(cursor) > 4096 or cursor in self.cursors:
                raise CatalogError("invalid or repeated catalog cursor")
            self.cursors.add(cursor)
        for tool in tools:
            document = tool if isinstance(tool, dict) else tool.model_dump(
                mode="json", by_alias=True, exclude_none=True
            )
            encoded = canonical(document, MAX_TOOL_BYTES)
            self.bytes += len(encoded)
            if self.bytes > MAX_CATALOG_BYTES or len(self.tools) >= MAX_TOOLS:
                raise CatalogError("catalog size limit exceeded")
            name = document.get("name")
            if not valid_name(name) or name in self.tools:
                raise CatalogError("invalid or duplicate catalog tool name")
            if not isinstance(document.get("inputSchema"), dict):
                raise CatalogError("tool input schema is not an object")
            output = document.get("outputSchema")
            if output is not None and not isinstance(output, dict):
                raise CatalogError("tool output schema is not an object")
            self.tools[name] = {
                "input_schema_sha256": fingerprint(document["inputSchema"]),
                "output_schema_sha256": fingerprint(output) if output is not None else None,
                "metadata_sha256": fingerprint({key: document.get(key) for key in (
                    "title", "description", "annotations", "execution"
                )}),
            }

    def snapshot(self, config):
        selected = selected_names(config, self.tools)
        return {
            "auth_profile": auth_profile(config),
            "catalog_names": sorted(self.tools),
            "tools": {name: self.tools[name] for name in sorted(selected)},
        }


def load_manifest(path, servers):
    """Missing manifests are observable as unbaselined; malformed ones fail closed."""
    if path.is_symlink():
        raise ContractError("invalid MCP contract file")
    if not path.exists():
        return {}
    if not path.is_file() or path.stat().st_size > MAX_CATALOG_BYTES:
        raise ContractError("invalid MCP contract file")

    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ContractError("duplicate MCP contract field")
            result[key] = value
        return result

    try:
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique_object)
        if (set(value) != {"schema_version", "servers"}
                or type(value["schema_version"]) is not int or value["schema_version"] != 1):
            raise ValueError
        entries = value["servers"]
        if not isinstance(entries, dict) or len(entries) > MAX_TOOLS:
            raise ValueError
        for name, contract in entries.items():
            if not valid_name(name) or name not in servers:
                raise ValueError
            if set(contract) != {"reviewed_at", "auth_profile", "catalog_names", "tools"}:
                raise ValueError
            date.fromisoformat(contract["reviewed_at"])
            if contract["auth_profile"] != auth_profile(servers[name]):
                raise ValueError
            names = contract["catalog_names"]
            if not isinstance(names, list) or len(names) > MAX_TOOLS:
                raise ValueError
            if not all(valid_name(tool) for tool in names) or len(set(names)) != len(names):
                raise ValueError
            tools = contract["tools"]
            if not isinstance(tools, dict) or not set(tools) <= set(names):
                raise ValueError
            for hashes in tools.values():
                if set(hashes) != FINGERPRINT_KEYS:
                    raise ValueError
                for key, digest in hashes.items():
                    if key == "output_schema_sha256" and digest is None:
                        continue
                    if not isinstance(digest, str) or not DIGEST.fullmatch(digest):
                        raise ValueError
            expected = selected_names(servers[name], names)
            allow = servers[name].get("enabledTools", [])
            if not set(allow) <= set(names) or set(tools) != expected:
                raise ValueError
        canonical(value)
        return entries
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError) as exc:
        raise ContractError("invalid MCP contract or configuration coverage") from exc


def compare(config, contract, observed):
    """Return redacted findings; a catalog never grants or updates permissions."""
    names = set(observed["catalog_names"])
    missing = sorted(set(config.get("enabledTools", [])) - names)
    detail = {"verdict": "unverified", "observed": observed,
              "observed_at": datetime.now(UTC).isoformat(timespec="seconds")}
    if contract is not None:
        detail["contract_sha256"] = hashlib.sha256(canonical(contract)).hexdigest()
        detail["added"] = sorted(names - set(contract["catalog_names"]))
        detail["removed"] = sorted(set(contract["catalog_names"]) - names)
    if missing:
        detail.update(verdict="incompatible", missing=missing)
        return {"status": "FAIL", "detail": "configured tools missing: " + ", ".join(missing[:20])
                + ("; added: " + ", ".join(detail["added"][:20]) if detail.get("added") else ""),
                "compatibility": detail}
    if contract is None:
        hosted = "command" not in config
        detail["verdict"] = "unbaselined" if hosted else "not_enrolled"
        return {"status": "SETUP" if hosted else "PASS",
                "detail": "no reviewed hosted MCP contract" if hosted else "local MCP discovery",
                "compatibility": detail}
    if contract["auth_profile"] != observed["auth_profile"]:
        detail["verdict"] = "unverified"
        return {"status": "SETUP", "detail": "MCP authentication profile needs review",
                "compatibility": detail}
    selected = set(observed["tools"])
    schemas, metadata = [], []
    for name in sorted(selected & set(contract["tools"])):
        expected, actual = contract["tools"][name], observed["tools"][name]
        if any(expected[key] != actual[key] for key in (
            "input_schema_sha256", "output_schema_sha256"
        )):
            schemas.append(name)
        if expected["metadata_sha256"] != actual["metadata_sha256"]:
            metadata.append(name)
    changed_selection = selected != set(contract["tools"])
    detail.update(schema_changed=schemas, metadata_changed=metadata)
    if schemas or changed_selection:
        detail["verdict"] = "review"
        return {"status": "FAIL", "detail": "MCP contract review required: " + (
            "schema changed for " + ", ".join(schemas[:20])
            if schemas else "exposed tool set changed"
        ), "compatibility": detail}
    if metadata:
        detail["verdict"] = "review"
        return {"status": "SETUP", "detail": "MCP metadata review required: "
                + ", ".join(metadata[:20]),
                "compatibility": detail}
    detail["verdict"] = "matched"
    notice = ""
    if detail["added"] or detail["removed"]:
        notice = "; unselected catalog changes: +{} -{}".format(
            len(detail["added"]), len(detail["removed"])
        )
    return {"status": "PASS", "detail": "reviewed interface matches" + notice,
            "compatibility": detail}


def validate_sources(root: Path):
    """Offline CI validation, including modules not selected by the current operator."""
    directories = [root / "runtime", *sorted((root / "modules").glob("*/runtime"))]
    for directory in directories:
        path = directory / "mcp.json"
        if not path.is_file():
            if (directory / "mcp-contracts.json").exists():
                raise ContractError("MCP contract has no owning configuration")
            continue
        servers = json.loads(path.read_text())["mcpServers"]
        contracts = load_manifest(directory / "mcp-contracts.json", servers)
        required = {name for name, config in servers.items()
                    if "command" not in config and config.get("enabled", True)
                    and config.get("auth") != "oauth" and name != "nvidia-cuda-docs"}
        if required - set(contracts):
            raise ContractError("enabled hosted MCP server lacks a reviewed contract")
