"""The tree-shaped surface: a diagram the operator can edit.

The launcher's context step is not a list. It is a set of documents, each composed out of the ones
under it, some of which are overridden by others, and each carrying a switch — which is exactly a
tree, and was being shown as two flat columns of text joined by nothing. ``menu`` cannot draw that
and ``input`` does not want to, so this module holds the third shape, and the drawing work that goes
with it stays here rather than in the step that knows what a context block is.

Two kinds of answer live in one tree and the surface keeps them apart the way a settings screen
does. A two-state row is a checkbox, because everyone knows what ``[x]`` means. A row with more than
two states shows the state's own **word** in a column of its own: no surveyed terminal interface
ships a tri-state glyph, so an invented third mark would be precisely the undocumented signal this
redesign was commissioned to remove, whereas ``auto`` / ``on`` / ``off`` needs no legend, survives a
monochrome terminal, and cannot be mistaken for a checkbox in a state nobody explained.

Structure and state are separate axes and are drawn by separate means. Structure is the branch
guide, ``▶`` in the joint where one source feeds more than one parent, and a magenta connector where
a document is substituted into another by template rather than appended after it. State is the mark,
the word and the colour: a superseded row is dim **and** labelled, and hollow wherever it had a
switch to hollow, so it reads as inactive without anyone being asked to trust hue alone, which
neither ``NO_COLOR`` nor a colour-blind operator can be required to discount.

The tail columns are measured over the whole tree rather than over the visible slice, so neither
scrolling nor cycling a state can slide a number sideways, and a figure the operator was comparing
two rows ago is still in the same column.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field, replace

from . import layout as L
from .app import Result, Step
from .layout import BLANK, Line, Row, Segment

#: Nothing to change here: a heading, a branch, or a source whose answer belongs to its parent.
PLAIN = "plain"
#: Two states, drawn as a checkbox. The first state in ``states`` is the marked one.
CHECK = "check"
#: Any number of states, drawn as the state's own word, in a column of its own.
WORD = "word"

#: Cells a checkbox and its gutter occupy, so every label in a branch starts in one column.
MARK_WIDTH = 4
#: Columns one guide level occupies: the tee or the rail, the dash, and its space.
RAIL_WIDTH = 3
#: The fewest label columns worth keeping before the numeric tail starts giving way. Below this a
#: figure attached to half a name identifies nothing, so the tail is dropped instead of the label.
MIN_LABEL = 12
#: The floor on the gap between a label and its first right-hand column, shared with the plain
#: panel so a wrapped label never lands against a number in either renderer.
COLUMN_GAP = 3
#: The gap between two tail columns.
FIELD_GAP = 2
#: The fewest columns a sentence needs before it is worth setting at an indent at all. Below this a
#: wrapped note becomes one word per line, which is not a sentence any more.
NOTE_ROOM = 4

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
    #: :data:`CHECK` or :data:`WORD` — how ``states`` is drawn.
    kind: str = PLAIN
    #: The state this row opens with, for as long as nobody has answered it.
    default: str = ""
    #: The right-hand figures: tokens, and the share of the lane's cap they spend.
    value: str = ""
    pct: str = ""
    #: Prose under the row, wrapped to the room the label had. The generated help overlay has no
    #: slot for a step's own sentences, so a fact the operator needs on screen lives here, beside
    #: the thing it describes, rather than behind a keypress.
    note: str = ""
    #: One word after the label — ``unused``, ``shared``, ``staged``. Words, because a glyph that
    #: exists only in this one program is the thing this redesign is not allowed to invent.
    badge: str = ""
    #: False draws a hollow mark, dims the row, and keeps the cursor off it. For a source that is
    #: present but overridden: it belongs in the picture and must not look selectable.
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

        A checkbox says two states and a spelled word says as many as there are; a row that asks for
        one while carrying the other would put a switch on screen whose position nobody can read —
        the exact class of thing this redesign exists to remove. Cheaper to raise while the tree is
        being built than to explain in a legend afterwards.
        """
        if self.states and self.kind == PLAIN:
            raise ValueError(f"{self.id}: {PLAIN!r} draws no mark, so it cannot carry a switch")
        if self.kind == CHECK and len(self.states) != 2:
            raise ValueError(f"{self.id}: a checkbox has exactly two states, got {self.states}")
        if self.kind == WORD and not self.states:
            raise ValueError(f"{self.id}: a word column needs states to spell")

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
    ) -> None:
        """
        ``head`` is prose above the tree — the sentences a generated legend cannot carry, because
        the legend is built from the binding table and knows nothing about prompts.

        ``opening`` is the answer the user is revisiting. A row it does not name falls back to its
        own :attr:`Node.default`, the same precedence :class:`~.menu.ListStep` uses, so the two
        surfaces cannot disagree about what "remembered" means.

        ``tone_of`` colours a row's state. The surface knows that there are three states and the
        launcher knows what they mean, and this is where those two knowledges have to meet.
        """
        self.title = title
        self.rail = rail or title
        self.nodes = tuple(nodes)
        self.head = tuple(head)
        self.reset_note = reset_note
        self._opening = dict(opening or {})
        self._tone_of = tone_of
        self._roots_are_checkable = any(node.kind == CHECK for node in self.nodes)

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

    def tail_widths(self) -> tuple[int, int, int]:
        """Widths of the state-word, figure and percentage columns.

        Measured over the whole tree, and over every *spelling* of a state rather than only its
        current one, so cycling a row from ``auto`` to ``off`` cannot move the number beside it.
        """
        rows = self.rows_in_order()
        word = max(
            (
                max((self.cells(spelling) for spelling in node.states), default=0)
                for node, _, _, _ in rows
                if node.kind == WORD
            ),
            default=0,
        )
        value = max((self.cells(node.value) for node, _, _, _ in rows), default=0)
        pct = max((self.cells(node.pct) for node, _, _, _ in rows), default=0)
        return (word, value, pct)

    def fit_tail(self, widths: tuple[int, int, int]) -> tuple[int, int, int]:
        """Shrink the tail until even the deepest row has a label worth reading.

        The decision is made once against the deepest row rather than per row: a tail that appeared
        beside a short label and vanished beside a long one would put the same figure in a different
        column two rows apart, which is the one thing a column of numbers is on screen for.
        """
        widest = max(
            (
                self.left(node, depth) + self._badge_cells(node.badge)
                for node, _, depth, _ in self.rows_in_order()
            ),
            default=0,
        )
        word, value, pct = widths
        while self._tail_cells(word, value, pct) and (
            self.room - widest - COLUMN_GAP - self._tail_cells(word, value, pct) < MIN_LABEL
        ):
            word, value, pct = _drop(word, value, pct)
        return (word, value, pct)

    def left(self, node: Node, depth: int) -> int:
        """Columns the guide and the mark spend before a label may start.

        Every row below a root pays for the mark even when it has no checkbox, so a source, a
        switch and a heading in one branch share a single label edge. A root pays for it too as soon
        as any root has one, because a tree whose sections start at two different columns reads as
        two trees.
        """
        return depth * RAIL_WIDTH + self.mark_room(node, depth)

    def mark_room(self, node: Node, depth: int) -> int:
        """Whether this row's label sits behind a mark column, and how wide that column is."""
        if depth or node.kind == CHECK or self._roots_are_checkable:
            return MARK_WIDTH
        return 0

    def _tail_cells(self, word: int, value: int, pct: int) -> int:
        """Columns the tail spends, gaps included, with no leading gap on the first live column."""
        out = 0
        if word:
            out += word
        if value:
            out += value + (FIELD_GAP if word else 0)
        if pct:
            out += pct + (FIELD_GAP if word or value else 0)
        return out

    def _badge_cells(self, badge: str) -> int:
        """A badge sits between the label and the tail, so it is neither free nor forgettable."""
        return 1 + self.cells(badge) if badge else 0

    def badge_shown(self, node: Node, depth: int, *tail: int) -> bool:
        """Whether this row can still afford its badge, which it gives up before its label does.

        The word is the secondary signal for what the hollow mark already says, whereas the label is
        the row's identity: at a width where both cannot live, the number went first, the word goes
        next, and the name survives. Which rows lose theirs is a per-row question, so it is answered
        per row rather than with a flag that would drop every ``unused`` to spare one crowded row.
        """
        if not node.badge:
            return False
        give = self.left(node, depth) + self._badge_cells(node.badge) + self._tail_cells(*tail)
        return self.room - give - COLUMN_GAP >= MIN_LABEL

    def _cut(self, text: str, room: int) -> str:
        if self.caps is not None:
            return L.cut(text, room, self.caps)
        return text if len(text) <= room else text[: max(0, room)]

    def _clip_label(self, text: str, room: int) -> str:
        """A label the row cannot hold is marked as clipped, never chopped in silence.

        ``main system promp`` reads as a shorter name than ``main system prompt``, and the two are
        different rows to a user scanning the tree. The mark costs a column and says the rest is
        somewhere else — which it is, in the status line, where the focused row is spelled out.
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
        for text in self.head:
            for piece in self._wrapped(text, self.room):
                out.append(Row.heading(Line(Segment(piece, self.tone("dim")))))
        if self.head:
            out.append(Row.gap())
        word, value, pct = self.fit_tail(self.tail_widths())
        for node, rails, depth, last in self.rows_in_order():
            out.extend(self._rows(node, rails, depth, last, state, word, value, pct))
        return out

    def _rows(
        self,
        node: Node,
        rails: tuple[bool, ...],
        depth: int,
        last: bool,
        state: ForestState,
        word: int,
        value: int,
        pct: int,
    ) -> list[Row]:
        columns = self._columns(node, word, value, pct)
        if self._is_prose(node, columns):
            out = self._prose(node, rails, depth, last, state)
        else:
            out = [self._row(node, self._line(node, rails, depth, last, state, word, value, pct))]
        if node.note:
            out.extend(self._note(node, depth))
        return out

    def _is_prose(self, node: Node, columns: tuple[int, int, int]) -> bool:
        """Whether a row is a sentence rather than a control.

        Only a plain row with no figure to align may take the whole width: a switch, a state word,
        and even a disabled checkbox all print a mark that the label edge is measured against, and
        wrapping a row that has one moves the mark off the column every other row shares. Prose is
        the heading and the source name — the rows that carry nothing but words.
        """
        return node.kind == PLAIN and not any(columns)

    def _columns(self, node: Node, word: int, value: int, pct: int) -> tuple[int, int, int]:
        """The tail this row really has, out of the columns the tree fitted.

        The columns are shared so figures line up, and they stay shared among the rows that carry
        figures — a number that moved a column between two rows is the failure this whole layout
        exists to avoid. What is *not* reserved is tail a row can never print: reserving it anyway
        is what cut a section title off mid-word in a window with room for the whole of it.
        """
        shown = word if node.kind == WORD else 0
        if not node.value and not node.pct:
            return (shown, 0, 0)
        return (shown, value, pct)

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
        badge = node.badge if self.badge_shown(node, depth, 0, 0, 0) else ""
        room = max(1, self.room - edge - self._badge_cells(badge))
        pieces = self._wrapped(node.label, room)
        tone = self._label_tone(node, state)
        parts: list[Segment] = []
        if guide:
            parts.append(Segment(guide, self.tone("link") if node.link else self.tone("rule")))
        if mark:
            parts.append(Segment(mark))
        parts.append(Segment(pieces[0], tone))
        if badge:
            parts.append(self._badge(badge))
        out = [self._row(node, Line(*parts))]
        out.extend(
            self._row(node, Line(Segment(f"{' ' * edge}{piece}", tone))) for piece in pieces[1:]
        )
        return out

    def _note(self, node: Node, depth: int) -> list[Row]:
        """The sentence under a row, at the row's own indent until that leaves nothing to write in.

        The indent is a nicety and the sentence is not, so a deep row in a shallow window puts its
        note at column zero rather than losing it. Everything the tree says has to reach the
        screen — that is the whole argument for scrolling inside the frame instead of clipping.
        """
        indent = self.left(node, depth) + MARK_WIDTH
        room = self.room - indent
        if room < NOTE_ROOM:
            indent, room = 0, self.room
        pad = " " * indent
        return [
            Row.heading(Line(Segment(f"{pad}{piece}", self.tone("dim"))))
            for piece in self._wrapped(node.note, room)
        ]

    def _row(self, node: Node, line: Line) -> Row:
        if not node.editable:
            return Row.heading(line, self.tone("dim") if not node.enabled else "")
        return Row.item(line, node.id, wash=False)

    def _line(
        self,
        node: Node,
        rails: tuple[bool, ...],
        depth: int,
        last: bool,
        state: ForestState,
        word: int,
        value: int,
        pct: int,
    ) -> Line:
        parts: list[Segment] = []
        guide = self._guide(rails, depth, last, node.branch)
        if guide:
            parts.append(Segment(guide, self.tone("link") if node.link else self.tone("rule")))
        if node.kind == CHECK:
            parts.append(Segment(f"{self._box(node, state)} ", self._mark_tone(node, state)))
        elif self.mark_room(node, depth):
            parts.append(Segment(" " * MARK_WIDTH))
        columns = self._columns(node, word, value, pct)
        badge = node.badge if self.badge_shown(node, depth, *columns) else ""
        spare = self._badge_cells(badge)
        label = self._clip_label(node.label, self._label_room(node, depth, spare, *columns))
        parts.append(Segment(label, self._label_tone(node, state)))
        if badge:
            parts.append(self._badge(badge))
        parts.extend(self._tail(node, depth, state, spare, *columns, label))
        return Line(*parts)

    def _label_room(self, node: Node, depth: int, spare: int, *tail: int) -> int:
        give = self.left(node, depth) + spare + self._tail_cells(*tail)
        return max(1, self.room - give - COLUMN_GAP)

    def _guide(self, rails: tuple[bool, ...], depth: int, last: bool, branch: bool) -> str:
        """The rails of the ancestors, then this row's joint.

        ``▶`` is set *in* the joint rather than beside it, so a fan-out costs no extra column and
        still reads at a glance; the ASCII tier gets ``>`` for the same reason it gets ``+`` for a
        tee. A root draws nothing, because there is no joint above it to connect to.
        """
        if not depth:
            return ""
        glyph = L.box_glyphs(self.caps) if self.caps is not None else L.BOX_LIGHT
        out = "".join(f"{glyph['v']}  " if keep else "   " for keep in rails)
        joint = glyph["bl"] if last else glyph["tee_l"]
        if branch:
            dash = ">" if glyph is L.BOX_ASCII else "▶"
        else:
            dash = glyph["h"]
        return f"{out}{joint}{dash} "

    def _badge(self, badge: str) -> Segment:
        """A superseded block is labelled with a word, and the word is the same one every time."""
        role = "dim" if badge == "unused" else "info"
        return Segment(f" {badge}", self.tone(role))

    def _box(self, node: Node, state: ForestState) -> str:
        """The checkbox, always three cells.

        Hollow ``[-]`` is the mark :mod:`~.menu` already spends on a row the cursor cannot reach, so
        a superseded source reads as disabled rather than merely unchecked — the difference between
        "this is off" and "this is off and no key here changes it".
        """
        if not node.enabled:
            return "[-]"
        return "[x]" if state.values.get(node.id, "") == node.states[0] else "[ ]"

    def _mark_tone(self, node: Node, state: ForestState) -> str:
        if not node.enabled:
            return self.tone("dim")
        return self.tone("active") if state.values.get(node.id, "") == node.states[0] else ""

    def _label_tone(self, node: Node, state: ForestState) -> str:
        """The label's colour, which is how a superseded chain goes grey before a word is read.

        Only a state heavy enough to matter carries onto the label, and a forced override takes the
        override colour rather than green, so two different reasons for "not the default" never
        arrive in the same glyph.
        """
        if node.tone:
            return self.tone(node.tone)
        if not node.enabled:
            return self.tone("dim")
        role = self._state_role(node, state)
        return self.tone(role) if role in _HEAVY else ""

    def _state_role(self, node: Node, state: ForestState) -> str:
        value = state.values.get(node.id, "")
        return self._tone_of(node, value) or TONE_FOR.get(value, "")

    def _tail(
        self,
        node: Node,
        depth: int,
        state: ForestState,
        spare: int,
        word: int,
        value: int,
        pct: int,
        label: str,
    ) -> list[Segment]:
        used = (
            self.left(node, depth) + spare + self.cells(label) + self._tail_cells(word, value, pct)
        )
        # The gap belongs to the columns, not to the row: padding the edge of an empty tail would
        # widen the row for nothing, and a row the frame has to clip is a row that looks broken.
        if not any((word, value, pct)):
            return []
        out = [Segment(" " * max(COLUMN_GAP, self.room - used))]
        if word:
            shown = state.values.get(node.id, "") if node.kind == WORD else ""
            out.append(Segment(self._cut(shown, word).rjust(word), self._state_attr(node, state)))
        if value:
            if word:
                out.append(Segment(" " * FIELD_GAP))
            out.append(Segment(self._cut(node.value, value).rjust(value)))
        if pct:
            if word or value:
                out.append(Segment(" " * FIELD_GAP))
            out.append(Segment(self._cut(node.pct, pct).rjust(pct), self.tone("dim")))
        return out

    def _state_attr(self, node: Node, state: ForestState) -> str:
        return self.tone(self._state_role(node, state))

    # -- status -----------------------------------------------------------------------------

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
            Segment(f"  {value}" if value else "", self._state_attr(node, state)),
        )

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


def _drop(word: int, value: int, pct: int) -> tuple[int, int, int]:
    """Give up the rightmost non-empty column, which is the one that says the least per cell."""
    if pct:
        return (word, value, 0)
    if value:
        return (word, 0, 0)
    return (0, 0, 0)
