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

The harness stages two documents, not one. ``CONTEXT.md`` composes into the all-lane contract at
``~/.kimi-code/AGENTS.md``, which Kimi substitutes into the main agent's prompt and every
subagent's alike; ``SYSTEM.md`` composes into the main agent's own prompt and reaches nobody else.
Both are assembled by ``tools/prompt_context.py`` from the operator's files, the selected modules'
guidance, and the generated envelope sections the startup panel left switched on - the panel's
choices are the only way to omit any of it, and nothing is ever written inside the workspace. An
empty ``SYSTEM.md`` with every main-only add-on switched off stages as a lone period, because Kimi
Code reads a blank file as "no file" and substitutes its own prompt.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import secrets
import shlex
from pathlib import Path
from typing import Any

if __package__:
    from . import prompt_context
    from .env_values import read_env_values
    from .kimi_prompts import substitutions as kimi_substitutions
    from .model_config import render as render_model_tables
    from .models import POLICY_FILE, load_plan, materialise_credentials
    from .private_file import write_private
else:
    import prompt_context
    from env_values import read_env_values
    from kimi_prompts import substitutions as kimi_substitutions
    from model_config import render as render_model_tables
    from private_file import write_private

    from models import POLICY_FILE, load_plan, materialise_credentials

#: Marker comment in runtime/config.toml that the generated tables replace.
MODEL_MARKER = "#__KIMI_MODEL_CONFIG__"
#: Where ``tools/modules.py`` stages the selected modules' guidance. The modules write it before
#: this tool runs, and it is never merged into a workspace file.
MODULE_GUIDANCE_FILE = "module-guidance.md"
ALIAS_MARKER = "__KIMI_PRIMARY_ALIAS__"
PLACEHOLDER_MARKER = "__MODEL_PROXY_TOKEN__"


def write_secret(path: Path, value: str) -> None:
    """Stage one rendered file at mode 0600. See :func:`private_file.write_private`."""
    write_private(path, value)


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


def module_guidance(runtime_dir: Path) -> str:
    """The guidance ``tools/modules.py`` staged for the selected modules, if any."""
    path = runtime_dir / MODULE_GUIDANCE_FILE
    return path.read_text(encoding="utf-8") if path.is_file() else ""


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--runtime-dir", type=Path, required=True)
    parser.add_argument("--resolved-env", type=Path, required=True)
    parser.add_argument("--prompt-context", type=Path)
    args = parser.parse_args()
    args.runtime_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(args.runtime_dir, 0o700)
    values = read_env_values(args.resolved_env)
    plan = load_plan(args.runtime_dir)
    materialise_credentials(plan, args.runtime_dir, values)
    # The startup panel's choices, or every option on when nobody drew the panel. This is the only
    # read of that file outside the panel and prompts.sh, and it is what keeps the picture the
    # operator approved and the bytes the container mounts from disagreeing.
    prefs_path = args.prompt_context or args.runtime_dir / prompt_context.PREFS_FILE
    enabled = prompt_context.load_prefs(prefs_path)
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
    rendered = prompt_context.apply_config_toggles(
        kimi_config(args.root, plan, ephemeral["proxy-token"]), enabled
    )
    write_secret(args.runtime_dir / "kimi-config.toml", rendered)
    # Both documents are installed by the initializer as root-owned and immutable, so customising
    # a prompt never hands the agent a writable one. The all-lane file is always written, even when
    # it resolves to empty: Docker turns a missing bind source into a directory, and that fails at
    # container start rather than here.
    # Placeholders the harness resolves itself, before anything is staged: its own dynamic values,
    # plus any Kimi literals cached from this image. A literal that was never extracted is a
    # staging error rather than a silent literal, so the mapping is assembled even when neither
    # operator file is expected to use one.
    template_values = kimi_substitutions(args.runtime_dir)
    agents_markdown = args.runtime_dir / prompt_context.STAGED_AGENTS
    system_markdown = args.runtime_dir / prompt_context.STAGED_SYSTEM
    agents_markdown.unlink(missing_ok=True)
    system_markdown.unlink(missing_ok=True)
    write_secret(
        agents_markdown,
        prompt_context.compose_agents_document(
            args.root, plan, module_guidance(args.runtime_dir), enabled, template_values
        ),
    )
    write_secret(
        system_markdown,
        prompt_context.compose_system_document(args.root, plan, enabled, template_values),
    )
    runtime_env = {
        "SEARCH_ADAPTER_TOKEN": ephemeral["search-token"],
        "MODEL_PROXY_POLICY_FILE": str(args.runtime_dir / POLICY_FILE),
        "MODEL_PROXY_INTERNAL_TOKEN_FILE": str(args.runtime_dir / "proxy-token"),
        "MODEL_PROXY_CACHE_SALT_FILE": str(args.runtime_dir / "cache-salt"),
        "KIMI_RENDERED_CONFIG": str(args.runtime_dir / "kimi-config.toml"),
        "KIMI_RENDERED_AGENTS_MD": str(agents_markdown),
        "KIMI_SYSTEM_MD": str(system_markdown),
    }
    prompt_context.apply_env_toggles(runtime_env, enabled)
    content = "".join(f"{key}={shlex.quote(value)}\n" for key, value in runtime_env.items())
    path = args.runtime_dir / "runtime.env"
    path.unlink(missing_ok=True)
    write_secret(path, content)
    # The staged documents are installed read-only and immutable, so an edit after this moment
    # cannot take effect. Recording what each one was composed from is what lets the launcher at
    # readiness, and the prompt panel afterwards, say that in words instead of leaving the operator
    # to guess why nothing changed.
    sources = args.runtime_dir / prompt_context.SOURCES_FILE
    sources.unlink(missing_ok=True)
    write_secret(sources, json.dumps(prompt_context.document_sources(args.root), indent=1) + "\n")
    print(f"cache_salt_chars={len(cache_salt)}")


if __name__ == "__main__":
    main()
