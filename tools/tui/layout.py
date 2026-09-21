"""Window- and content-aware frame: where things go, and what is currently off-screen.

Two rules govern this module, both borrowed from interfaces people already know.

The first is that the body's height is **derived, never stored**. A frame is a function of the
current window and the current chrome, so a resize cannot leave a stale number behind, and the code
that clips content has exactly one place to be wrong. htop does the same by recomputing the cursor
row from ``panel->y`` and the panel height on every movement (`Panel.c:83-85`) rather than caching
a position.

The second is that scrolling is **cursor slack, not page quantisation**: moving one row past the
edge of the viewport moves the viewport by one row, so the list creeps and the user never loses
their place. That is fzf's ``--scroll-off`` contract (`fzf.man:722-723`). The opposite — snapping a
whole page — is disorienting in a short list, which is all this launcher has.

Everything here is pure arithmetic over the cell model in ``cells``. Nothing in this module writes
to a terminal, which is what makes the scroll clamping testable without a pty.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from .caps import Caps
from .cells import Screen

#: Lines of context kept above and below the cursor as it passes the edge of the viewport. One
#: line is enough to see where you came from; more wastes rows in a short window.
SCROLL_SLACK = 1
#: Columns at the body's right edge reserved for the scrollbar track, and at its left for the
#: focus gutter. Both are reserved whether or not anything is currently using them, so neither a
#: resize nor a focus move can reflow the content.
SCROLLBAR_WIDTH = 1
GUTTER_WIDTH = 2
#: The fewest body rows worth drawing. Below this the chrome starts giving way.
MIN_BODY = 2


@dataclass(frozen=True)
class Rect:
    """A rectangle of cells in screen coordinates, half-open on the bottom and right edges."""

    top: int = 0
    left: int = 0
    height: int = 0
    width: int = 0

    @property
    def bottom(self) -> int:
        return self.top + self.height

    @property
    def right(self) -> int:
        return self.left + self.width

    def shrink(self, *, top: int = 0, bottom: int = 0, left: int = 0, right: int = 0) -> Rect:
        return Rect(
            self.top + top,
            self.left + left,
            max(0, self.height - top - bottom),
            max(0, self.width - left - right),
        )

    def contains(self, row: int, column: int) -> bool:
        return self.top <= row < self.bottom and self.left <= column < self.right

    @property
    def blank(self) -> bool:
        return self.height <= 0 or self.width <= 0


#: Border glyph tables. The ASCII tier is a data swap rather than a second code path, which is how
#: a terminal without UTF-8 stays a one-line change instead of a fork in every drawing routine.
BOX_LIGHT = {
    "h": "─",
    "v": "│",
    "tl": "┌",
    "tr": "┐",
    "bl": "└",
    "br": "┘",
    "tee_l": "├",
    "tee_r": "┤",
}
BOX_ASCII = {
    "h": "-",
    "v": "|",
    "tl": "+",
    "tr": "+",
    "bl": "+",
    "br": "+",
    "tee_l": "+",
    "tee_r": "+",
}


def box_glyphs(caps: Caps) -> dict[str, str]:
    return BOX_LIGHT if caps.unicode else BOX_ASCII


def ellipsis(caps: Caps) -> str:
    """The clipping marker, in the glyph register this terminal can count.

    U+2026 is East Asian *Neutral*, so unlike the Ambiguous box-drawing and geometric symbols it
    cannot be mistaken for two cells, and it stays safe in every locale this launcher starts in.
    """
    return "…" if caps.unicode else "..."


# --------------------------------------------------------------------------------------------
# lines and rows
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Segment:
    """Styled text — the unit a step renders with.

    Attributes are carried per segment rather than per line so a row can mix a bold label, a dim
    note and a coloured value without the caller managing escape codes at all.
    """

    text: str
    attr: str = ""


class Line:
    """One row of content, as a sequence of styled segments.

    Accepts bare strings and ``(text, attr)`` pairs as well as ``Segment``\\ s, because a step that
    wrote ``Segment("Cost")`` for every literal would be a step whose rendering nobody reads.
    """

    __slots__ = ("segments",)

    def __init__(self, *parts: object) -> None:
        segments: list[Segment] = []
        for part in parts:
            segments.extend(_as_segments(part))
        self.segments: tuple[Segment, ...] = tuple(segments)

    def __add__(self, other: Line) -> Line:
        joined = Line()
        joined.segments = self.segments + other.segments
        return joined

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Line) and other.segments == self.segments

    def __hash__(self) -> int:
        return hash(self.segments)

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return f"Line({self.text()!r})"

    def text(self) -> str:
        return "".join(segment.text for segment in self.segments)

    def width(self, caps: Caps) -> int:
        return sum(caps.width(segment.text) for segment in self.segments)


BLANK = Line()


def _as_segments(part: object) -> list[Segment]:
    # Structural checks, not ``isinstance``. The engine is importable both as ``tui`` — the way a
    # tool under ``tools/`` sees it — and as ``tools.tui``, the way a test importing from the
    # repository root does, and those are two distinct sets of classes holding one interpreter.
    # A row built through one identity and painted through the other is still a row, and an
    # ``isinstance`` test would have it fall through to the scalar case and be drawn as its repr.
    if isinstance(part, Segment) or (hasattr(part, "text") and hasattr(part, "attr")):
        return [part if isinstance(part, Segment) else Segment(part.text, part.attr)]
    if isinstance(part, Line) or hasattr(part, "segments"):
        return list(part.segments)
    if isinstance(part, str):
        return [Segment(part)]
    # Any iterable of parts, which is what a caller assembling a line out of several lines hands
    # over — a generator among them. Restricting this to the two concrete sequence types made
    # ``Line(*(x.segments for x in parts))`` fall through to the scalar case below and paint the
    # repr of a generator, which a renderer must never do silently.
    if isinstance(part, (bytes, bytearray)):
        return [Segment(part.decode("utf-8", "replace"))]
    if isinstance(part, Iterable):
        flat: list[Segment] = []
        for item in part:
            flat.extend(_as_segments(item))
        return flat
    return [Segment(str(part))]


def styled(text: str, attr: str = "") -> Line:
    return Line(Segment(text, attr))


def spaced(count: int) -> Line:
    return Line(Segment(" " * max(0, count)))


def columns_line(parts: list[tuple[Line, int]], caps: Caps) -> Line:
    """Lay parts out in fixed columns, clipping each to its own width.

    Column arithmetic is width-aware for the same reason everything else here is: a label in a CJK
    locale is twice as wide in cells as it is in characters, and a table that counted characters
    would put its right-hand columns out of alignment on exactly the rows it meant to emphasise.
    """
    out = Line()
    for index, (value, width) in enumerate(parts):
        piece = fit(value, width, caps)
        if index:
            piece = Line(Segment(" "), *piece.segments)
        padding = width - piece.width(caps)
        if padding > 0:
            piece = Line(*piece.segments, Segment(" " * padding))
        out = out + piece
    return out


def padded(value: Line, width: int, caps: Caps, *, right: bool = False) -> Line:
    """Pad or clip ``value`` to exactly ``width`` columns."""
    piece = fit(value, width, caps)
    room = width - piece.width(caps)
    if room <= 0:
        return piece
    spaces = Segment(" " * room)
    return Line(spaces, *piece.segments) if right else Line(*piece.segments, spaces)


def fit(value: Line, room: int, caps: Caps) -> Line:
    """Clip ``value`` to ``room`` columns, marking the cut.

    Truncation with an explicit marker is the only honest rendering: an option label that was
    silently chopped reads as a shorter label, whereas one ending in ``…`` reads as clipped. The
    marker costs a column, and the cut is made so the total still fits.
    """
    if room <= 0:
        return BLANK
    if value.width(caps) <= room:
        return value
    mark = ellipsis(caps)
    budget = max(0, room - caps.width(mark))
    kept: list[Segment] = []
    used = 0
    for segment in value.segments:
        for character in segment.text:
            taken = caps.width(character)
            if used + taken > budget:
                return Line(*kept, Segment(mark, segment.attr))
            if kept and kept[-1].attr == segment.attr:
                kept[-1] = Segment(kept[-1].text + character, segment.attr)
            else:
                kept.append(Segment(character, segment.attr))
            used += taken
    return Line(*kept)


def cut(value: str, room: int, caps: Caps) -> str:
    """Take the leading characters of ``value`` that fit in ``room`` columns."""
    kept = []
    used = 0
    for character in value:
        taken = caps.width(character)
        if used + taken > room:
            break
        kept.append(character)
        used += taken
    return "".join(kept)


def wrap(value: str, room: int, caps: Caps) -> list[str]:
    """Break ``value`` into lines that each fit ``room`` columns, at a space where one exists.

    Body rows are drawn one per terminal row and clipped rather than flowed, so a sentence the
    frame cannot fit has to be cut up before it becomes rows. A word wider than the room keeps its
    own line whole: half a URL is useless in a way that a wrapped paragraph never is, and the row
    is still clipped at the edge rather than chopped mid-token here.

    Never returns an empty list, because a caller drawing heading rows has nothing to do with "no
    lines" and a blank row is the honest answer for blank text.
    """
    words = value.split()
    if not words:
        return [""]
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}" if current else word
        if current and caps.width(candidate) > room:
            lines.append(current)
            current = word
        else:
            current = candidate
    lines.append(current)
    return lines


@dataclass(frozen=True)
class Row:
    """One body row: what to draw, what it means, and whether the cursor can rest on it.

    Headings, rules and blank separators are rows too, with ``focusable`` off. That is deliberate:
    a step composes one vertical list rather than a list plus a side channel for decoration, so the
    viewport arithmetic has a single thing to reason about, and a heading can never scroll away from
    the content belonging to it.
    """

    line: Line = BLANK
    #: Whatever the step wants back when this row is chosen; ``None`` on a non-selectable row.
    target: object = None
    focusable: bool = False
    #: A row-wide attribute, which is how a dimmed superseded block and a highlighted focused block
    #: are the same mechanism applied twice.
    attr: str = ""
    #: Whether the focus wash spans the row's width. Tree rows turn this off so the wash stops at
    #: the content instead of underlining a diagram.
    wash: bool = True

    @classmethod
    def heading(cls, value: Line, attr: str = "") -> Row:
        return cls(line=value, attr=attr)

    @classmethod
    def gap(cls, value: Line | None = None) -> Row:
        return cls(line=value or BLANK)

    @classmethod
    def item(cls, value: Line, target: object, attr: str = "", *, wash: bool = True) -> Row:
        return cls(line=value, target=target, focusable=True, attr=attr, wash=wash)


def focusable_rows(rows: list[Row]) -> list[int]:
    """Indices, into ``rows``, of the ones the cursor may rest on.

    Focus is an index into this list rather than into the body, so a step can add or remove a
    heading without every cursor position in the file shifting by one.
    """
    return [index for index, row in enumerate(rows) if row.focusable]


# --------------------------------------------------------------------------------------------
# viewport
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Window:
    """Which slice of a list is visible, and what that implies about the rest of it.

    ``above`` and ``below`` are rows hidden off each end. They are not decoration: the frame turns
    them into an ``N more above`` / ``N more below`` line, because a scrollbar alone does not tell
    anyone that content exists — and the panel this replaces has neither.
    """

    first: int
    last: int
    above: int
    below: int

    @property
    def rows(self) -> int:
        return max(0, self.last - self.first)

    @property
    def scrolling(self) -> bool:
        return bool(self.above or self.below)

    def row_of(self, index: int) -> int:
        """Body-relative row of a list index, or ``-1`` when it is off-screen."""
        return index - self.first if self.first <= index < self.last else -1


def scroll_for(
    total: int,
    height: int,
    selected: int,
    previous: int = 0,
    slack: int = SCROLL_SLACK,
) -> int:
    """The top visible index after moving to ``selected``, given it was at ``previous``.

    Three constraints, in this order:

    * never scroll past the last page, so a short final page stays bottom-aligned instead of
      jumping to the top — htop clamps ``scroll <= size - height`` for exactly this reason
      (`Panel.c:268-280`);
    * never scroll above zero;
    * move the *minimum* number of rows that restores ``slack`` lines of context on the side the
      cursor left. That is what makes one arrow press shift one row rather than a screenful.
    """
    if total <= 0 or height <= 0:
        return 0
    # The usable slack cannot exceed what the viewport has to give: demanding two lines of context
    # in a three-row window would leave one row for the cursor and turn every move into a scroll.
    context = min(slack, max(0, (height - 1) // 2))
    ceiling = max(0, total - height)
    top = max(0, min(previous, ceiling))
    if selected < 0:
        return top
    index = max(0, min(selected, total - 1))
    if index - top < context:
        top = index - context
    elif top + height - 1 - index < context:
        top = index - (height - 1 - context)
    return max(0, min(top, ceiling))


def visible_window(total: int, height: int, scroll: int) -> Window:
    """The half-open slice of indices a scroll position shows, plus what it hides."""
    if total <= 0 or height <= 0:
        return Window(0, 0, 0, max(0, total))
    first = max(0, min(scroll, max(0, total - height)))
    last = min(total, first + height)
    return Window(first, last, first, total - last)


def thumb(window: Window, total: int, height: int) -> tuple[int, int]:
    """Start and end rows of the scrollbar thumb within a ``height``-row track.

    The thumb is proportional to the visible fraction but never smaller than one row, because a
    scrollbar that renders nothing is worse than one that is a cell too long.
    """
    if total <= 0 or height <= 0:
        return (0, 0)
    visible = max(1, window.rows)
    if visible >= total:
        return (0, height)
    size = min(height, max(1, round(height * visible / total)))
    span = height - size
    progress = window.first / max(1, total - visible)
    start = min(span, max(0, round(progress * span)))
    return (start, start + size)


def page(total: int, height: int, direction: int, selected: int) -> int:
    """The index to focus after a page movement.

    A page leaves one row of context behind so the user can see where they came from, which is what
    makes ``PgDn`` on a long list feel like turning a page rather than being teleported.
    """
    if total <= 0:
        return 0
    step = max(1, height - 1)
    return max(0, min(selected + direction * step, total - 1))


# --------------------------------------------------------------------------------------------
# frame
# --------------------------------------------------------------------------------------------

#: The chrome regions, top to bottom. ``rule`` rows are separators that belong to the region below
#: them and disappear with it.
CHROME = ("title", "rail", "status", "footer")


@dataclass(frozen=True)
class Frame:
    """The regions of one screen, derived from the window size and the chrome a step needs.

    Chrome order is fixed — title, step rail, body, status, footer — and rows are given up from the
    middle outward when the window is too short to hold all of it. The body is the last thing to
    lose height, because it is the only part carrying the answer, and the footer outlives the status
    line because the legend is what makes the keys discoverable at all.
    """

    screen: Rect = Rect()
    title: Rect = Rect()
    rail: Rect = Rect()
    body: Rect = Rect()
    status: Rect = Rect()
    footer: Rect = Rect()
    rule_top: Rect = Rect()
    rule_bottom: Rect = Rect()
    #: Which chrome survived a short window, so the painter skips the rest.
    shown: frozenset[str] = frozenset()

    def has(self, name: str) -> bool:
        return name in self.shown

    @property
    def rows(self) -> int:
        return self.screen.height

    @property
    def columns(self) -> int:
        return self.screen.width


#: Chrome given up first when the window is short. The footer is absent because it is the legend,
#: and a key the user cannot discover is a key that does not exist.
_GIVE_UP = ("status", "rail", "title")

#: How much list an extra legend row has to leave behind to be worth taking. The hint bar wraps to
#: two rows and then to three only when a window this size would still show a screenful of the
#: answer, because wrapping it is a convenience — the overlay under ``?`` always carries the whole
#: table — so it is given up before the rail and before the status line, and never out of the body.
COMFORT_BODY = 8


def layout(columns: int, rows: int, *, footer_rows: int = 1, rail: bool = True) -> Frame:
    """Divide a ``columns`` x ``rows`` window into its regions.

    ``footer_rows`` is how many rows the legend needs, which only the caller knows, because it
    depends on the bindings a step actually answers rather than on the window. It is clamped here
    so the footer can never eat the body: two legend rows are a luxury on a four-row window.

    ``rail`` is whether the step rail has anything to say, which is likewise the caller's question.
    A row that would be blank is not chrome, and the body is short enough without it.
    """
    columns = max(20, columns)
    rows = max(4, rows)
    footer_rows = max(1, min(footer_rows, rows - 3))
    shown = set(CHROME)
    if not rail:
        shown.discard("rail")

    def chrome_rows(kept: set[str]) -> int:
        total = len(kept) + footer_rows - 1
        if kept & {"title", "rail"}:
            total += 1  # the rule under the header block
        if "status" in kept:
            total += 1  # the rule above it
        return total

    # The extra legend row is given up before any chrome is, because a wrapped hint is the one
    # piece of the footer the ``?`` overlay already carries in full. It is only worth a row of
    # list while the list still has five rows to show in.
    while footer_rows > 1 and rows - chrome_rows(shown) < COMFORT_BODY:
        footer_rows -= 1

    while rows - chrome_rows(shown) < MIN_BODY:
        for name in _GIVE_UP:
            if name in shown:
                shown.discard(name)
                break
        else:
            break

    cursor = 0
    title = rail = status = Rect()
    if "title" in shown:
        title = Rect(cursor, 0, 1, columns)
        cursor += 1
    if "rail" in shown:
        rail = Rect(cursor, 0, 1, columns)
        cursor += 1
    rule_top = Rect(cursor, 0, 1, columns)
    cursor += 1
    footer = Rect(rows - footer_rows, 0, footer_rows, columns)
    rule_bottom = Rect()
    if "status" in shown:
        status = Rect(footer.top - 1, 0, 1, columns)
        rule_bottom = Rect(footer.top - 2, 0, 1, columns)
        body_end = rule_bottom.top
    else:
        body_end = footer.top
    body = Rect(cursor, 0, max(1, body_end - cursor), columns).shrink(
        left=GUTTER_WIDTH, right=SCROLLBAR_WIDTH
    )
    return Frame(
        screen=Rect(0, 0, rows, columns),
        title=title,
        rail=rail,
        body=body,
        status=status,
        footer=footer,
        rule_top=rule_top,
        rule_bottom=rule_bottom,
        shown=frozenset(shown | {"body"}),
    )


def paint_rule(screen: Screen, area: Rect, caps: Caps, attr: str = "") -> None:
    """A horizontal separator across the full width."""
    if area.blank or not (0 <= area.top < screen.rows):
        return
    glyph = box_glyphs(caps)["h"]
    screen.text(area.top, 0, glyph * screen.columns, attr or caps.color_pair("rule"))


def paint_line(
    screen: Screen,
    row: int,
    area: Rect,
    value: Line,
    caps: Caps,
    *,
    attr: str = "",
    fill: bool = False,
) -> None:
    """Draw one line inside ``area``, clipped to its width.

    ``fill`` paints the row's whole width with ``attr`` first. That is how a focus wash extends past
    the end of the label: a highlight stopping at the last character reads as a cursor, and one
    spanning the row reads as a selection.
    """
    if not (0 <= row < screen.rows):
        return
    if fill and attr:
        screen.pad(row, area.left, area.width, attr)
    column = area.left
    for segment in fit(value, area.width, caps).segments:
        if column >= area.right:
            break
        screen.text(row, column, segment.text, segment.attr or attr)
        column += caps.width(segment.text)


def paint_rows(
    screen: Screen,
    area: Rect,
    rows: list[Row],
    window: Window,
    caps: Caps,
    *,
    focused: int = -1,
    focus_attr: str = "",
) -> None:
    """Draw the visible slice of ``rows``, one per body row.

    Non-focused rows keep their own attribute and nothing else: the indicator lives in the reserved
    gutter, so moving the cursor repaints two cells and a wash rather than reflowing the list.
    """
    for offset in range(window.rows):
        index = window.first + offset
        if index >= len(rows):
            break
        row = rows[index]
        at = area.top + offset
        if at >= area.bottom:
            break
        attr = focus_attr if index == focused else row.attr
        if index == focused:
            screen.text(at, area.left - GUTTER_WIDTH, pointer(caps), focus_attr)
            if row.wash:
                screen.pad(at, area.left, area.width, focus_attr)
        paint_line(
            screen, at, area, row.line, caps, attr=attr, fill=bool(attr) and index == focused
        )


def pointer(caps: Caps) -> str:
    """The focus marker, in a gutter that is reserved whether or not anything is focused here.

    U+258C is fzf's default ``--pointer`` *and* its ``--gutter`` glyph (`fzf.man:738,744`), which is
    why it reads as familiar rather than invented; ``>`` is its own ASCII fallback.
    """
    return "▌" if caps.unicode else ">"


def scrollbar(screen: Screen, area: Rect, window: Window, total: int, caps: Caps) -> None:
    """Draw the track and thumb in the column reserved at the body's right edge.

    Nothing is drawn when everything is visible: a full-height thumb is a control with no meaning,
    and the column is worth more to the content. fzf's contract for the same space is a
    one-character track (`fzf.man:759-766`).
    """
    if not window.scrolling:
        return
    track = box_glyphs(caps)["v"] if caps.unicode else ":"
    start, end = thumb(window, total, area.height)
    rule = caps.color_pair("rule")
    column = area.right
    for offset in range(area.height):
        row = area.top + offset
        if not (0 <= row < screen.rows):
            continue
        filled = "█" if caps.unicode else "#"
        mark = filled if start <= offset < end else track
        screen.text(row, column, mark, rule)


def overflow_note(count: int, direction: str, caps: Caps, attr: str = "") -> Line:
    """``2 more below`` — the discoverability half of scrolling.

    The count is the point. A scrollbar shows *that* there is more, and a half-visible row shows
    *that* it continues, but neither says how much is left, and how much is what decides whether to
    keep pressing the key.
    """
    if direction == "above":
        word, mark = "above", ("↑" if caps.unicode else "^")
    else:
        word, mark = "below", ("↓" if caps.unicode else "v")
    # ``more`` is not the counted noun — the rows are — so it never takes a plural. ``2 more
    # below`` reads as a direction; ``2 mores below`` reads as a typo.
    return Line(Segment(f" {mark} {count} more {word}", attr or caps.color_pair("rule")))
