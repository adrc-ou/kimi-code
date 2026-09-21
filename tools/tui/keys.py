"""Key decoding and the binding table that the footer is generated from.

The table is the interface's only statement of what a key does. The footer legend, the ``?``
overlay, the mouse-to-key translation and the dispatch itself are all read out of it, so a
hint can never describe a binding that was removed — which is precisely how the panel this
replaces ended up documenting keys it no longer answered to.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Named keys arrive as CSI or SS3 sequences; a bare ESC is its own key and must not be mistaken
# for the start of a sequence that never comes.
_CSI = re.compile(rb"\x1b\[([0-?]*)([ -/]*)([@-~])")
_SS3 = re.compile(rb"\x1bO([@-P])")
_OSC = re.compile(rb"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")

#: ``CSI <button>;<col>;<row>M/m`` from SGR mouse reporting.
_MOUSE = re.compile(rb"\x1b\[<(\d+);(\d+);(\d+)([MQm])")

_CSI_KEYS = {
    "A": "Up",
    "B": "Down",
    "C": "Right",
    "D": "Left",
    "E": "Begin",
    "F": "End",
    "H": "Home",
    "L": "Insert",
    "P": "F1",
    "Q": "F2",
    "R": "F3",
    "S": "F4",
}

#: Tilde-keyed function and navigation keys, including the modified forms xterm sends.
_TILDE_KEYS = {
    "1": "Home",
    "2": "Insert",
    "3": "Delete",
    "4": "End",
    "5": "PgUp",
    "6": "PgDn",
    "7": "Home",
    "8": "End",
    "11": "F1",
    "12": "F2",
    "13": "F3",
    "14": "F4",
    "15": "F5",
    "17": "F6",
    "18": "F7",
    "19": "F8",
    "20": "F9",
    "21": "F10",
    "23": "F11",
    "24": "F12",
}

#: Names as they are printed in the legend. A key never appears on screen with a spelling the
#: user did not press, so this table is also the documentation.
DISPLAY = {
    "Up": "↑",
    "Down": "↓",
    "Left": "←",
    "Right": "→",
    "PgUp": "<PgUp>",
    "PgDn": "<PgDn>",
    "Home": "<Home>",
    "End": "<End>",
    "Enter": "<Enter>",
    # Spelled out rather than abbreviated, unlike the rest of the named keys: no surveyed tool has a
    # back affordance, so this is the one name the user has to *read* before using it. It is also
    # labelled ``delete`` on a Mac keyboard, which an abbreviation cannot survive.
    "Backspace": "<Backspace>",
    "Delete": "<Del>",
    "Space": "Space",
    "Escape": "<Esc>",
    "Tab": "<Tab>",
    "BackTab": "<S-Tab>",
}


def display(name: str) -> str:
    """How one key or chord is printed, using ``<...>`` for named keys.

    Angle brackets are lazygit's notation and every surveyed tool that spells out a key name has
    converged on something like it: ``<`` is both a binding and a character, and only delimiters
    keep the two from being read as each other.
    """
    if name in DISPLAY:
        return DISPLAY[name]
    if name.startswith("Ctrl-") or name.startswith("Alt-"):
        return "<" + name.replace("-", "+") + ">"
    return name


_NAMED = {
    "\r": "Enter",
    "\n": "Enter",
    "\x1b\r": "Enter",
    "\x08": "Backspace",
    "\x7f": "Backspace",
    "\t": "Tab",
    "\x1b[Z": "BackTab",
    "\x1b": "Escape",
    " ": "Space",
    "\x03": "Ctrl-C",
    "\x04": "Ctrl-D",
    "\x1b[1;5A": "Ctrl-Up",
    "\x1b[1;5B": "Ctrl-Down",
    "\x1b[1;2A": "Shift-Up",
    "\x1b[1;2B": "Shift-Down",
}


@dataclass(frozen=True)
class Key:
    """One decoded keypress: a canonical name, and the printable character if it had one."""

    name: str
    char: str = ""

    def __str__(self) -> str:  # pragma: no cover - convenience for test failure messages
        return self.name


def _control(byte: int) -> str:
    """Name a control character as ``Ctrl-<letter>``, which is what the user actually presses."""
    if byte == 0:
        return "Ctrl-@"
    letter = chr(byte + 64) if byte < 32 else chr(byte - 64)
    return f"Ctrl-{letter}"


class Decoder:
    """Turns a byte stream into keypresses without guessing at partial sequences.

    Two real hazards are handled here. Terminals over Windows SSH deliver ``CR CR`` where one was
    typed, which :meth:`feed` folds back into one. And a lone ``ESC`` is the Escape key on most
    terminals but the first byte of an arrow sequence on all of them, so a decoder must wait for
    bytes that may never arrive — a bounded wait, because a terminal that does not support a key
    should not stall the interface. That is what :attr:`waiting` and :meth:`flush` are for.
    """

    def __init__(self) -> None:
        self._buffer = bytearray()
        self._pending_text = ""

    def feed(self, data: bytes) -> list[Key]:
        """Decode everything the new bytes complete, holding back an incomplete sequence.

        Two ``Enter``\\ s out of one packet are treated as one. Windows OpenSSH is the reason: it
        sends ``CR CR`` for a single Return, and a step-on-Enter interface would otherwise skip
        every step it walked through. A repeat that arrives in a *later* packet is a real repeat,
        which is why the comparison is against the previous key from this same call and not against
        a timestamp.
        """
        if not data:
            return []
        self._buffer.extend(data)
        keys: list[Key] = []
        while self._buffer:
            consumed = self._one(keys)
            if not consumed:
                break
            del self._buffer[:consumed]
        return [
            key
            for index, key in enumerate(keys)
            if not (key.name == "Enter" and index and keys[index - 1].name == "Enter")
        ]

    def _one(self, keys: list[Key]) -> int:
        buffer = self._buffer
        first = buffer[0]

        mouse = _MOUSE.match(bytes(buffer))
        if mouse:
            keys.append(_mouse(mouse))
            return mouse.end()

        osc = _OSC.match(bytes(buffer))
        if osc:
            return osc.end()  # a response to our own query; consumed silently

        if first == 0x1B and len(buffer) == 1:
            return 0  # could be the start of anything; wait for a neighbour

        if first == 0x1B:
            return self._escape(keys)

        if first < 0x20 or first == 0x7F:
            text = bytes(buffer[:1]).decode("latin-1")
            if text in _NAMED:
                keys.append(Key(_NAMED[text]))
            else:
                keys.append(Key(_control(first)))
            return 1

        # A printable character, possibly multi-byte UTF-8. Decode only as much as is complete.
        for size in range(1, min(4, len(buffer)) + 1):
            try:
                text = bytes(buffer[:size]).decode("utf-8")
            except UnicodeDecodeError:
                continue
            if text in _NAMED:
                keys.append(Key(_NAMED[text]))
            else:
                keys.append(Key(text, text))
            return size
        return 0  # a truncated multi-byte character; wait for the rest

    def _escape(self, keys: list[Key]) -> int:
        buffer = bytes(self._buffer)
        csi = _CSI.match(buffer)
        if csi:
            final = csi.group(3).decode("ascii")
            parameter = csi.group(1).decode("ascii")
            modifier = csi.group(2).decode("ascii")
            name = _csi_name(final, parameter, modifier)
            if name:
                keys.append(Key(name))
                return csi.end()
            return csi.end()  # an unrecognised sequence: consume it rather than mis-dispatch it
        ss3 = _SS3.match(buffer)
        if ss3:
            final = ss3.group(1).decode("ascii")
            keys.append(Key(_CSI_KEYS.get(final, "Escape")))
            return ss3.end()
        if buffer[1:2] == b"\r":
            keys.append(Key("Enter"))
            return 2
        if len(buffer) == 1:
            return 0
        keys.append(Key("Escape"))
        return 1

    def flush(self) -> list[Key]:
        """Name whatever is held back, for the bounded wait that decides ESC was really ESC."""
        keys: list[Key] = []
        if self._buffer:
            held = bytes(self._buffer)
            self._buffer.clear()
            if held == b"\x1b":
                keys.append(Key("Escape"))
            else:
                keys.extend(self.feed(held))
        return keys

    @property
    def waiting(self) -> bool:
        """True when held bytes could still become a longer sequence."""
        return bool(self._buffer)


def _csi_name(final: str, parameter: str, modifier: str) -> str:
    if final in ("A", "B", "C", "D", "E", "F", "H", "L", "P", "Q", "R", "S") and not modifier:
        base = _CSI_KEYS[final]
        return _modified(base, parameter) if ";" in parameter else base
    if final == "Z":
        return "BackTab"
    if final == "M" or final == "m":
        return ""
    if final == "~":
        return _TILDE_KEYS.get(parameter.split(";")[0], "")
    return ""


def _modified(base: str, parameter: str) -> str:
    """Fold ``CSI 1;<modifier>A`` into ``Ctrl-Up`` and friends, or plain ``Up`` if unmodified."""
    parts = parameter.split(";")
    if len(parts) < 2:
        return base
    try:
        code = int(parts[1]) - 1
    except ValueError:
        return base
    names = {
        0x02: "Shift-",
        0x04: "Alt-",
        0x08: "Ctrl-",
        0x0A: "Ctrl-Shift-",
    }
    for bit in (0x0A, 0x08, 0x04, 0x02):
        if code & bit == bit:
            return names[bit] + base
    return base


def _mouse(match) -> Key:
    button, column, row, pressed = int(match[1]), int(match[2]), int(match[3]), match[4]
    if button == 64:
        return Wheel(name="WheelUp", column=column, row=row)
    if button == 65:
        return Wheel(name="WheelDown", column=column, row=row)
    if button & 0x40:  # the release half of a click we did not ask to distinguish
        return Key("MouseRelease")
    return Click(
        name="Click",
        column=column,
        row=row,
        right=bool(button & 0x03),
        dragged=pressed == b"M",
    )


@dataclass(frozen=True)
class Wheel(Key):
    """A wheel event, which carries a position so a focused pane can be the one that scrolls."""

    column: int = 0
    row: int = 0


@dataclass(frozen=True)
class Click(Key):
    """A button press, kept as a position because the footer is hit-tested by column."""

    column: int = 0
    row: int = 0
    right: bool = False
    dragged: bool = False


@dataclass(frozen=True)
class Binding:
    """One row of the keymap, and simultaneously one row of the footer legend.

    ``keys`` holds every spelling of the action, and exactly one is printed: the first. That is
    how ``<PgUp>`` and ``K`` can both page while the legend stays short enough to read.
    """

    action: str
    description: str
    keys: tuple[str, ...]
    #: A binding may be conditional — ``Back`` is meaningless on the first step. The predicate is
    #: passed the step's state, and an unavailable action leaves the legend instead of ignoring
    #: a keypress with no explanation.
    when: object = field(default=None, repr=False)

    def available(self, state) -> bool:
        return True if self.when is None else bool(self.when(state))

    @property
    def label(self) -> str:
        return display(self.keys[0])


def bind(action: str, description: str, *keys: str, when=None) -> Binding:
    return Binding(action=action, description=description, keys=tuple(keys), when=when)


def lookup(bindings: tuple[Binding, ...], key: Key, state) -> str:
    """The action for one keypress, or ``""`` when nothing answers it."""
    for binding in bindings:
        if not binding.available(state):
            continue
        if key.name in binding.keys:
            return binding.action
    return ""


def legend_lines(
    bindings: tuple[Binding, ...],
    state,
    caps,
    *,
    width: int | None = None,
    rows: int = 1,
    ellipsis: str | None = None,
) -> list[str]:
    """Assemble the footer across at most ``rows`` lines, marking whatever it still had to drop.

    Wrapping comes before truncating. An item is never split and a line is never left empty, so a
    narrow window gets one item per row and the decisive keys still appear in the first row —
    ``navigation()`` orders them that way precisely so that the budget runs out on the scrolling
    hints rather than on ``Space`` and ``Enter``. The ellipsis is the last resort, and it is
    honest: what it gives up is the conventional tail of the table, and a step that answers ``?``
    still lists everything in the overlay. A field step has no overlay to fall back on, because it
    gave ``?`` up to the value being typed, which is why that ordering matters most there.

    ``width`` is the region the legend is drawn into rather than the whole window, so a frame that
    gave up its chrome rows to a short window still fits the columns it actually has.
    """
    room = caps.columns if width is None else max(1, width)
    mark = "…" if caps.unicode and ellipsis is None else ellipsis or "..."
    parts = [
        f"{binding.label} {binding.description}"
        for binding in bindings
        if binding.available(state) and binding.description
    ]
    separator = " · "
    lines: list[list[str]] = []
    remaining = list(parts)
    for _ in range(max(1, rows)):
        current: list[str] = []
        while remaining:
            candidate = separator.join([*current, remaining[0]])
            # The first item of a row always goes in, however wide it is, so a row is never empty
            # and the fill loop can never stall; the painter clips it.
            if current and caps.width(candidate) > room:
                break
            current.append(remaining.pop(0))
        lines.append(current)
        if not remaining:
            break
    if remaining:
        last = lines[-1]
        text = separator.join(last)
        # The separator in front of the mark costs as much as the mark, and on a narrow window that
        # is the difference between fitting one more hint and fitting none. An ellipsis trailing a
        # line needs no space before it to still read as an ellipsis.
        if caps.width(text + mark) > room:
            while last and caps.width(separator.join([*last, mark])) > room:
                last.pop()
            text = separator.join(last)
        lines[-1] = [text + mark]
    return [separator.join(current) for current in lines if current]


def legend(
    bindings: tuple[Binding, ...],
    state,
    caps,
    *,
    width: int | None = None,
    ellipsis: str | None = None,
) -> str:
    """The one-line form of :func:`legend_lines`, for a caller with exactly one row.

    Empty rather than missing when the step answers nothing with a legend entry, which is what a
    caller with no bindings at all should get.
    """
    lines = legend_lines(bindings, state, caps, width=width, rows=1, ellipsis=ellipsis)
    return lines[0] if lines else ""
