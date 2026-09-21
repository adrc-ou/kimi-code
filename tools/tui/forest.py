"""The tree-shaped surface: a diagram the operator can edit.

The launcher's context step is not a list. It is a set of documents, each composed out of the ones
under it, some of which are overridden by others, and each carrying a switch — which is exactly a
tree, and was being shown as two flat columns of text joined by nothing. ``menu`` cannot draw that
and ``input`` does not want to, so this module holds the third shape, and the drawing work that goes
with it stays here rather than in the step that knows what a context block is.

Every row answers two independent questions and the mark column says both at once, the way the
kernel's configuration browser does: the character *inside* the mark is the value, and the
characters *around* it are whether anyone can change it. ``scripts/kconfig/mconf.c`` says it
in one line — ``if (sym_is_changeable(sym)) item_make("[%c]", ...); else
item_make("-%c-", ...)`` — and this file follows it, so a person who has ever configured a
kernel already knows how to read this screen. ``[x]`` and ``[ ]`` are yours to move with
``Space``; ``-x-`` and ``- -`` are facts about the workspace that no key here alters. One
column, one grammar, and no right-hand word pretending to be a control.

Structure and state stay separate axes drawn by separate means. Structure is the guide, the
``▶`` in the joint where one source feeds more than one parent, and a magenta connector where
a document is substituted into another by template rather than appended after it. State is the
mark and the colour: a superseded row is dim *and* has a hollow value, so nothing rides on hue
alone, which neither ``NO_COLOR`` nor a colour-blind operator can be required to discount.

The sentence that explains a row does not live under it. A diagram the operator has to wade through
is a paragraph with indentation, so each row carries one :attr:`Node.detail` string instead,
drawn by :meth:`ForestStep.detail` in the pane beside the tree, and only the row under the cursor
is spelled out at a time. That is the ``menuconfig`` arrangement and it is why the tree can hold
nine add-ons, two file blocks and three price groups without leaving the screen: the map is for
orientation and the pane is for reading. Where the window is too narrow for a pane, the frame puts
the same lines in the band under the status row, so the prose is never *only* behind a keypress and
never stacked under every row at once either.

The tail columns are measured over the whole tree rather than over the visible slice, so neither
scrolling nor cycling a switch can slide a number sideways, and a figure the operator was comparing
two rows ago is still in the same column.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field, replace

from . import layout as L
from .app import Result, Step
from .layout import BLANK, Line, Row, Segment

#: Nothing to change here and nothing to report: a heading, or a source its parent answers for.
PLAIN = "plain"
#: Two states, drawn as a checkbox. The first state in ``states`` is the marked one.
CHECK = "check"
#: A row whose value is real but is not the operator's to move: ``-x-`` when it holds, ``- -``
#: when it does not, whether because the file is absent or because another source supersedes it.
#: The delimiter is the whole statement about changeability, so a file that is present and
#: superseded still appears in the map — deleting it would make the directory and the picture
#: disagree — without competing for the cursor, and why it contributes nothing rides on the row's
#: :attr:`Node.detail` rather than on a second glyph nobody has to learn.
FIXED = "fixed"

#: Cells a mark and its gutter occupy, so every label in a branch starts in one column.
MARK_WIDTH = 4
#: Columns one guide level occupies: the tee or the rail, the dash, and its space.
RAIL_WIDTH = 3
#: Columns the fan-out marker occupies beside a root that composes into more than one parent. A
#: root has no joint to set a glyph in, so unlike every deeper row it buys its own column — and
#: pays for it on every row in the tree, because two roots starting at different columns read as
#: two unrelated lists rather than as one map with a branch in it.
GUIDE_WIDTH = 2
#: The fewest label columns worth keeping before the numeric tail starts giving way. Below this a
#: figure attached to half a name identifies nothing, so the tail is dropped instead of the label.
MIN_LABEL = 12
#: The floor on the gap between a label and its first right-hand column, shared with the plain
#: panel so a wrapped label never lands against a number in either renderer.
COLUMN_GAP = 3
#: The gap between two tail columns.
FIELD_GAP = 2
#: The two characters the mark column puts between delimiters. ``x`` is the value every vocabulary
#: here agrees means "in use", because ``[x]`` is the one mark nobody has to learn.
ON_MARK = "x"
OFF_MARK = " "

#: A state's colour when the caller has no opinion: in use, not in use, undecided.
TONE_FOR = {"on": "active", "off": "dim", "auto": "info"}
#: Roles that are strong enough to be worth carrying onto the label as well as the state.
_HEAVY = ("dim", "over", "warn")


@dataclass(frozen=True)
class Node:
    """One row of the tree: its name, its answer, its figures, and what hangs below it."""

    #: Stable identity and the key in the answer map. A heading may leave it empty, which is also
    #: how it opts out of the cursor: a row with no answer should not absorb keystrokes.
    id: str
    label: str
    #: The states ``Space`` moves through, wrapping. Empty means the row takes no input at all.
    states: tuple[str, ...] = ()
    #: :data:`CHECK`, :data:`FIXED` or :data:`PLAIN` — how the mark column is spelled.
    kind: str = PLAIN
    #: The state this row opens with, for as long as nobody has answered it.
    default: str = ""
    #: The right-hand figures: tokens, and the share of the lane's cap they spend.
    value: str = ""
    pct: str = ""
    #: The one sentence that says why this row reads the way it does. Drawn in the pane beside the
    #: tree while there is one, and under the row while there is not; never both, and never for a
    #: row nobody is looking at. This is where every paragraph the old screen stacked in the body
    #: went, and moving them here is what let the map become a map.
    detail: str = ""
    #: For a mark, this is its value: ``False`` hollows it and dims the row. For a row that asks
    #: for input it also keeps the cursor off it, so a source that is present but overridden
    #: belongs to the picture without looking selectable.
    enabled: bool = True
    #: This source is composed into more than one parent, so the joint opens into a branch.
    branch: bool = False
    #: The edge to the parent is a template substitution rather than an appended block.
    link: bool = False
    #: Colour role for the label. Left empty, the row's state decides it.
    tone: str = ""
    children: tuple[Node, ...] = ()

    def __post_init__(self) -> None:
        """Refuse a row whose answer has no honest mark.

        A checkbox says two states, and a row that is nobody's to move says one thing: whether it
        holds. A row that asked for a state while carrying the other would put a switch on screen
        whose position nobody can read — the exact class of thing this redesign exists to remove —
        so the pairing is checked while the tree is being built rather than explained in a legend
        afterwards.
        """
        if self.states and self.kind == PLAIN:
            raise ValueError(f"{self.id}: {PLAIN!r} draws no mark, so it cannot carry a switch")
        if self.kind == CHECK and len(self.states) != 2:
            raise ValueError(f"{self.id}: a checkbox has exactly two states, got {self.states}")
        if self.kind == FIXED and self.states:
            raise ValueError(
                f"{self.id}: {FIXED!r} draws a value no key moves, so it takes no states"
            )

    @property
    def editable(self) -> bool:
        """Whether the cursor may rest here, which is the same question as whether ``Space`` means a
        thing — so a row that cannot be changed can never be focused and then refuse a key."""
        return bool(self.states) and self.enabled

    @property
    def checked(self) -> bool:
        """Whether this row's default is the state its checkbox marks."""
        return bool(self.states) and self.default == self.states[0]


