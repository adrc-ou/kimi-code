"""Terminal ownership: what it takes to borrow a screen and hand it back intact.

The mode deliberately used here is cbreak rather than raw, and the reason is a guarantee about
Ctrl-C that outlives this file. ``tty.setcbreak`` clears ``ICANON`` and ``ECHO`` — which is all
an arrow-key interface needs — but leaves ``ISIG`` set, so Ctrl-C still arrives as ``SIGINT``.
Python turns that into ``KeyboardInterrupt``, which unwinds normally, which means the terminal is
restored on the way out and the process dies of the interrupt with status 130, exactly as the
line-buffered panel it replaces did. The launcher's ``trap cleanup EXIT`` then releases the lock
file and removes the resolved bootstrap.

Had this been ``tty.setraw``, ``ISIG`` would be off, Ctrl-C would be an ordinary byte, and the
only way to keep that promise would be to claim the signal — which the launcher's contract forbids.
So the mode is chosen by the cleanup contract, not by convenience.

Resize is polled with an ``ioctl`` on every wait rather than caught with ``SIGWINCH``, for the same
kind of reason: it cannot race the input loop, it needs no handler, and one syscall per keypress
is not something the user can feel.
"""

from __future__ import annotations

import fcntl
import os
import select
import struct
import sys
import termios
import time
import tty
from types import TracebackType

from .caps import TRUECOLOR, Caps, detect, truecolor_query, upgrade
from .cells import ALT_OFF, ALT_ON, HIDE_CURSOR, MOUSE_OFF, MOUSE_ON, RESET, SHOW_CURSOR
from .keys import Decoder, Key

#: How long to wait for the terminal to answer the queries in :meth:`Terminal.probe`, in seconds.
#: It is deliberately short: an unresponsive terminal must not cost a perceptible pause, and every
#: answer it could have given already has a conservative default in ``caps``.
PROBE_TIMEOUT = 0.05
#: How long a lone ``ESC`` is given to acquire the rest of a sequence before being read as Escape.
#: Arrow keys arrive in one packet, so this only ever delays the Escape key itself.
ESCAPE_TIMEOUT = 0.03
#: Ceiling on how much of a query reply to absorb. A real reply to both queries is well under this;
#: the limit exists so that a terminal sending something unexpected cannot fill the input buffer.
REPLY_LIMIT = 128

_TIOCGWINSZ = 0x5413
#: DECRQSS asking for the current window title (``OSC 0`` is reported as ``21``). A terminal that
#: does not implement it stays silent, and silence means the title is left alone rather than
#: overwritten with something we cannot undo.
TITLE_QUERY = b"\033P$q21t\033\\"
_TITLE_REPLY = b"\033P1r21;"
_HEXDIGIT = frozenset(b"0123456789abcdefABCDEF")


def _printable(value: str) -> bool:
    """Whether a decoded title could plausibly be a title, rather than the wrong bytes."""
    return bool(value) and not any(ord(c) < 32 or c == "\x7f" or c == "\ufffd" for c in value)


def _title_from_reply(response: bytes) -> str | None:
    """Recover the window title from a DECRQSS reply, if this terminal sent one.

    The reply is ``DCS 1 r 21 ; <title> t ST``. Anything else — a NACK, an unrelated ``DCS``, a
    terminal that was echoing our own colour query — yields ``None``, and the caller reads that as
    "do not touch the title", which is the only safe reading of silence.

    Terminals differ on whether the payload is the literal title or its hex encoding, and both are
    in the wild, so the shape decides and the result is checked: a title that happens to look like
    hex decodes into control bytes, fails that check, and is then taken at face value instead. An
    answer that satisfies neither reading is discarded rather than guessed at, because the cost of
    guessing wrong is writing garbage into the user's window title on the way out.
    """
    start = response.find(_TITLE_REPLY)
    if start < 0:
        return None
    rest = response[start + len(_TITLE_REPLY) :]
    end = rest.find(b"t")
    terminator = rest.find(b"\033\\")
    if end < 0 or (0 <= terminator < end):
        return None
    payload = rest[:end]
    literal = payload.decode("utf-8", "replace")
    if payload and len(payload) % 2 == 0 and all(c in _HEXDIGIT for c in payload):
        try:
            escaped = bytes.fromhex(payload.decode("ascii")).decode("utf-8", "replace")
        except ValueError:  # pragma: no cover - the predicate above rules this out
            return literal if _printable(literal) else None
        if _printable(escaped):
            return escaped
    return literal if _printable(literal) else None


