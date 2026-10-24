"""One launcher-owned spinner, with serialized handoff to each foreground painter.

Only coordinates and capability flags cross processes. Frames (including masked
credential fields) stay in the renderer's memory, never in runtime state files.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import time
from pathlib import Path

from .caps import ANSI16, NONE, Caps
from .cells import MOVE, RESET, SYNC_OFF, SYNC_ON

if __package__.startswith("tools."):
    from ..private_file import write_private_json
else:
    from private_file import write_private_json

CLAIM = "screen-claim"
STATE = "screen-wait.json"
LOCK = "screen-render.lock"
INTERVAL = 0.1
UNICODE_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
ASCII_FRAMES = "|/-\\"


def runtime() -> str:
    return os.environ.get("HARNESS_RUNTIME_DIR", "").strip()


@contextlib.contextmanager
def rendering(directory: str | None = None):
    """Serialize spinner output with screen changes; feedback must never stall a picker."""
    directory = runtime() if directory is None else directory
    descriptor = None
    locked = False
    try:
        if directory:
            descriptor = os.open(Path(directory) / LOCK,
                                 os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            deadline = time.monotonic() + 0.2
            while True:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    locked = True
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        break
                    time.sleep(0.005)
    except OSError:
        pass
    try:
        yield locked
    finally:
        if descriptor is not None:
            os.close(descriptor)


def claim(directory: str) -> str:
    if not directory:
        return ""
    try:
        with (Path(directory) / CLAIM).open(encoding="ascii") as source:
            return source.read(64)
    except (OSError, UnicodeError):
        return ""


def cancel(directory: str | None = None) -> None:
    """Called under the render lock before another screen is cleared or drawn."""
    directory = runtime() if directory is None else directory
    if not directory:
        return
    try:
        write_private_json(Path(directory) / STATE, {"active": False})
    except OSError:
        pass


def activate(row: int, column: int, *, unicode: bool, styled: bool) -> None:
    """Publish a waiting indicator after its first frame is already on screen, under the lock."""
    directory = runtime()
    token = claim(directory) if directory else ""
    if not token:
        return
    try:
        write_private_json(Path(directory) / STATE, {
            "active": True, "claim": token, "row": row, "column": column,
            "unicode": unicode, "styled": styled,
        })
    except OSError:
        pass


def animate(directory: str, stream, owner: int | None = None) -> None:
    """Draw only the spinner, never read keystrokes, and exit when the launcher or claim dies."""
    owner = os.getppid() if owner is None else owner
    token = claim(directory)
    index = 0
    while token and os.getppid() == owner:
        with rendering(directory) as locked:
            if not locked:
                return
            if claim(directory) != token:
                return
            try:
                with (Path(directory) / STATE).open(encoding="utf-8") as source:
                    state = json.loads(source.read(4096))
                if state.get("active") and state.get("claim") == token:
                    row, column = state["row"], state["column"]
                    if not (type(row) is int and type(column) is int
                            and 0 <= row < 10000 and 0 <= column < 10000):
                        return
                    size = os.get_terminal_size(stream.fileno())
                    row = min(row, max(0, size.lines - 1))
                    column = min(column, max(0, size.columns - 1))
                    frames = UNICODE_FRAMES if state.get("unicode") else ASCII_FRAMES
                    attr = Caps(color=ANSI16 if state.get("styled") else NONE).color_pair("info")
                    stream.write(SYNC_ON + MOVE.format(row=row + 1, col=column + 1)
                                 + RESET + attr + frames[index % len(frames)] + RESET + SYNC_OFF)
                    stream.flush()
                    index += 1
            except FileNotFoundError:
                pass
            except (OSError, ValueError, TypeError, AttributeError, KeyError):
                return
        time.sleep(INTERVAL)
