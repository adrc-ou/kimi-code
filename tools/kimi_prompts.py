#!/usr/bin/env python3
"""Read Kimi Code's own built-in prompt blocks out of its bundle, so an operator can quote one.

``${kimi.system_default}`` and friends let ``SYSTEM.md`` embed a *piece* of the built-in prompt
rather than the whole of it, which is the only way to keep a customised prompt that inherits an
upstream edit. ``${base_prompt}`` wraps everything; this takes one part.

Anchor **regexes**, never byte offsets. Offsets belong to one minified build and move on every
upgrade, and a stale offset does not fail — it reads a neighbouring string and quietly ships the
wrong instructions. Every name here is found by matching ``IDENTIFIER =`` at a statement boundary
and parsing the string literal that follows, which survives reordering, renaming of everything
around it, and a shift of several megabytes.

The bundle is 182 MB and lives inside the agent image, not on the host, so this module is normally
run there (``./prompts.sh --extract``) and its output cached under the instance runtime directory
keyed by image digest. ``render_runtime`` reads that cache; a cold cache is an error it reports as
"run ``./prompts.sh --extract``", never a placeholder shipped to the model as literal text.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import mmap
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_TOOLS_DIR = Path(__file__).resolve().parent
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))
if __package__:
    from . import prompt_context as pc
    from . import prompt_measure
else:
    import prompt_context as pc  # one direction only; prompt_context never imports this
    import prompt_measure  # estimated sizes are reported, never used as a decision

#: Where the cache lives, relative to the instance runtime directory.
CACHE_DIRECTORY = "kimi-prompts"
#: The file ``render_runtime`` reads. It names the image it came from, so a refresh is visible.
CACHE_FILE = "literals.json"

#: Placeholder name, then the bundle identifiers to try for it in order. The first entry is the
#: name a build is expected to keep; the rest are spellings this build is known to use today, so
#: one rename upstream does not turn a working override into a hard staging failure.
_IDENTIFIERS: dict[str, tuple[str, ...]] = {
    "kimi.system_default": ("system_default",),
    "kimi.coder_role": ("CODER_ROLE", "coder_role"),
    "kimi.explore_overlay": ("explore_overlay_default", "explore_overlay"),
    "kimi.task_agent_prefix": ("TASK_AGENT_ROLE_PREFIX", "task_agent_prefix"),
}

# The names themselves are declared once, in ``prompt_context.kimi_literal_names()``; this table
# owns the bundle spellings each is read from. A literal in one list and not the other would be
# exempt from both placeholder gates without anything failing, so the two sets are compared at
# import time rather than discovered at first use.
if set(_IDENTIFIERS) != set(pc.kimi_literal_names()):
    raise ImportError(
        "the kimi literal lists have drifted: "
        f"named only here {sorted(set(_IDENTIFIERS) - set(pc.kimi_literal_names()))}, "
        f"only there {sorted(set(pc.kimi_literal_names()) - set(_IDENTIFIERS))}"
    )

#: Every identifier we know, back to the placeholder an operator writes. A backtick literal
#: interpolates its siblings by bundle name, so the second pass resolves through this.
_BY_IDENTIFIER = {
    identifier: name
    for name, candidates in _IDENTIFIERS.items()
    for identifier in candidates
}

#: When each of Kimi's template variables actually carries text, read off ``systemPromptVars`` in
#: the pinned bundle. These describe that build, not a permanent property of Kimi, which is why the
#: panel prints the ones it can compute (whether a name appears in the built-in prompt, and whether
#: it appears in the operator's files) instead of asserting them here.
PLACEHOLDER_CONDITIONS: dict[str, str] = {
    "role_additional": "always empty in this build, whatever the request",
    "product_name": "the product name, defaulted to Kimi Code CLI",
    "reply_style_guide": "a fixed paragraph about replying in Markdown",
    "notify_user_guidance": "only while the notify-user feature is active",
    "os": "the OS kind, and empty when the build cannot name it",
    "windows_notes": "only on Windows, so empty on this stack",
    "shell": "only when the request carries a shell name",
    "cwd": "always: the working directory",
    "cwd_listing": "always: the two-level listing of the workspace",
    "agents_md": "the hoisted project instructions, empty when the workspace has none",
    "additional_dirs_info": "only when extra directories are configured for the session",
    "additional_dirs_section": "only when additional_dirs_info carries anything",
    "skills": "only when a skill is active in the request",
    "skills_section": "only when skills carries anything",
    "plugin_sections": "only when a plugin supplies instructions",
}
_QUOTES = (b'"', b"'", b"`")
#: A ``${NAME}`` inside a JS template literal, where NAME may be a bundle identifier.
_INTERPOLATED = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def anchor(identifier: str) -> re.Pattern[bytes]:
    """The assignment that introduces one literal, at a statement boundary.

    The boundary class is why this cannot land inside another identifier: searching for
    ``CODER_ROLE`` alone would also match a ``MY_CODER_ROLE``. Assignment only, not ``==``,
    because a comparison reads the same literal back from a place that is not its definition.
    """
    return re.compile(rb"[\n\t;{}]" + re.escape(identifier.encode()) + rb" *=[ \t\n]*")


def read_literal(view: mmap.mmap, start: int) -> tuple[bytes, int] | None:
    """The raw source bytes of the string literal at ``start``, and the offset after it.

    Escapes are kept as written; decoding happens once, in :func:`decode_literal`, so a quote
    inside ``\\"`` cannot end the literal early.
    """
    index = start
    length = len(view)
    while index < length and view[index : index + 1] in (b" ", b"\t", b"\n"):
        index += 1
    if index >= length or view[index : index + 1] not in _QUOTES:
        return None
    quote = view[index : index + 1]
    index += 1
    out = bytearray()
    while index < length:
        byte = view[index : index + 1]
        if byte == b"\\":
            out += view[index : index + 2]
            index += 2
            continue
        if byte == quote:
            return bytes(out), index + 1
        out += byte
        index += 1
    return None


#: Every escape a JS string literal can carry, other than the two numeric forms.
_ESCAPES = {
    "n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f", "v": "\v", "0": "\0",
    "\\": "\\", '"': '"', "'": "'", "`": "`", "/": "/",
}
#: One escape sequence, so everything between two of them can be read as plain UTF-8.
_ESCAPE_RUN = re.compile(rb"\\(u[0-9a-fA-F]{4}|x[0-9a-fA-F]{2}|.)", re.DOTALL)


def decode_literal(raw: bytes) -> str:
    """Expand a JS literal's escapes without losing the UTF-8 the bundle stores unescaped.

    The obvious one-liner, ``unicode_escape``, decodes byte-wise and so shreds every real
    em-dash in the source into three latin-1 characters; and repairing that with a latin-1
    round trip then explodes on a numeric escape like ``\\u2014``, which *is* meant to become a
    non-ASCII character. Reading the escapes one at a time and decoding the runs between them as
    UTF-8 is correct in both directions, and it is explicit enough to trust.

    An unrecognised escape keeps its character and loses its backslash, which is what JS does.
    """
    parts: list[str] = []
    position = 0
    for run in _ESCAPE_RUN.finditer(raw):
        parts.append(raw[position:run.start()].decode("utf-8"))
        body = run.group(1)
        if body[:1] in (b"u", b"x"):
            parts.append(chr(int(body[1:].decode("ascii"), 16)))
        else:
            parts.append(_ESCAPES.get(body.decode("utf-8"), body.decode("utf-8")))
        position = run.end()
    parts.append(raw[position:].decode("utf-8"))
    return "".join(parts)


def find(view: mmap.mmap, name: str) -> str | None:
    """The decoded literal behind one placeholder name, or ``None`` if this bundle lacks it."""
    for identifier in _IDENTIFIERS.get(name, ()):
        match = anchor(identifier).search(view)
        if not match:
            continue
        literal = read_literal(view, match.end())
        if literal is not None:
            return decode_literal(literal[0])
    return None


def interpolate(literals: dict[str, str]) -> dict[str, str]:
    """Expand ``${BUNDLE_NAME}`` references left inside a template literal.

    ``CODER_ROLE`` is written as a backtick string that interpolates ``TASK_AGENT_ROLE_PREFIX``,
    so reading it alone would ship a placeholder nobody intended. Resolution is one pass over the
    finished set, which is enough because no literal here interpolates another interpolated one;
    a name outside our set is left alone rather than guessed at.
    """
    resolved = {}
    for name, text in literals.items():
        resolved[name] = _INTERPOLATED.sub(
            lambda match: literals.get(_BY_IDENTIFIER.get(match.group(1), ""), match.group(0)),
            text,
        )
    return resolved


def extract(view: mmap.mmap) -> dict[str, str]:
    """Every promised literal, keyed by the ``${kimi.*}`` name an operator writes.

    Raises :class:`KeyError` on a name the bundle does not carry: a build that dropped one of
    these is a build this harness should not guess about, and a half-filled cache is worse than an
    empty one because it substitutes confidently wrong text.
    """
    raw = {}
    for name in pc.kimi_literal_names():
        if name not in _IDENTIFIERS:
            raise KeyError(f"{name} has no bundle identifier to look for")
        found = find(view, name)
        if found is None:
            candidates = ", ".join(_IDENTIFIERS[name])
            raise KeyError(f"{name}: none of these string literals ({candidates}) in this bundle")
        raw[name] = found
    return interpolate(raw)


def document(literals: dict[str, str], image: str = "") -> dict[str, Any]:
    """The cache file's contents: the text, plus what build it came from and its fingerprints."""
    return {
        "schema_version": 1,
        "image": image,
        "extractedAt": datetime.now(UTC).isoformat(timespec="seconds"),
        "counts": {
            name: {
                "characters": len(text),
                "bytes": len(text.encode()),
                "tokensEstimated": prompt_measure.estimate_tokens(text),
                "sha256": hashlib.sha256(text.encode()).hexdigest(),
            }
            for name, text in sorted(literals.items())
        },
        "literals": dict(sorted(literals.items())),
    }


