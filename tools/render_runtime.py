#!/usr/bin/env python3
"""Create instance-scoped secrets and the immutable Kimi runtime configuration.

The rendered ``kimi-config.toml`` is a splice of two sources: the static behaviour baseline in
``runtime/config.toml`` and the model/provider tables generated from the resolved model plan.
Neither half can express the other, which is the point - a model window, a concurrency ceiling,
and a fair-use percentage exist exactly once, in ``./models`` and ``./providers``.

Every file written here is mode-0600 and lives under the instance runtime directory. Provider
API keys are materialised as per-credential files that only the proxy container receives; the
values themselves never enter ``.env``, any environment file the agent can read, or the
rendered configuration.
"""

from __future__ import annotations

import argparse
import base64
import os
import secrets
import shlex
from pathlib import Path
from typing import Any

if __package__:
    from .env_values import read_env_values
    from .model_config import render as render_model_tables
    from .models import POLICY_FILE, load_plan, materialise_credentials
else:
    from env_values import read_env_values
    from model_config import render as render_model_tables

    from models import POLICY_FILE, load_plan, materialise_credentials

#: Marker comment in runtime/config.toml that the generated tables replace.
MODEL_MARKER = "#__KIMI_MODEL_CONFIG__"
ALIAS_MARKER = "__KIMI_PRIMARY_ALIAS__"
PLACEHOLDER_MARKER = "__MODEL_PROXY_TOKEN__"


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


def kimi_config(root: Path, plan: dict[str, Any], token: str) -> str:
    """Splice the generated model tables into the static behaviour baseline."""
    template = (root / "runtime" / "config.toml").read_text(encoding="utf-8")
    for marker in (MODEL_MARKER, ALIAS_MARKER):
        if marker not in template:
            raise SystemExit(f"runtime/config.toml is missing the {marker} marker")
    # The proxy-token marker is contributed by the generated providers table, not by the
    # baseline: a baseline that named a token would be naming a specific provider, and the
    # baseline is provider-agnostic by design.
    generated = render_model_tables(plan, PLACEHOLDER_MARKER)
    if PLACEHOLDER_MARKER not in generated:
        raise SystemExit("generated model configuration lost the proxy token marker")
    rendered = template.replace(ALIAS_MARKER, plan["lanes"]["primary"]["alias"])
    rendered = rendered.replace(MODEL_MARKER, generated)
    rendered = rendered.replace(PLACEHOLDER_MARKER, token)
    for marker in (MODEL_MARKER, ALIAS_MARKER, PLACEHOLDER_MARKER):
        if marker in rendered:
            raise SystemExit(f"rendered Kimi config still contains {marker}")
    return rendered


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--runtime-dir", type=Path, required=True)
    parser.add_argument("--resolved-env", type=Path, required=True)
    args = parser.parse_args()
    args.runtime_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(args.runtime_dir, 0o700)
    values = read_env_values(args.resolved_env)
    plan = load_plan(args.runtime_dir)
    materialise_credentials(plan, args.runtime_dir, values)
    ephemeral = {
        "proxy-token": secrets.token_urlsafe(32),
        "search-token": secrets.token_urlsafe(32),
    }
    for name, value in ephemeral.items():
        path = args.runtime_dir / name
        path.unlink(missing_ok=True)
        write_secret(path, value)
    cache_salt = ensure_secret(
        args.runtime_dir / "cache-salt",
        lambda: base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("="),
    )
    rendered = kimi_config(args.root, plan, ephemeral["proxy-token"])
    write_secret(args.runtime_dir / "kimi-config.toml", rendered)
    # Deliberately empty: the initializer stages this as an immutable root-owned SYSTEM.md, which
    # denies the agent a writable system-prompt file without Kimi needing one from the harness.
    system_markdown = args.runtime_dir / "SYSTEM.md"
    system_markdown.unlink(missing_ok=True)
    write_secret(system_markdown, "")
    runtime_env = {
        "SEARCH_ADAPTER_TOKEN": ephemeral["search-token"],
        "MODEL_PROXY_POLICY_FILE": str(args.runtime_dir / POLICY_FILE),
        "MODEL_PROXY_INTERNAL_TOKEN_FILE": str(args.runtime_dir / "proxy-token"),
        "MODEL_PROXY_CACHE_SALT_FILE": str(args.runtime_dir / "cache-salt"),
        "KIMI_RENDERED_CONFIG": str(args.runtime_dir / "kimi-config.toml"),
        "KIMI_SYSTEM_MD": str(system_markdown),
    }
    content = "".join(f"{key}={shlex.quote(value)}\n" for key, value in runtime_env.items())
    path = args.runtime_dir / "runtime.env"
    path.unlink(missing_ok=True)
    write_secret(path, content)
    print(f"cache_salt_chars={len(cache_salt)}")


if __name__ == "__main__":
    main()
