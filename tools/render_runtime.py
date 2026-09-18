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

The staged system prompt is the first of the project-root ``SYSTEM.md`` or ``SYSTEM.md.example``
that exists, and nothing otherwise, which leaves Kimi Code on its own built-in prompt. Selected
module guidance and the generated runtime envelope are appended to that text unless
``KIMI_SYSTEM_PROMPT_OMIT_ENVELOPE`` says to leave them out, so nothing is ever written inside the
workspace. A prompt file that is blank and has nothing appended stages as a lone period instead,
because Kimi Code reads a blank file as "no file" and substitutes its own prompt.
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
    from . import policy
    from .env_values import read_env_values
    from .model_config import render as render_model_tables
    from .models import POLICY_FILE, load_plan, materialise_credentials
else:
    import policy
    from env_values import read_env_values
    from model_config import render as render_model_tables

    from models import POLICY_FILE, load_plan, materialise_credentials

#: Marker comment in runtime/config.toml that the generated tables replace.
MODEL_MARKER = "#__KIMI_MODEL_CONFIG__"
#: System prompt sources, tried in order from the project root, the way ``.env`` and
#: ``.env.example`` are tried: the operator's own untracked file, then the harness default. No
#: file at all means stage nothing, which is how Kimi Code is told to use its own.
SYSTEM_PROMPT_FILES = ("SYSTEM.md", "SYSTEM.md.example")
#: Where ``tools/modules.py`` stages the selected modules' guidance, appended to the prompt. The
#: modules write it before this tool runs, and it is never merged into a workspace file.
MODULE_GUIDANCE_FILE = "module-guidance.md"
#: Switch read from the resolved ``.env``. The name carries the omission, so a truthy value is what
#: leaves the runtime envelope out of the prompt; unset, blank, or a value that is not truthy omits
#: nothing and leaves the envelope appended, which is the shipped default.
OMIT_ENVELOPE_FLAG = "KIMI_SYSTEM_PROMPT_OMIT_ENVELOPE"
TRUTHY_VALUES = frozenset({"1", "true", "yes", "on"})
#: Kimi Code drops any system prompt that is blank once trimmed and silently uses its built-in
#: prompt, so an operator who asks for no prompt cannot have one through the file. A lone period
#: survives that check, costs one token, and carries no instruction of its own.
EMPTY_PROMPT_SENTINEL = "."
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


def system_prompt(root: Path) -> str | None:
    """Read the session's prompt file: the operator's own, else the harness default.

    ``None`` means no prompt file exists at all - the documented last tier, which stages nothing
    and leaves Kimi Code on its own built-in prompt. The text of a file that does exist is the
    whole prompt for the session, so a file that omits the ``${base_prompt}`` placeholder is a
    complete replacement of Kimi Code's own prompt rather than an error. An empty file is a
    decision and not a missing file: it yields empty text instead of falling back to the default,
    and ``session_prompt`` turns that into ``EMPTY_PROMPT_SENTINEL``, because Kimi Code reads a
    blank file the same way it reads no file at all.
    """
    for relative in SYSTEM_PROMPT_FILES:
        path = root / relative
        if path.is_file():
            return path.read_text(encoding="utf-8")
    return None


def envelope_omitted(values: dict[str, str]) -> bool:
    """Whether the operator asked for the runtime envelope to be left out of the prompt.

    The flag is named after the omission, so only a truthy value performs it. Unset, blank, or a
    value that is not truthy omits nothing, which keeps the envelope in the prompt - the shipped
    default that an operator who never heard of the flag gets.
    """
    return values.get(OMIT_ENVELOPE_FLAG, "").strip().lower() in TRUTHY_VALUES


def module_guidance(runtime_dir: Path) -> str:
    """The guidance ``tools/modules.py`` staged for the selected modules, if any."""
    path = runtime_dir / MODULE_GUIDANCE_FILE
    return path.read_text(encoding="utf-8") if path.is_file() else ""


def session_prompt(
    root: Path, runtime_dir: Path, plan: dict[str, Any], values: dict[str, str]
) -> str:
    """Assemble what Kimi reads: prompt file, module guidance, then the envelope.

    The prompt file keeps its meaning as a complete replacement for Kimi's own, and an empty prompt
    file stays empty in its own right - it is only the blankness of the final text that matters,
    since Kimi Code would read that as "no file" and answer with its built-in prompt. So when there
    is nothing to append - no module selected and the envelope omitted - an empty prompt file stages
    as ``EMPTY_PROMPT_SENTINEL``, while an absent one stages as nothing at all. Appended sections
    are trimmed of edge newlines and separated by a blank line, so the prompt file's own trailing
    newlines cannot pile up.
    """
    text = system_prompt(root)
    extra = [module_guidance(runtime_dir)]
    if not envelope_omitted(values):
        extra.append(policy.render_guidance(plan))
    additions = [part.strip("\n") for part in extra if part.strip()]
    if not additions:
        if text is None:
            return ""
        return text if text.strip() else EMPTY_PROMPT_SENTINEL + "\n"
    base = [text.strip("\n")] if text and text.strip() else []
    return "\n\n".join([*base, *additions]) + "\n"


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
    # The prompt file's own text plus whatever the harness appends, or an empty string that leaves
    # Kimi Code on its built-in prompt. Whatever lands here is installed by the initializer as
    # root-owned and immutable, so customising the prompt never hands the agent a writable one.
    system_markdown = args.runtime_dir / "SYSTEM.md"
    system_markdown.unlink(missing_ok=True)
    write_secret(system_markdown, session_prompt(args.root, args.runtime_dir, plan, values))
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