def walk(
    nodes: Sequence[Node], rails: tuple[bool, ...] = (), depth: int = 0
) -> Iterator[tuple[Node, tuple[bool, ...], int, bool]]:
    """``(node, rails, depth, last)`` for every row, in display order.

    A rail says that the ancestor at that level still has siblings below it, which is all a renderer
    needs to know where a vertical line stops. A root has no joint above it to connect to, so its
    children begin at column zero and the rails start one level later — otherwise every diagram here
    would hang itself three columns right of nothing.
    """
    total = len(nodes)
    for index, node in enumerate(nodes):
        last = index == total - 1
        yield node, rails, depth, last
        if node.children:
            yield from walk(node.children, (*rails, not last) if depth else (), depth + 1)


def next_state(node: Node, value: str) -> str:
    """The state ``Space`` lands on, wrapping at the end.

    Wrapping is right for a control with no meaningful order and wrong for a cursor, which is why
    the engine's own movement does not wrap and this does.
    """
    if not node.states:
        return value
    try:
        at = node.states.index(value)
    except ValueError:
        at = 0
    return node.states[(at + 1) % len(node.states)]


@dataclass(frozen=True)
class ForestState:
    """Cursor position and the whole answer map.

    ``values`` carries every row's current state, editable or not, so a caller reads one mapping out
    of the result and never has to ask which rows were the interactive ones. The opening map and the
    opening cursor both live on the step, which is what makes ``Ctrl-R`` an exact undo rather than a
    partial one. The viewport is not here at all: the loop owns scrolling, and a second copy of it
    in a step's state is a second answer to the same question.
    """

    focus: int = 0
    values: dict[str, str] = field(default_factory=dict)


