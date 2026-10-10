#!/usr/bin/env python3
"""Serve images that have been evicted from conversation context.

``model-proxy`` replaces an inline image with a short handle once an assistant turn has
answered it, because the base64 otherwise rides along on every later step of the session:
measured on a real request, images were 92.4% of the body. That eviction is only safe if the
image stays reachable, and this is the way back.

It is deliberately not a replacement for ``ReadMediaFile``. Eviction is origin-agnostic, so a
``ReadMediaFile`` read is evicted and re-read through the same path it arrived by. This server
covers the case that has no path to return to: an image the user attached through the UI,
which reaches the model as inline bytes with nothing in the transcript naming the file.

Handles are resolved against the session media directories, which is why resolution lives
here rather than in the proxy: this process runs in the agent container and can read
``$HOME/.kimi-code``, and the proxy cannot.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

PROTOCOL_FALLBACK = "2025-03-26"
PROTOCOL_SUPPORTED = ("2025-06-18", "2025-03-26", "2024-11-05")
SERVER_NAME = "analyze_image"
SERVER_VERSION = "1"

#: Refuse to hand back anything larger than the built-in reader would accept.
MAX_IMAGE_BYTES = 100 * 1024 * 1024

MEDIA_SUBDIRS = ("media", "media-originals")

MIME_BY_SUFFIX = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}

CONTINUE_INSTRUCTION = (
    "Afterward, continue with the task you were working on previously."
)


class HandleError(Exception):
    """A handle could not be resolved to exactly one image."""


@dataclass
class Image:
    """One candidate file, with every name it might be asked for."""

    path: Path
    mime: str
    size: int
    digests: tuple[str, ...]
    names: tuple[str, ...]
    #: The roots this was found under, kept so a later read can re-prove containment rather
    #: than trusting that nothing changed between cataloguing and returning the bytes.
    roots: tuple[Path, ...] = ()

    @property
    def handle(self) -> str:
        return f"sha256:{self.digests[0]}"

    @property
    def display(self) -> str:
        """The name a human would recognise: the attached filename if one was recorded."""
        for name in self.names:
            if name not in (self.path.name, self.path.stem):
                return name
        return self.path.name


@dataclass
class Catalogue:
    images: list[Image] = field(default_factory=list)
    scanned: int = 0


def session_owned_by(session_dir: Path, cwd: Path) -> bool:
    """Whether this stored session belongs to the project the agent is working in.

    ``state.json`` records the cwd the session was created in, which is the only fact that
    survives the session id not being passed down to an MCP subprocess. Matching on it keeps
    ``list_images`` inside the operator's current project: a digest handle still resolves from
    anywhere, because a handle is only ever handed out to the session that produced it, but a
    *listing* must not read out attachments belonging to an unrelated project.
    """
    try:
        state = json.loads((session_dir / "state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    recorded = state.get("cwd")
    if not isinstance(recorded, str) or not recorded:
        return False
    try:
        return Path(recorded).resolve() == cwd
    except OSError:
        return False


def media_roots(home: Path, cwd: Path | None = None) -> list[Path]:
    """Media directories belonging to sessions of this project.

    With no ``cwd`` there is nothing to scope against and nothing is offered: guessing a wider
    scan would expose other projects' attachment names to a session that never touched them.
    """
    roots: list[Path] = []
    sessions = home / ".kimi-code" / "sessions"
    if cwd is None or not sessions.is_dir():
        return roots
    for workspace in sorted(sessions.iterdir()):
        if not workspace.is_dir():
            continue
        for session in sorted(workspace.iterdir()):
            if not session.is_dir() or not session_owned_by(session, cwd):
                continue
            for subdir in MEDIA_SUBDIRS:
                candidate = session / subdir
                if candidate.is_dir():
                    roots.append(candidate)
            for agent_home in (session / "agents").glob("*/media"):
                if agent_home.is_dir():
                    roots.append(agent_home)
    return roots


def read_names(path: Path) -> tuple[str, ...]:
    """Names this image may be called by: its own, and the original the user attached.

    Kimi stores an attachment under a generated key and keeps the user's filename in a sibling
    ``meta`` record, so without this a handle could only ever be the digest.
    """
    names = [path.name, path.stem]
    meta = path.parent / "meta" / f"{path.stem}.json"
    if meta.is_file():
        try:
            declared = json.loads(meta.read_text(encoding="utf-8")).get("name")
        except (OSError, ValueError):
            declared = None
        if isinstance(declared, str) and declared:
            names.append(declared)
    return tuple(dict.fromkeys(n for n in names if n))


def readable_inside(path: Path, roots: list[Path]) -> bool:
    """Whether this path is a real file inside a media root, with no symlink in the way.

    The media directories sit in the agent's own writable volume, so anything the agent wrote
    there — including a symlink to a credential file — would otherwise be handed back to the
    model as an image. That is a route around the read tools' secret filtering, which is why
    this reads nothing until it has proved the target is a plain file below a media root, and
    why ``:`` following a symlink is refused rather than resolved.
    """
    if path.is_symlink():
        return False
    try:
        if not path.is_file():
            return False
        real = path.resolve()
    except OSError:
        return False
    return any(real.is_relative_to(root.resolve()) for root in roots)


def build_catalogue(roots: list[Path]) -> Catalogue:
    catalogue = Catalogue()
    for root in roots:
        for path in sorted(root.rglob("*")):
            catalogue.scanned += 1
            if path.suffix.lower() not in MIME_BY_SUFFIX:
                continue
            if not readable_inside(path, roots):
                continue
            try:
                # Sized before reading: the cap is 100 MiB and a directory of near-cap files
                # would otherwise be pulled into memory one at a time to discover none fit.
                if not 0 < path.stat().st_size <= MAX_IMAGE_BYTES:
                    continue
                raw = path.read_bytes()
            except OSError:
                continue
            if not raw or len(raw) > MAX_IMAGE_BYTES:
                continue
            encoded = base64.b64encode(raw).decode("ascii")
            # The proxy hashes the base64 payload exactly as it travelled in the data URL, so
            # that is the primary digest. The raw-bytes digest is offered too: it is what
            # anyone inspecting the file on disk would compute.
            catalogue.images.append(
                Image(
                    path=path,
                    mime=MIME_BY_SUFFIX[path.suffix.lower()],
                    size=len(raw),
                    digests=(
                        hashlib.sha256(encoded.encode("ascii")).hexdigest(),
                        hashlib.sha256(raw).hexdigest(),
                    ),
                    names=read_names(path),
                    roots=tuple(roots),
                )
            )
    return catalogue


def _matches(image: Image, lowered: str) -> bool:
    """Whether a normalised handle names this image, by digest prefix or by any of its names."""
    return any(digest.startswith(lowered) for digest in image.digests) or lowered in {
        name.lower() for name in image.names
    }


def resolve(handle: str, catalogue: Catalogue) -> Image:
    """Find the one image a handle names.

    Accepts the proxy's ``sha256:`` handle, a full or abbreviated digest, a media key such as
    ``f_5241a03f``, or the original filename the user attached. An abbreviation is only a
    match while it is unambiguous, because guessing which of two scans the model meant is
    worse than asking.
    """
    def normalise(text: str) -> str:
        text = text.strip().strip("\"'`“”()[]{}")
        if text.lower().startswith("sha256:"):
            text = text[len("sha256:") :]
        return text.strip().rstrip(".,;:!?)")

    # Try the handle exactly as given before tidying it. A filename is a legal handle and may
    # legitimately end in punctuation a strip would eat, so the literal reading has to win;
    # the tidied one exists because a model copying a handle out of a sentence tends to bring
    # the punctuation with it, and "no image matches" over one character is a poor way to say so.
    attempts = list(dict.fromkeys(k for k in (handle.strip(), normalise(handle)) if k))
    if not attempts:
        raise HandleError("empty handle")

    found: list[Image] = []
    for candidate in attempts:
        lowered = candidate.lower()
        found = [image for image in catalogue.images if _matches(image, lowered)]
        if found:
            break

    if not found:
        raise HandleError(
            f"no image matches {handle!r}; {len(catalogue.images)} images are available "
            "from list_images"
        )
    exact = [
        image
        for image in found
        if lowered in image.digests or lowered in {name.lower() for name in image.names}
    ]
    if len(found) > 1 and not exact:
        raise HandleError(
            f"handle {handle!r} is ambiguous across {len(found)} images; "
            "use a longer sha256 prefix"
        )
    return (exact or found)[0]


def tool_result(image: Image, prompt: str) -> dict:
    """The payload the model sees: its own question, then the picture, then the resume cue."""
    # Re-checked at read time, not only at catalogue time: the file was catalogued when the
    # handle was minted and may have been replaced with a symlink before this call.
    if not readable_inside(image.path, image.roots):
        raise HandleError(f"{image.path.name} is no longer a readable file in this session")
    raw = image.path.read_bytes()
    if not 0 < len(raw) <= MAX_IMAGE_BYTES:
        raise HandleError(f"{image.path.name} is too large to return ({len(raw):,} bytes)")
    encoded = base64.b64encode(raw).decode("ascii")
    lead = prompt.strip() or "Describe this image."
    return {
        "content": [
            {
                "type": "text",
                "text": (
                    f"{lead}\n\n"
                    f"[image {image.handle} from {image.path.name}, {image.size:,} bytes]"
                ),
            },
            {"type": "image", "data": encoded, "mimeType": image.mime},
            {"type": "text", "text": CONTINUE_INSTRUCTION},
        ]
    }


def listing(catalogue: Catalogue) -> dict:
    if not catalogue.images:
        text = "No stored images found. Images attached in this session appear here."
    else:
        rows = [
            f"{image.handle}  {image.size:>10,} B  {image.mime:<10}  {image.display}"
            for image in catalogue.images
        ]
        text = "Available images:\n" + "\n".join(rows)
    return {"content": [{"type": "text", "text": text}]}


TOOLS = [
    {
        "name": "analyze_image",
        "description": (
            "Re-read an image that has left the conversation context, and answer a specific "
            "question about it. Pass the `handle` shown by an eviction notice, or a value "
            "from list_images, and write `prompt` as the question you want answered from the "
            "pixels. The image and your question arrive together; answer the question, then "
            "continue where you left off. Use this whenever you need detail you did not "
            "capture earlier; do not use it to recall something you already wrote down."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "handle": {
                    "type": "string",
                    "description": "sha256:<prefix>, a media key, or the original filename.",
                },
                "prompt": {
                    "type": "string",
                    "description": "What to extract from the image this time.",
                },
            },
            "required": ["handle", "prompt"],
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "list_images",
        "description": (
            "List the images this session can re-read, with their handles, sizes and "
            "original filenames."
        ),
        "inputSchema": {"type": "object", "properties": {}},
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
]


def respond(request: dict, home: Path, protocol: str, cwd: Path | None = None) -> dict | None:
    method = request.get("method")
    params = request.get("params") or {}
    request_id = request.get("id")

    if method == "initialize":
        asked = params.get("protocolVersion")
        protocol = asked if asked in PROTOCOL_SUPPORTED else PROTOCOL_FALLBACK
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "protocolVersion": protocol,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                "instructions": (
                    "Answers questions about images that have been evicted from this "
                    "session's context. Call analyze_image with the handle from an eviction "
                    "notice and the question you need answered."
                ),
            },
        }

    if request_id is None:
        return None  # notifications need no reply

    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": request_id, "result": {"tools": TOOLS}}

    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments") or {}
        catalogue = build_catalogue(media_roots(home, cwd if cwd is not None else Path.cwd()))
        try:
            if name == "list_images":
                result = listing(catalogue)
            elif name == "analyze_image":
                handle = arguments.get("handle")
                prompt = arguments.get("prompt")
                if not isinstance(handle, str) or not isinstance(prompt, str):
                    raise HandleError("both handle and prompt are required strings")
                result = tool_result(resolve(handle, catalogue), prompt)
            else:
                return {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32602, "message": f"unknown tool {name!r}"},
                }
        except (HandleError, OSError) as exc:
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {"content": [{"type": "text", "text": str(exc)}], "isError": True},
            }
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": -32601, "message": f"method {method!r} is not supported"},
    }


def main() -> int:
    home = Path(os.environ.get("HOME", "/home/agent")).expanduser()
    cwd = Path.cwd()
    # stdout is the protocol channel. Anything this server says about itself goes to stderr,
    # because a stray line there would corrupt the stream for the client.
    stream = sys.stdout
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except (json.JSONDecodeError, RecursionError, ValueError):
            # RecursionError is not a subclass of JSONDecodeError: a body of 200k nested
            # brackets raises it out of json.loads, and losing the process here would hang
            # the client until its own timeout with no explanation in any log.
            continue
        if not isinstance(request, dict):
            # Anything that is valid JSON but not an object (null, a list, a bare string)
            # has no method and no id to answer with; the protocol says a server must not
            # invent one.
            continue
        try:
            reply = respond(request, home, PROTOCOL_FALLBACK, cwd)
        except Exception as exc:  # a crash here would hang the client until its timeout
            sys.stderr.write(f"analyze_image: {type(exc).__name__}: {exc}\n")
            reply = None
            if request.get("id") is not None:
                reply = {
                    "jsonrpc": "2.0",
                    "id": request.get("id"),
                    "error": {"code": -32603, "message": str(exc)},
                }
        if reply is not None:
            stream.write(json.dumps(reply, separators=(",", ":")) + "\n")
            stream.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
