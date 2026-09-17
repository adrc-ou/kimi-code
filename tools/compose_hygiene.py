#!/usr/bin/env python3
"""Assert a resolved Compose configuration still confines the agent.

Reads `docker compose config --format json` on stdin. These are the properties the harness
claims about the agent container: its writable world is the workspace, no host path other than
the workspace and the approved-extension snapshot appears in its mount table, nothing it can
see names a provider credential, every published port is loopback, and every container keeps a
read-only root filesystem. Compose overlays accumulate long after a reviewer looked, so each
check fails closed: an unrecognised shape is a failure rather than a skip, which turns a
Compose schema change into a loud error instead of a silent pass.

`tools/runtime.sh` runs this over the fully resolved launch configuration, so every module
overlay and approved-extension bind is checked on the way up and not only in CI.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

AGENT = "kimi-agent"
PROXY = "model-proxy"
REQUIRED_VOLUMES = {"kimi-state", "serena-state", "kimi-assets"}
SNAPSHOT_DIRECTORY = "extension-snapshot"
# A bind source is readable from /proc/self/mountinfo inside the container that owns it, so
# these are the only two host locations a kimi-agent bind may ever name.
SNAPSHOT_TARGETS = ("/workspace/.kimi-code/", "/workspace/.agents/")
CREDENTIAL_DIRECTORY = "credentials"
LOOPBACK = "127.0.0.1"


def under(path: str, parent: str) -> bool:
    return path == parent or path.startswith(parent.rstrip("/") + "/")


def entries(owner: str, service: dict[str, Any], key: str) -> list[Any]:
    value = service.get(key) or []
    if not isinstance(value, list):
        raise SystemExit(f"{owner} has an unrecognised {key} section")
    return value


def check_read_only_roots(services: dict[str, dict[str, Any]]) -> None:
    for name, service in sorted(services.items()):
        if service.get("read_only") is not True:
            raise SystemExit(f"{name} lost its read-only root filesystem")


def check_credentials(
    services: dict[str, dict[str, Any]], runtime_dir: str, expect_secret: str | None
) -> None:
    credential_root = os.path.join(runtime_dir, CREDENTIAL_DIRECTORY)
    held_by_proxy: set[str] = set()
    for name, service in sorted(services.items()):
        for secret in entries(name, service, "secrets"):
            if not isinstance(secret, dict) or "source" not in secret:
                raise SystemExit(f"{name} has an unrecognised secret entry")
            if name != PROXY:
                raise SystemExit(
                    f"{name} mounts secret {secret['source']}; only {PROXY} may hold secrets"
                )
            held_by_proxy.add(str(secret["source"]))
        for mount in entries(name, service, "volumes"):
            if not isinstance(mount, dict):
                raise SystemExit(f"{name} has an unrecognised mount entry")
            source = str(mount.get("source", ""))
            if mount.get("type") == "bind" and under(os.path.realpath(source), credential_root):
                raise SystemExit(f"{name} bind-mounts a credential path: {source}")
    if expect_secret is not None and expect_secret not in held_by_proxy:
        raise SystemExit(f"provider credential {expect_secret} did not reach {PROXY} alone")


def check_agent_mounts(
    services: dict[str, dict[str, Any]], workspace: str, runtime_dir: str
) -> None:
    agent = services.get(AGENT)
    if agent is None:
        raise SystemExit("the configuration has no kimi-agent service")
    snapshot_root = os.path.join(runtime_dir, SNAPSHOT_DIRECTORY)
    volumes: set[str] = set()
    for mount in entries(AGENT, agent, "volumes"):
        kind = mount.get("type") if isinstance(mount, dict) else None
        if kind == "volume":
            volumes.add(str(mount.get("source")))
            continue
        if kind != "bind":
            raise SystemExit("kimi-agent has an unrecognised or unexpected mount entry")
        assert isinstance(mount, dict)
        source = os.path.realpath(str(mount.get("source", "")))
        target = str(mount.get("target", ""))
        if (mount.get("bind") or {}).get("create_host_path") is not False:
            raise SystemExit(f"kimi-agent bind may create a host path: {source}")
        if source == workspace:
            # The workspace is the agent's writable world by design, and it is the only one.
            continue
        if mount.get("read_only") is not True:
            raise SystemExit(f"kimi-agent bind is not read-only: {source}")
        if under(source, snapshot_root) and target.startswith(SNAPSHOT_TARGETS):
            continue
        raise SystemExit(f"kimi-agent exposes host bind {source} at {target}")
    missing = REQUIRED_VOLUMES - volumes
    if missing:
        raise SystemExit(f"kimi-agent lost its state volumes: {sorted(missing)}")


def check_loopback_ports(services: dict[str, dict[str, Any]]) -> None:
    for name, service in sorted(services.items()):
        for entry in entries(name, service, "ports"):
            if not isinstance(entry, dict):
                raise SystemExit(f"{name} has an unrecognised port entry")
            host_ip = entry.get("host_ip", entry.get("HostIp"))
            if host_ip != LOOPBACK:
                published = entry.get("published", entry.get("target"))
                raise SystemExit(
                    f"{name} publishes {published} on {host_ip or 'all interfaces'}; "
                    "every port must be bound to 127.0.0.1"
                )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--runtime-dir", type=Path, required=True)
    parser.add_argument("--expect-secret", help="secret name that must reach model-proxy alone")
    parser.add_argument("--label", default="configuration")
    args = parser.parse_args()
    configuration = json.load(sys.stdin)
    services = configuration.get("services")
    if not isinstance(services, dict) or not services:
        raise SystemExit("the resolved configuration has no services section")
    for service in services.values():
        if not isinstance(service, dict):
            raise SystemExit("the resolved configuration has a malformed service")
    runtime_dir = os.path.realpath(args.runtime_dir)
    check_read_only_roots(services)
    check_credentials(services, runtime_dir, args.expect_secret)
    check_agent_mounts(services, os.path.realpath(args.workspace), runtime_dir)
    check_loopback_ports(services)
    print(f"{args.label}: agent mounts, credentials, ports and root filesystems are confined")


if __name__ == "__main__":
    main()