class ForestStep(Step):
    """A titled tree of :class:`Node` rows that answers with a state map."""

    def __init__(
        self,
        *,
        title: str,
        nodes: Sequence[Node],
        rail: str = "",
        head: Sequence[str] = (),
        opening: dict[str, str] | None = None,
        tone_of: Callable[[Node, str], str] = lambda node, value: "",
        reset_note: str = "options reset",
        rules: Sequence[str] = (),
    ) -> None:
        """
        ``head`` is the one line above the tree that a reader needs before the tree means anything —
        which of the answers on screen are the operator's own and which are defaults nobody has
        agreed to yet. Everything longer than a line belongs in :attr:`Node.detail` or in ``rules``.

        ``opening`` is the answer the user is revisiting. A row it does not name falls back to its
        own :attr:`Node.default`, the same precedence :class:`~.menu.ListStep` uses, so the two
        surfaces cannot disagree about what "remembered" means.

        ``tone_of`` colours a row's state. The surface knows that there are two marks and the
        launcher knows what they mean, and this is where those two knowledges have to meet.

        ``rules`` are the sentences that are true of every row rather than of one — the reading of a
        figure, the definition of an empty file. They go to the key overlay, which is the one place
        a sentence about the whole step has room, and they are listed there beside the keys because
        a rule nobody can find is a rule nobody follows.
        """
        self.title = title
        self.rail = rail or title
        self.nodes = tuple(nodes)
        self.head = tuple(head)
        self.rules = tuple(rules)
        self.reset_note = reset_note
        self._opening = dict(opening or {})
        self._tone_of = tone_of

    # The two shape questions a tree asks of its own roots are derived rather than cached, because a
    # step that rebuilds its nodes from the operator's answer (:class:`~.prompt_panel.ContextStep`)
    # would otherwise be measured against the tree it displayed a keystroke ago.

    @property
    def _roots_are_checkable(self) -> bool:
        """Whether any root is a control, which is what makes every root pay for the mark column."""
        return any(node.kind in (CHECK, FIXED) for node in self.nodes)

    @property
    def _roots_are_branching(self) -> bool:
        """Whether any root fans out, which is what makes every row pay for the marker column."""
        return any(node.branch for node in self.nodes)

    # -- geometry ---------------------------------------------------------------------------

    @property
    def pane_room(self) -> int:
        """Columns the detail pane gives to text, or zero when the frame has no pane."""
        if self.frame is None or self.frame.detail.blank:
            return 0
        return self.frame.detail.width

    @property
    def detail_room(self) -> int:
        """Columns to wrap the focused row's prose in: the pane's, or the screen's.

        A frame with no pane still has somewhere to say what a row means — the help bar under the
        list, which is full width and is :mod:`menuconfig`'s own answer at a width where a
        side-by-side pane would starve the tree. Prose is never *only* behind a keypress, and it is
        never stacked under every row at once either; this is the one place that choice is made.
        """
        if self.frame is None:
            return self.room
        return self.pane_room or self.frame.columns

    @property
    def room(self) -> int:
        """Columns a body row has, with the pane's share taken off the right edge.

        A tree measured against the full body width while a pane covers part of it does not fail
        loudly — it paints its numbers underneath the prose, which reads as a rendering bug in the
        prose. The subtraction happens here, once, so every measurement downstream is honest.
        """
        base = super().room
        if self.frame is None or self.pane_room <= 0:
            return base
        return max(MIN_LABEL, base - self.pane_room - COLUMN_GAP)

    # -- measuring --------------------------------------------------------------------------

    def rows_in_order(self) -> tuple[tuple[Node, tuple[bool, ...], int, bool], ...]:
        return tuple(walk(self.nodes))

    def options(self) -> tuple[Node, ...]:
        """The rows the cursor may rest on, in the order it meets them."""
        return tuple(node for node, _, _, _ in self.rows_in_order() if node.editable)

    def option(self, state: ForestState) -> Node | None:
        options = self.options()
        if not options:
            return None
        return options[min(max(state.focus, 0), len(options) - 1)]

    def tail_widths(self) -> tuple[int, int]:
        """Widths of the figure and percentage columns.

        Measured over the whole tree rather than over the visible slice, so switching a block off —
        which recomposes the prices under it — cannot move a number that was two rows ago.
        """
        rows = self.rows_in_order()
        value = max((self.cells(node.value) for node, _, _, _ in rows), default=0)
        pct = max((self.cells(node.pct) for node, _, _, _ in rows), default=0)
        return (value, pct)

    def fit_tail(self, widths: tuple[int, int]) -> tuple[int, int]:
        """Shrink the tail until even the deepest row has a label worth reading.

        The decision is made once against the deepest row rather than per row: a tail that appeared
        beside a short label and vanished beside a long one would put the same figure in a different
        column two rows apart, which is the one thing a column of numbers is on screen for.
        """
        widest = max(
            (self.left(node, depth) for node, _, depth, _ in self.rows_in_order()), default=0
        )
        value, pct = widths
        while self._tail_cells(value, pct) and (
            self.room - widest - COLUMN_GAP - self._tail_cells(value, pct) < MIN_LABEL
        ):
            value, pct = _drop(value, pct)
        return (value, pct)

    def left(self, node: Node, depth: int) -> int:
        """Columns the guide and the mark spend before a label may start.

        Every row below a root pays for the mark even when it has no checkbox, so a source, a
        switch and a heading in one branch share a single label edge. A root pays for it too as soon
        as any root has one, because a tree whose sections start at two different columns reads as
        two trees. The same argument covers the root-level fan-out marker, which is why its column
        is reserved whether or not this particular root branches.
        """
        return depth * RAIL_WIDTH + self.guide_room + self.mark_room(node, depth)

    @property
    def guide_room(self) -> int:
        """Columns the root-level branch marker spends, or none when no root branches."""
        return GUIDE_WIDTH if self._roots_are_branching else 0

    def mark_room(self, node: Node, depth: int) -> int:
        """Whether this row's label sits behind a mark column, and how wide that column is."""
        if depth or node.kind in (CHECK, FIXED) or self._roots_are_checkable:
            return MARK_WIDTH
        return 0

    def _tail_cells(self, value: int, pct: int) -> int:
        """Columns the tail spends, gaps included, with no leading gap on the first live column."""
        out = 0
        if value:
            out += value
        if pct:
            out += pct + (FIELD_GAP if value else 0)
        return out

    def _cut(self, text: str, room: int) -> str:
        if self.caps is not None:
            return L.cut(text, room, self.caps)
        return text if len(text) <= room else text[: max(0, room)]

    def _clip_label(self, text: str, room: int) -> str:
        """A label the row cannot hold is marked as clipped, never chopped in silence.

        ``main system promp`` reads as a shorter name than ``main system prompt``, and the two are
        different rows to a user scanning the tree. The mark costs a column and says the rest is
        somewhere else — which it is, twice over: in the status line, and in the pane.
        """
        if self.caps is None or self.cells(text) <= room:
            return self._cut(text, room)
        mark = L.ellipsis(self.caps)
        spent = self.cells(mark)
        if room <= spent:
            return self._cut(text, room)
        return f"{self._cut(text, room - spent)}{mark}"

    def _wrapped(self, text: str, room: int) -> list[str]:
        """Prose broken to fit, and every piece clipped to fit.

        Wrapping alone is not enough: a word longer than the column keeps its own line rather than
        being hyphenated mid-letter, and a line longer than the body is the overflow this whole
        surface is supposed to prevent.
        """
        if self.caps is not None and room > 0:
            return [self._cut(piece, room) for piece in L.wrap(text, room, self.caps)]
        return [text] if room <= 0 else [self._cut(text, room)]

    # -- opening ----------------------------------------------------------------------------

    def value_for(self, node: Node) -> str:
        named = self._opening.get(node.id)
        if named:
            return named
        if node.default:
            return node.default
        return node.states[0] if node.states else ""

    def initial(self) -> ForestState:
        """Every row at the answer it opens with, cursor at the top of the tree.

        Deliberately *not* landed on a row away from its default, as a list does: a diagram this
        shape usually opens with half a dozen answers away from their defaults at once, so there is
        no single row that "is" the answer, and choosing one would hide the cursor somewhere the
        user did not look for it.
        """
        values = {node.id: self.value_for(node) for node, _, _, _ in self.rows_in_order()}
        return ForestState(values=values)

    def reset(self, state: object) -> ForestState:
        """The opening map *and* the opening cursor, which is what makes the key an undo."""
        del state
        return self.initial()

    # -- rendering --------------------------------------------------------------------------

    def rows(self, state: object) -> list[Row]:
        assert isinstance(state, ForestState)
        out: list[Row] = []
        lines: list[tuple[str, str]] = [(text, "dim") for text in self.head]
        lines += [(text, "warn") for text in self.alerts(state)]
        for text, role in lines:
            for piece in self._wrapped(text, self.room):
                out.append(Row.heading(Line(Segment(piece, self.tone(role)))))
        if lines:
            out.append(Row.gap())
        widths = self.fit_tail(self.tail_widths())
        # A root is a section, and four sections drawn in one grammar read as one long list. The
        # blank row and the weight are what tell them apart: a glyph in the mark column would be a
        # fifth spelling in a vocabulary whose whole job is saying "this is a switch".
        roots = 0
        for node, rails, depth, last in self.rows_in_order():
            if depth == 0:
                roots += 1
                if roots > 1:
                    out.append(Row.gap())
            out.extend(self._rows(node, rails, depth, last, state, *widths))
        return out

    def _rows(
        self,
        node: Node,
        rails: tuple[bool, ...],
        depth: int,
        last: bool,
        state: ForestState,
        value: int,
        pct: int,
    ) -> list[Row]:
        columns = self._columns(node, value, pct)
        if self._is_prose(node, columns):
            return self._prose(node, rails, depth, last, state)
        return [self._row(node, self._line(node, rails, depth, last, state, value, pct))]

    def _is_prose(self, node: Node, columns: tuple[int, int]) -> bool:
        """Whether a row is a sentence rather than a control.

        Only a plain row with no figure to align may take the whole width: a switch, a fixed mark,
        and even a disabled checkbox all print a mark that the label edge is measured against, and
        wrapping a row that has one moves the mark off the column every other row shares. Prose is
        the heading and the source name — the rows that carry nothing but words.
        """
        return node.kind == PLAIN and not any(columns)

    def _columns(self, node: Node, value: int, pct: int) -> tuple[int, int]:
        """The tail this row really has, out of the columns the tree fitted.

        The columns are shared so figures line up, and they stay shared among the rows that carry
        figures — a number that moved a column between two rows is the failure this whole layout
        exists to avoid. What is *not* reserved is tail a row can never print: reserving it anyway
        is what cut a section title off mid-word in a window with room for the whole of it.
        """
        if not node.value and not node.pct:
            return (0, 0)
        return (value, pct)

    def _prose(
        self, node: Node, rails: tuple[bool, ...], depth: int, last: bool, state: ForestState
    ) -> list[Row]:
        """A row with neither a switch nor a figure takes the whole width, and wraps to get it.

        Section titles are how a tall tree is navigated by eye, and every step has them. Everything
        the tree says has to reach the screen, which is the argument for scrolling instead of
        clipping, and the same argument covers a heading that runs past one row.
        """
        guide = self._guide(rails, depth, last, node.branch)
        mark = " " * MARK_WIDTH if self.mark_room(node, depth) else ""
        edge = self.cells(guide) + len(mark)
        room = max(1, self.room - edge)
        pieces = self._wrapped(node.label, room)
        tone = self._label_tone(node, state, depth)
        parts: list[Segment] = []
        if guide:
            parts.append(Segment(guide, self.tone("link") if node.link else self.tone("rule")))
        if mark:
            parts.append(Segment(mark))
        parts.append(Segment(pieces[0], tone))
        out = [self._row(node, Line(*parts))]
        out.extend(
            self._row(node, Line(Segment(f"{' ' * edge}{piece}", tone))) for piece in pieces[1:]
        )
        return out

    def _row(self, node: Node, line: Line) -> Row:
        line = self._bounded(line)
        if not node.editable:
            return Row.heading(line, self.tone("dim") if not node.enabled else "")
        return Row.item(line, node.id, wash=False)

    def _bounded(self, line: Line) -> Line:
        """No row costs more columns than the body has, however deep its guide runs.

        Below about a dozen columns the rails and the mark column of a third-level row are wider on
        their own than the room they are drawn in. The frame would clip that at the edge anyway,
        which cuts the row mid-glyph and tells the reader nothing, so the cut happens here instead,
        where it carries the same marker every other clip in this surface pays for.
        """
        if self.caps is None:
            return line
        return L.fit(line, self.room, self.caps)

    def _line(
        self,
        node: Node,
        rails: tuple[bool, ...],
        depth: int,
        last: bool,
        state: ForestState,
        value: int,
        pct: int,
    ) -> Line:
        parts: list[Segment] = []
        guide = self._guide(rails, depth, last, node.branch)
        if guide:
            parts.append(Segment(guide, self.tone("link") if node.link else self.tone("rule")))
        box = self._box(node, state)
        parts.append(Segment(f"{box} " if box else " " * MARK_WIDTH, self._mark_tone(node, state)))
        columns = self._columns(node, value, pct)
        label = self._clip_label(node.label, self._label_room(node, depth, *columns))
        parts.append(Segment(label, self._label_tone(node, state, depth)))
        parts.extend(self._tail(node, depth, state, *columns, label))
        return Line(*parts)

    def _label_room(self, node: Node, depth: int, *tail: int) -> int:
        give = self.left(node, depth) + self._tail_cells(*tail)
        return max(1, self.room - give - COLUMN_GAP)

    def _guide(self, rails: tuple[bool, ...], depth: int, last: bool, branch: bool) -> str:
        """The rails of the ancestors, then this row's joint.

        ``▶`` is set *in* the joint rather than beside it, so a fan-out costs no extra column and
        still reads at a glance; the ASCII tier gets ``>`` for the same reason it gets ``+`` for a
        tee. A root has no joint to carry it, so a branching root gets a column of its own and every
        other row in the tree gets that column blank — :meth:`left` charges for it either way, which
        is the only way the labels stay in one edge.
        """
        glyph = L.box_glyphs(self.caps) if self.caps is not None else L.BOX_LIGHT
        dash = ">" if glyph is L.BOX_ASCII else "▶" if branch else glyph["h"]
        if not depth:
            if not self._roots_are_branching:
                return ""
            return f"{dash} " if branch else " " * GUIDE_WIDTH
        rail = "".join(f"{glyph['v']}  " if keep else "   " for keep in rails)
        joint = glyph["bl"] if last else glyph["tee_l"]
        return f"{' ' * self.guide_room}{rail}{joint}{dash} "

    def _box(self, node: Node, state: ForestState) -> str:
        """The mark column: the value inside, the changeability around it. Always three cells.

        ``[-]`` is the mark :mod:`~.menu` already spends on a row the cursor cannot reach, so a
        switch with nothing to switch reads as *inert* rather than merely unchecked — the difference
        between "this is off" and "this is off and no key here changes it". ``-x-`` and ``- -`` are
        the same fact about a row that was never a switch: a file on disk, priced or superseded.
        """
        if node.kind == FIXED:
            return f"-{ON_MARK if node.enabled else OFF_MARK}-"
        if not node.enabled:
            return "[-]"
        return (
            f"[{ON_MARK if state.values.get(node.id, '') == node.states[0] else OFF_MARK}]"
            if node.kind == CHECK
            else "   "
        )

    def _mark_tone(self, node: Node, state: ForestState) -> str:
        """The colour of a mark, which follows its value and never a hue of its own.

        Green for a row that holds — the file that is being read, the add-on that is on — and dim
        for one that does not. A fixed row's ``-x-`` is the same claim as a checkbox's ``[x]``, so
        it is the same colour, and the two spellings stay distinguishable by their delimiters
        rather than by being coloured differently for different reasons.
        """
        if not node.enabled:
            return self.tone("dim")
        holds = state.values.get(node.id, "") == node.states[0] if node.states else True
        return self.tone("active") if holds else ""

    def _label_tone(self, node: Node, state: ForestState, depth: int = 0) -> str:
        """The label's colour, which is how a superseded chain goes grey before a word is read.

        Only a state heavy enough to matter carries onto the label, and a forced override takes the
        override colour rather than green, so two different reasons for "not the default" never
        arrive in the same glyph. A root is weighted even when it carries no state at all, because
        the six sections of this map are drawn in one grammar and the reader still has to find their
        edges while scrolling.
        """
        if node.tone:
            return self.tone(node.tone)
        if not node.enabled:
            return self.tone("dim")
        role = self._state_role(node, state)
        if role in _HEAVY:
            return self.tone(role)
        return self.tone("title") if not depth else ""

    def _state_role(self, node: Node, state: ForestState) -> str:
        value = state.values.get(node.id, "")
        return self._tone_of(node, value) or TONE_FOR.get(value, "")

    def _tail(
        self,
        node: Node,
        depth: int,
        state: ForestState,
        value: int,
        pct: int,
        label: str,
    ) -> list[Segment]:
        used = self.left(node, depth) + self.cells(label) + self._tail_cells(value, pct)
        # The gap belongs to the columns, not to the row: padding the edge of an empty tail would
        # widen the row for nothing, and a row the frame has to clip is a row that looks broken.
        if not any((value, pct)):
            return []
        out = [Segment(" " * max(COLUMN_GAP, self.room - used))]
        if value:
            out.append(Segment(self._cut(node.value, value).rjust(value)))
        if pct:
            if value:
                out.append(Segment(" " * FIELD_GAP))
            out.append(Segment(self._cut(node.pct, pct).rjust(pct), self.tone("dim")))
        return out

    # -- status and pane --------------------------------------------------------------------

    def status(self, state: object) -> Line:
        """The focused row in full, because its label is the first thing a narrow window clips.

        The state rides with it, coloured as it is in the tree, so the one line the frame reserves
        for "what is under the cursor" also answers "what would ``Space`` do here".
        """
        assert isinstance(state, ForestState)
        node = self.option(state)
        if node is None:
            return BLANK
        value = state.values.get(node.id, "")
        return Line(
            Segment(node.label, self.tone("focus")),
            Segment(f"  {value}" if value else "", self.tone(self._state_role(node, state))),
        )

    def detail(self, state: object) -> list[Line]:
        """The prose describing the row under the cursor, wrapped for wherever it will be shown.

        Rebuilt per paint rather than cached, because the tree itself is re-derived from the answer
        on every keystroke — a pane that outlived the row it described would say something about a
        file the operator has just switched off.

        The row's own name is not repeated here: the cursor is on it and the status line spells it
        out, so a header would be one more line of the screen spent saying nothing.
        """
        assert isinstance(state, ForestState)
        node = self.option(state)
        if node is None or not node.detail:
            return []
        return [
            Line(Segment(piece, self.tone("dim")))
            for piece in self._wrapped(node.detail, self.detail_room)
        ]

    def detail_source(self, state: object) -> tuple[str, ...]:
        """The same prose before it was wrapped.

        The overlay re-measures it: a sentence broken for a pane twenty-eight columns wide breaks in
        the wrong place across the full width of the overlay, and a reader who went looking for the
        whole of a description should not get the pane's line endings with it.
        """
        assert isinstance(state, ForestState)
        node = self.option(state)
        return (node.detail,) if node is not None and node.detail else ()

    # -- answering --------------------------------------------------------------------------

    def focus(self, state: object) -> int:
        assert isinstance(state, ForestState)
        options = len(self.options())
        if options <= 0:
            return 0
        return min(max(state.focus, 0), options - 1)

    def with_focus(self, state: object, index: int) -> ForestState:
        assert isinstance(state, ForestState)
        return replace(state, focus=index)

    def toggled(self, state: object, target: object) -> object:
        """``Space``: cycle this row's state, leaving every other row's answer alone."""
        assert isinstance(state, ForestState)
        node = self._by_id(target)
        if node is None or not node.editable:
            return state
        values = dict(state.values)
        values[node.id] = next_state(node, values.get(node.id, ""))
        return replace(state, values=values)

    def _by_id(self, target: object) -> Node | None:
        if not isinstance(target, str):
            return None
        for node, _, _, _ in self.rows_in_order():
            if node.id == target:
                return node
        return None

    def commit(self, state: object) -> Result:
        """A copy of the answer map, so the caller cannot hold the loop's own dict open.

        No summary: what this tree *amounts to* is a question about prompts and documents, which is
        the launcher's vocabulary rather than the surface's, and the caller that built the rows is
        the one that can say what the answer means in a line.
        """
        assert isinstance(state, ForestState)
        return Result(value=dict(state.values))


def _drop(value: int, pct: int) -> tuple[int, int]:
    """Give up the rightmost non-empty column, which is the one that says the least per cell."""
    if pct:
        return (value, 0)
    return (0, 0)
