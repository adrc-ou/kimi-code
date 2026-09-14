#!/usr/bin/env python3
"""Create instance-scoped secrets and immutable Kimi runtime configuration."""

from __future__ import annotations

import argparse
import base64
import os
import secrets
import shlex
from pathlib import Path


def read_values(path: Path) -> dict[str, str]:
    values = {}
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def write_secret(path: Path, value: str) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.unlink(missing_ok=True)
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, value.encode())
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(temporary, path)


def ensure_secret(path: Path, generator) -> str:
    if path.is_file():
        value = path.read_text().strip()
        if len(value) >= 32:
            return value
    value = generator()
    path.unlink(missing_ok=True)
    write_secret(path, value)
    return value


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--runtime-dir", type=Path, required=True)
    parser.add_argument("--resolved-env", type=Path, required=True)
    args = parser.parse_args()
    args.runtime_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(args.runtime_dir, 0o700)
    values = read_values(args.resolved_env)
    api_key = values.get("LITELLM_API_KEY", "").strip()
    if not api_key:
        raise SystemExit("LITELLM_API_KEY must be set in .env")
    ephemeral = {
        "proxy-token": secrets.token_urlsafe(32),
        "search-token": secrets.token_urlsafe(32),
        "bridge-token": secrets.token_urlsafe(32),
        "nrp-api-key": api_key,
    }
    for name, value in ephemeral.items():
        path = args.runtime_dir / name
        path.unlink(missing_ok=True)
        write_secret(path, value)
    cache_salt = ensure_secret(
        args.runtime_dir / "cache-salt",
        lambda: base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("="),
    )
    template = (args.root / "runtime" / "config.toml").read_text()
    rendered = template.replace("__MODEL_PROXY_TOKEN__", ephemeral["proxy-token"])
    if "__MODEL_PROXY_TOKEN__" in rendered or rendered == template:
        raise SystemExit("runtime/config.toml is missing the proxy token placeholder")
    write_secret(args.runtime_dir / "kimi-config.toml", rendered)
    empty_system = args.runtime_dir / "SYSTEM.md"
    empty_system.unlink(missing_ok=True)
    write_secret(empty_system, "")
    for name in ("user-agents", "user-skills", "user-plugins"):
        path = args.runtime_dir / name
        path.mkdir(exist_ok=True)
        os.chmod(path, 0o555)  # noqa: S103 - intentionally immutable in the container
    runtime_env = {
        "SEARCH_ADAPTER_TOKEN": ephemeral["search-token"],
        "COMFYUI_TOKEN": ephemeral["bridge-token"],
        "NRP_API_KEY_FILE": str(args.runtime_dir / "nrp-api-key"),
        "NRP_INTERNAL_TOKEN_FILE": str(args.runtime_dir / "proxy-token"),
        "NRP_CACHE_SALT_FILE": str(args.runtime_dir / "cache-salt"),
        "KIMI_RENDERED_CONFIG": str(args.runtime_dir / "kimi-config.toml"),
        "KIMI_EMPTY_SYSTEM": str(empty_system),
        "KIMI_EMPTY_USER_AGENTS": str(args.runtime_dir / "user-agents"),
        "KIMI_EMPTY_USER_SKILLS": str(args.runtime_dir / "user-skills"),
        "KIMI_EMPTY_USER_PLUGINS": str(args.runtime_dir / "user-plugins"),
        "COMFYUI_BRIDGE_MAX_BODY": values.get("COMFYUI_BRIDGE_MAX_BODY", "536870912"),
        "COMFYUI_BRIDGE_MAX_WS_MESSAGE": values.get("COMFYUI_BRIDGE_MAX_WS_MESSAGE", "67108864"),
        "COMFYUI_BRIDGE_MAX_CONNECTIONS": values.get("COMFYUI_BRIDGE_MAX_CONNECTIONS", "16"),
    }
    content = "".join(f"{key}={shlex.quote(value)}\n" for key, value in runtime_env.items())
    path = args.runtime_dir / "runtime.env"
    path.unlink(missing_ok=True)
    write_secret(path, content)
    print(f"cache_salt_chars={len(cache_salt)}")


if __name__ == "__main__":
    main()
