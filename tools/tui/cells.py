"""The cell buffer and the diff that writes it.

Nothing here clears the screen. The previous frame is retained, the new one is composed into a
second buffer, and only cells whose character or attribute actually changed are emitted, coalesced
into runs and wrapped in a synchronous-update pair so the terminal never shows a half-applied
frame. That is what keeps a fullscreen interface feeling instant: the cost of a keystroke is
proportional to what moved, not to the size of the window, so a resize and a cursor step are both
cheap and neither ever flashes.
"""

from __future__ import annotations

from dataclasses import dataclass

from .caps import Caps

#: How many already-correct cells a run steps over instead of emitting a cursor jump for. A jump
#: costs about ten bytes, so a short gap is cheaper to rewrite than to skip — and keeping a run
#: contiguous is what lets anything downstream match on the text a row actually displays.
BRIDGE = 12

#: Hide the cursor while we draw, and park it out of the way of the diff.
CLEAR = "\033[2J\033[H"
RESET = "\033[0m"
SYNC_ON = "\033[?2026h"
SYNC_OFF = "\033[?2026l"
MOVE = "\033[{row};{col}H"
HIDE_CURSOR = "\033[?25l"
SHOW_CURSOR = "\033[?25h"
ALT_ON = "\033[?1049h"
ALT_OFF = "\033[?1049l"
MOUSE_ON = "\033[?1000h\033[?1006h"
MOUSE_OFF = "\033[?1000l\033[?1006l"


@dataclass(frozen=True, slots=True)
class Cell:
    """One character and the attribute that draws it.

    A cell compares by both fields, which is what makes a background wash repaint when only the
    colour changed and the character stayed a space.
    """

    char: str = " "
    attr: str = ""


class Screen:
    """A fixed grid of cells, with an exact view of what changed since the last paint."""

    def __init__(self, caps: Caps, rows: int, columns: int) -> None:
        self.caps = caps
        self.rows = rows
        self.columns = columns
        self.blank = Cell()
        self._front = [[self.blank] * columns for _ in range(rows)]
        self._back = [[self.blank] * columns for _ in range(rows)]

    def resize(self, rows: int, columns: int) -> bool:
        """Resize and force a full repaint, returning whether anything moved."""
        if (rows, columns) == (self.rows, self.columns):
            return False
        self.rows, self.columns = rows, columns
        self._front = [[self.blank] * columns for _ in range(rows)]
        self._back = [[self.blank] * columns for _ in range(rows)]
        self.clear()
        return True

    def clear(self) -> None:
        """Erase the *front* buffer only, which makes every next diff a full repaint."""
        self._front = [[self.blank] * self.columns for _ in range(self.rows)]

    # -- drawing into the back buffer -------------------------------------------------------

    def text(self, row: int, column: int, value: str, attr: str = "") -> int:
        """Write ``value`` at a position, returning the column after it.

        Text that would cross the right edge is clipped rather than wrapped. A wrapped row in a
        fixed layout does not reflow into the next pane, it overdraws it, and an ellipsis is the
        only honest rendering of content that does not fit.
        """
        if not (0 <= row < self.rows):
            return column
        cursor = column
        for character in value:
            if cursor >= self.columns:
                break
            if character != "\n":
                self._back[row][cursor] = Cell(character, attr)
            cursor += 1
        return cursor

    def pad(self, row: int, column: int, count: int, attr: str = "") -> None:
        """Fill a span with spaces carrying ``attr``, so a background wash survives the diff.

        The attribute has to be written into the blank cells: a diff that only compares characters
        would otherwise leave a highlighted row's background ending where its text ended.
        """
        if not (0 <= row < self.rows):
            return
        for offset in range(count):
            at = column + offset
            if 0 <= at < self.columns:
                self._back[row][at] = Cell(" ", attr)

    def box(
        self,
        top: int,
        left: int,
        bottom: int,
        right: int,
        edges: dict[str, str],
        attr: str = "",
    ) -> None:
        """Draw a border from a glyph table, so the ASCII tier is a table swap, not a code path."""
        vertical = edges.get("v", "|")
        horizontal = edges.get("h", "-")
        for column in range(left + 1, right):
            self.text(top, column, horizontal, attr)
            self.text(bottom, column, horizontal, attr)
        for row in range(top + 1, bottom):
            self.text(row, left, vertical, attr)
            self.text(row, right, vertical, attr)
        self.text(top, left, edges.get("tl", "+"), attr)
        self.text(top, right, edges.get("tr", "+"), attr)
        self.text(bottom, left, edges.get("bl", "+"), attr)
        self.text(bottom, right, edges.get("br", "+"), attr)

    # -- painting ---------------------------------------------------------------------------

    def snapshot(self) -> list[str]:
        """The last painted frame as plain text, one entry per row.

        Reading the grid rather than parsing the escape sequences is the only way to assert what a
        row *shows*, and the front buffer is exactly that once ``paint()`` has walked it.
        """
        return ["".join(cell.char for cell in row).rstrip() for row in self._front]

    def paint(self) -> str:
        """Emit the minimal sequence that turns the terminal into the back buffer.

        The front buffer is brought in line as the diff walks it, so no second comparison pass is
        needed and the two buffers cannot drift apart.

        A short gap of unchanged cells is *carried over* rather than closed: a cursor-address
        escape costs about as many bytes as the handful of cells it would skip, and breaking the
        run at every one of them would split a label like ``[ ] Beta`` across several escapes.
        Long gaps still jump, because repainting a wide blank to avoid one escape is the worse
        trade.
        """
        chunks: list[str] = []
        # An empty attribute is the null state, so returning to it must be explicit: the terminal
        # remembers the last SGR it was given, and a cell we skip would otherwise inherit whatever
        # colour was painted to its left.
        pending_attr: str | None = None
        start: tuple[int, int] | None = None
        run: list[str] = []
        carried = 0

        def flush() -> None:
            nonlocal pending_attr, start, run, carried
            if start is not None and carried:
                # Carried cells are already correct on screen, so a run that ends inside a gap
                # gives them back rather than rewriting them.
                del run[len(run) - carried :]
                carried = 0
            if start is not None and run:
                chunks.append(MOVE.format(row=start[0] + 1, col=start[1] + 1))
                chunks.append(pending_attr or RESET)
                chunks.append("".join(run))
                chunks.append(RESET)
            pending_attr, start, run, carried = None, None, [], 0

        for row in range(self.rows):
            front, back = self._front[row], self._back[row]
            for column in range(self.columns):
                cell, was = back[column], front[column]
                if cell == was:
                    if start is not None and carried < BRIDGE and cell.attr == pending_attr:
                        # Carry the cell over so the run stays contiguous; it is already correct
                        # on screen, so this only rewrites what the jump would have skipped.
                        carried += 1
                        run.append(cell.char)
                        continue
                    flush()
                    continue
                carried = 0
                if pending_attr is not None and cell.attr != pending_attr:
                    flush()
                if start is None:
                    start = (row, column)
                pending_attr = cell.attr
                run.append(cell.char)
                front[column] = cell
            flush()
        self._back = [[self.blank] * self.columns for _ in range(self.rows)]
        if not chunks:
            return ""
        # Synchronous update is ignored by terminals that do not implement it, so wrapping every
        # frame costs nothing and removes the only way a partial repaint could be seen.
        return SYNC_ON + "".join(chunks) + SYNC_OFF