def winsize(fd: int, fallback: tuple[int, int] = (80, 24)) -> tuple[int, int]:
    """Ask the kernel for the window's current size, since only the kernel knows."""
    try:
        packed = fcntl.ioctl(fd, _TIOCGWINSZ, bytearray(8))
    except OSError:
        return fallback
    try:
        rows, columns = struct.unpack("HH", bytes(packed)[:4])
    except struct.error:  # pragma: no cover - only a short ioctl result can do this
        return fallback
    return (columns or fallback[0], rows or fallback[1])


class Terminal:
    """The alternate screen, with the life cycle of the real one guaranteed around it."""

    def __init__(
        self,
        caps: Caps | None = None,
        *,
        stream=None,
        title: str = "",
        probe: bool = True,
    ) -> None:
        self.fd = sys.stdin.fileno() if hasattr(sys.stdin, "fileno") else -1
        self.out = stream if stream is not None else sys.stdout
        self.caps = caps if caps is not None else detect(self.out, probe=probe)
        #: Mouse reporting is a separate capability from colour and must not be inferred from it:
        #: a monochrome terminal still has a mouse, and a colour terminal may have none.
        self.mouse = self.caps.mouse
        self.title = title
        self.decoder = Decoder()
        self._saved = None
        self._entered = False
        self._previous_title: str | None = None
        self._title_set = False

    @property
    def interactive(self) -> bool:
        """Whether a screen may be borrowed at all.

        Callers already have a non-interactive path and must take it; running the modal on a pipe
        would block forever on input that will never come, which is a worse failure than the
        plain printout they were using before.
        """
        return self.fd >= 0 and os.isatty(self.fd)

    @property
    def size(self) -> tuple[int, int]:
        return (self.caps.columns, self.caps.rows)

    # -- lifecycle --------------------------------------------------------------------------

    def __enter__(self) -> Terminal:
        self._enter()
        return self

    def __exit__(
        self,
        kind: type[BaseException] | None,
        value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        self.leave()
        return False

    def _enter(self) -> None:
        if self._entered:
            return
        if not self.interactive:
            raise OSError("cannot take over a screen that is not a terminal")
        self._entered = True
        # The attributes are saved before they are changed, and everything after this point must
        # survive a failure, because a half-entered screen is still a borrowed one.
        self._saved = termios.tcgetattr(self.fd)
        # cbreak comes first: in canonical mode a query reply has no line terminator in it and would
        # sit unread in the line discipline, so the probe below could not see it.
        tty.setcbreak(self.fd)
        self.probe()
        self._sync()
        if self._title_allowed():
            self._write(self._title_escape(self.title))
            self._title_set = True
        self._write(ALT_ON + HIDE_CURSOR + (MOUSE_ON if self.mouse else ""))

    def leave(self) -> None:
        """Hand the terminal back; idempotent, since ``finally`` and ``exit`` both run it."""
        if not self._entered:
            return
        self._entered = False
        try:
            restore = ""
            if self._title_set:
                # Only ever set when the previous title is known, so this cannot blank a title.
                restore = self._title_escape(self._previous_title or "")
            self._write(
                RESET + (MOUSE_OFF if self.mouse else "") + ALT_OFF + SHOW_CURSOR + restore + "\r\n"
            )
        finally:
            self._title_set = False
            if self._saved is not None:
                termios.tcsetattr(self.fd, termios.TCSADRAIN, self._saved)
                self._saved = None

    def _title_allowed(self) -> bool:
        """Whether the window title may be rewritten, and undone afterwards.

        All three have to hold: the terminal is of a family where ``OSC 2`` is reliable, the step
        actually has a label to put there, and the read-back in :meth:`probe` recovered what to put
        back. The in-window title is drawn either way, so declining costs a nicety and not the
        information.
        """
        return bool(self.caps.titles and self.title and self._previous_title is not None)

    @staticmethod
    def _clean(value: str) -> str:
        """Strip the control characters that would break out of a title string."""
        return "".join(c for c in value if ord(c) >= 32 and c != "\x7f")

    @classmethod
    def _title_escape(cls, value: str) -> str:
        return "\033]2;" + cls._clean(value) + "\007"

    def _write(self, payload: str) -> bool:
        try:
            self.out.write(payload)
            self.out.flush()
        except (OSError, ValueError):
            # A closed or broken stdout is not a reason to leave the user staring at the alt
            # screen; the restore in leave() still runs and the caller still sees its exit status.
            return False
        return True

    # -- capability queries -----------------------------------------------------------------

    def probe(self) -> None:
        """Ask the terminal two questions in one round trip, before anything is drawn.

        ``COLORTERM`` does not survive ``sudo`` or ``ssh``, so an interactive program can do better
        than read the environment; and the window title cannot be restored afterwards unless it is
        read beforehand. Both answers are opportunistic — a terminal that stays silent keeps the
        conservative tier ``caps`` already chose, and loses its title to no one — which is why one
        short timeout covers both rather than two that would stack.

        The replies also *should* be drained before the alternate screen opens, because a late
        reply landing mid-keystroke would otherwise be parsed as input.
        """
        queries = bytearray()
        if self.caps.probe and self.caps.color != TRUECOLOR:
            queries += truecolor_query()
        if self.caps.titles and self.title:
            queries += TITLE_QUERY
        if not queries or self.fd < 0:
            return
        # The truecolor query works by *setting* an improbable colour, so it has to be unset again
        # on the same trip: the terminal evaluates the request in order, but the user's main screen
        # outlives us and would otherwise keep a magenta background we asked for.
        if not self._write(bytes(queries).decode("ascii", "ignore") + RESET):
            return
        response = self._read_reply()
        if not response:
            return
        if self.caps.probe:
            self.caps = upgrade(self.caps, response)
        self._previous_title = _title_from_reply(response)

    def _read_reply(self) -> bytes:
        """Collect whatever the terminal answers, bounded in bytes and in time alike.

        The byte cap is what stops a terminal that dumps its status line back at us from being
        mistaken for input, and the time cap is a hard stop rather than an idle gap, so a terminal
        that dribbles cannot extend the pause indefinitely.
        """
        collected = bytearray()
        expires = time.monotonic() + PROBE_TIMEOUT
        while len(collected) < REPLY_LIMIT:
            remaining = expires - time.monotonic()
            if remaining <= 0:
                break
            try:
                ready = select.select([self.fd], [], [], remaining)[0]
                if not ready:
                    break
                chunk = os.read(self.fd, REPLY_LIMIT - len(collected))
            except OSError:
                break
            if not chunk:
                break
            collected += chunk
        return bytes(collected)

    # -- geometry ---------------------------------------------------------------------------

    def _sync(self) -> bool:
        """Re-read the window size, which is how a resize is noticed without a signal."""
        columns, rows = winsize(self.fd, (self.caps.columns, self.caps.rows))
        if (columns, rows) == (self.caps.columns, self.caps.rows):
            return False
        self.caps = Caps(
            color=self.caps.color,
            unicode=self.caps.unicode,
            ambiguous_wide=self.caps.ambiguous_wide,
            theme=self.caps.theme,
            titles=self.caps.titles,
            mouse=self.caps.mouse,
            columns=columns,
            rows=rows,
            probe=False,
        )
        return True

    def resized(self) -> bool:
        """True when the window changed size since the last check."""
        return self._sync()

    def write(self, payload: str) -> None:
        self._write(payload)

    def keys(self, timeout: float | None = None) -> list[Key]:
        """Block for input, resolving a held ``ESC`` into the Escape key when none follows.

        ``timeout`` is for callers with something to poll; the modal loop passes nothing and is
        woken by the user, which is what keeps an idle screen at zero CPU.
        """
        if self.decoder.waiting:
            if not select.select([self.fd], [], [], ESCAPE_TIMEOUT)[0]:
                return self.decoder.flush()
        elif timeout is not None and not select.select([self.fd], [], [], timeout)[0]:
            return []
        try:
            data = os.read(self.fd, 1024)
        except OSError:
            return []
        return self.decoder.feed(data)