def cache_path(runtime_dir: Path) -> Path:
    return runtime_dir / CACHE_DIRECTORY / CACHE_FILE


def read_document(runtime_dir: Path) -> dict[str, Any] | None:
    """The whole cache document, or ``None`` when there is nothing readable to show."""
    try:
        parsed = json.loads(cache_path(runtime_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def load(runtime_dir: Path) -> dict[str, str]:
    """The cached literals, or ``{}`` when nothing has been extracted on this machine yet."""
    parsed = read_document(runtime_dir)
    if parsed is None:
        return {}
    literals = parsed.get("literals")
    if not isinstance(literals, dict):
        return {}
    return {str(key): str(value) for key, value in literals.items()}


def store(runtime_dir: Path, literals: dict[str, str], image: str = "") -> Path:
    """Write the cache, reporting when it replaces a different build's text.

    A changed literal is not a failure: it means upstream edited the prompt, which is exactly the
    case this mechanism exists to follow without the operator doing anything.
    """
    path = cache_path(runtime_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    previous = load(runtime_dir)
    changed = sorted(name for name, text in literals.items() if previous.get(name) != text)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(document(literals, image), indent=1) + "\n", encoding="utf-8")
    temporary.replace(path)
    path.chmod(0o600)
    if changed and previous:
        print(
            "Kimi updated; the measured literals were refreshed for: " + ", ".join(changed),
            file=sys.stderr,
        )
    return path


def values(runtime_dir: Path) -> dict[str, str]:
    """The ``${kimi.*}`` half of the harness's substitutions, keyed by placeholder name."""
    return load(runtime_dir)


def substitutions(runtime_dir: Path) -> dict[str, str]:
    """Everything the harness resolves in a template: its own values plus this image's literals.

    Both ``render_runtime`` and the startup panel compose the same documents, so both must call
    this rather than assemble the mapping their own way, or the panel prices one prompt and the
    container mounts another.
    """
    return {**pc.harness_values(), **values(runtime_dir)}


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="tools/kimi_prompts.py",
        description=(__doc__ or "").splitlines()[0],
    )
    parser.add_argument(
        "--bundle",
        type=Path,
        default=Path("/usr/local/bin/kimi"),
        help="the Kimi Code executable to read literals out of",
    )
    parser.add_argument(
        "--runtime-dir",
        type=Path,
        help="instance runtime directory to cache into; omit when using --print",
    )
    parser.add_argument("--image", default="", help="image digest this bundle came from")
    parser.add_argument(
        "--print",
        action="store_true",
        dest="to_stdout",
        help="write the cache document to stdout instead of a file, for one-shot extraction",
    )
    parser.add_argument(
        "--show",
        action="store_true",
        help="print what is cached, with sizes, and extract nothing",
    )
    return parser.parse_args(argv)


def show(runtime_dir: Path) -> int:
    """Report the cache: which image it came from, and how big each literal is."""
    parsed = read_document(runtime_dir)
    if parsed is None:
        print(pc.COLD_CACHE, file=sys.stderr)
        return 1
    stream = sys.stdout
    print(f"image  {parsed.get('image') or 'unknown'}", file=stream)
    print(f"cached {parsed.get('extractedAt') or 'unknown'}", file=stream)
    literals = parsed.get("literals")
    for name, text in sorted((literals or {}).items()):
        if not isinstance(text, str):
            continue
        print(
            f"  {name:<24}{len(text.encode()):>8d} bytes"
            f"  {prompt_measure.estimate_tokens(text):>7d} tokens (estimated)",
            file=stream,
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(list(sys.argv[1:] if argv is None else argv))
    if args.show:
        if args.runtime_dir is None:
            print("--show needs --runtime-dir", file=sys.stderr)
            return 2
        return show(args.runtime_dir)
    literals = _extract_file(args.bundle)
    if literals is None:
        return 1
    if args.to_stdout:
        sys.stdout.write(json.dumps(document(literals, args.image), indent=1) + "\n")
        return 0
    if args.runtime_dir is None:
        print("cache where? pass --runtime-dir, or --print to write stdout", file=sys.stderr)
        return 2
    store(args.runtime_dir, literals, args.image)
    print(f"cached {len(literals)} literals for {args.image or 'an unlabelled image'}")
    return 0


def _extract_file(bundle: Path) -> dict[str, str] | None:
    try:
        with bundle.open("rb") as handle:
            with mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ) as view:
                return extract(view)
    except (OSError, ValueError) as error:
        print(f"{bundle}: {error}", file=sys.stderr)
        return None
    except KeyError as error:
        print(f"{bundle}: {str(error).strip(chr(39))}", file=sys.stderr)
        return None


if __name__ == "__main__":
    raise SystemExit(main())
