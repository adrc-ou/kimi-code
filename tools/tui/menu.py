"""The list-shaped surface: the one step class the launcher's menus are written with.

Module selection, the two model lanes and the version menus are all the same shape — a heading, a
column of options, a hint against each one, and an answer that is a subset of the list. Putting that
shape here rather than in each caller is what makes those steps read as one program: the marker
glyphs, the focus ring, the status line and the footer all come from one place and cannot drift.

A surface that needs something a list does not have — the context forest, the secret prompts —
subclasses :class:`~.app.Step` directly and inherits the same frame. This module is a convenience on
top of that contract, not a replacement for it.

Two modes, because the launcher asks two different questions. ``multi`` is "which of these" and
answers with a set: ``Space`` ticks the row under the cursor and ``Enter`` accepts whatever is
ticked. ``single`` is "which one" and answers with one id: ``Space`` moves the mark to the row under
the cursor, and ``Enter`` accepts whichever row carries the mark. The cursor is therefore where the
next ``Space`` lands and never the answer itself, on either mode — a row can only be the answer by
looking like it, which is what the mark is for. Both modes answer ``Space``, and the legend names
the key after the job it does in each, since a hint that describes the other mode's key misleads
exactly as surely as one that advertises a key the step ignores.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace

from . import layout as L
from .app import Result, Step
from .layout import BLANK, Line, Row, Segment

#: Which of these — ``Space`` toggles rows and ``Enter`` accepts the set.
MULTI = "multi"
#: Which one — ``Enter`` chooses the focused row and ends the step.
SINGLE = "single"

#: Blank cells between the label column and the hint column.
HINT_GAP = 3

#: Blank cells between two columns of a table. Narrower than :data:`HINT_GAP` because a table has
#: more seams to pay for, and the whole point of one is to be read across.
COLUMN_GAP = 2

#: ``(x) `` ahead of every row, table or not.
MARKER_FIELD = 4

#: A column is not allowed to eat the window: past this its content is clipped and the row is
#: better read from the status line under the list.
MAX_COLUMN_FIELD = 28

#: The narrowest a table's first column may be squeezed before the table gives up and falls back
#: to the two-column form. Below this the name is not a name any more.
MIN_LABEL_COLUMN = 12


@dataclass(frozen=True)
class Column:
    """One table column's header.

    Only the header needs saying: width comes from the widest cell the list actually holds, and
    the columns without a header are the ones whose content is already a whole phrase (``Ready``,
    ``(last used)``), where a label above would repeat what the row says.
    """

    header: str = ""


@dataclass(frozen=True)
class Choice:
    """One option: what it is called, what to say about it, and whether it can be taken.

    ``id`` is what the launcher gets back, and what ``previous`` and ``initial`` are written in;
    ``label`` is what the user reads. Keeping them apart is what lets a manifest rename an option
    without stranding the answer recorded from the last launch.
    """

    id: str
    label: str
    #: Right-hand column: provider, size, or why an option cannot be taken. Shown dim, and repeated
    #: in the status row for whichever row the cursor is on, because this column is the first thing
    #: a narrow window clips.
    hint: str = ""
    #: False draws the row dimmed with a hollow marker and keeps the cursor off it. The reason
    #: belongs in ``hint``: an option that is unavailable without explanation reads as a bug.
    enabled: bool = True
    #: The declared default, which ``previous`` overrides, which ``initial`` overrides.
    checked: bool = False
    #: One string per :class:`Column`, when the step was given a table to render in. ``label`` and
    #: ``hint`` stay filled beside them: they are what the row falls back to when the window is too
    #: narrow for the grid, and a reader with a 60-column terminal deserves the same facts.
    cells: tuple[str, ...] = ()


@dataclass(frozen=True)
class ListState:
    """Cursor position and checked set, as the loop holds them.

    ``focus`` counts *selectable* rows rather than body rows, matching what the loop passes in, so a
    disabled row between two options cannot make the cursor jump. ``start`` remembers where the
    cursor opened, so ``reset`` can put it back.
    """

    focus: int = 0
    chosen: tuple[str, ...] = ()
    start: int = 0


class ListStep(Step):
    """A titled list of :class:`Choice` rows that answers with ids."""

    @property
    def toggle_hint(self) -> str:
        """What the footer calls ``Space``, which depends on what it can do to a row here.

        A checkbox has two states and the key walks between them, so it ticks. A radio has one state
        shared by the whole list and the key moves it, so it selects. Saying "toggle" to the second
        one promises a key that switches the answer off, which is the thing a radio cannot do.
        """
        return "select" if self.mode == SINGLE else "toggle"

    def __init__(
        self,
        *,
        title: str,
        choices: Sequence[Choice],
        mode: str = MULTI,
        prompt: str = "",
        rail: str = "",
        head: Sequence[str] = (),
        previous: Sequence[str] = (),
        initial: Sequence[str] | None = None,
        columns: Sequence[Column] = (),
    ) -> None:
        """
        ``prompt`` is the heading above the list and ``title`` is the frame's top-left label and the
        window title; they are separate because "Modules" and "Choose which modules to load" are
        both useful and neither repeats the other.

        ``columns`` turns the list into a table: one header per entry in :attr:`Choice.cells`, and
        every row that leaves ``cells`` empty is still drawn the old way. A table is what a list of
        models needs, because the same model name appears once per provider and once per window and
        only a column can say which is which without reusing the name as a sentence.

        The opening selection is ``initial`` when given, otherwise whatever ``previous`` names,
        otherwise the rows that declare ``checked`` — an answer the user is revisiting beats last
        launch's answer, which beats a default.
        """
        if mode not in (MULTI, SINGLE):
            raise ValueError(f"unknown list mode: {mode}")
        self.title = title
        self.rail = rail or title
        self.choices = tuple(choices)
        self.mode = mode
        self.prompt = prompt
        self.head = tuple(head)
        self.columns = tuple(columns)
        self._opening = self._resolve(previous, initial)

    @property
    def table(self) -> bool:
        """Whether this step was given columns at all, which is not the same as rows using them."""
        return bool(self.columns)

    # -- opening ---------------------------------------------------------------------------

    def _resolve(self, previous: Sequence[str], initial: Sequence[str] | None) -> tuple[str, ...]:
        """Which ids are on when the step opens, in list order.

        Restricted to selectable rows: an option the cursor cannot reach must not be able to arrive
        in the answer by having been chosen before it became unavailable.
        """
        available = {choice.id for choice in self.selectable()}
        if initial is not None:
            wanted = tuple(initial)
        elif any(item in available for item in previous):
            wanted = tuple(previous)
        else:
            wanted = tuple(choice.id for choice in self.choices if choice.checked)
        chosen = tuple(choice.id for choice in self.selectable() if choice.id in set(wanted))
        return chosen[:1] if self.mode == SINGLE else chosen

    def selectable(self) -> list[Choice]:
        """The rows the cursor may rest on, in the order it meets them."""
        return [choice for choice in self.choices if choice.enabled]

    def option(self, state: ListState) -> Choice | None:
        """The choice under the cursor, or ``None`` when every row is disabled."""
        options = self.selectable()
        if not options:
            return None
        return options[min(max(state.focus, 0), len(options) - 1)]

    def picked(self, state: ListState) -> Choice | None:
        """The row a radio step answers with, which is the marked one.

        The mark is the answer's only visible form, so it is also what the status row and ``Enter``
        both read — a screen that shows one row checked and returns another is the failure this step
        class exists not to repeat. A list that opens with nothing marked, because it has neither a
        remembered answer nor a declared default, has no mark to read, and takes the cursor's row
        rather than answering with nothing.
        """
        chosen = set(state.chosen)
        for choice in self.selectable():
            if choice.id in chosen:
                return choice
        return self.option(state)

    def initial(self) -> ListState:
        """The opening state, with the cursor already on the answer it starts with.

        Landing the cursor on the pre-selected row is what makes ``Enter`` correct without reading
        the list first, which is the whole value of a remembered answer.
        """
        ids = [choice.id for choice in self.selectable()]
        opening = set(self._opening)
        focus = next((at for at, id_ in enumerate(ids) if id_ in opening), 0)
        return ListState(focus=focus, chosen=self._opening, start=focus)

    def reset(self, state: object) -> ListState:
        """The opening answer *and* the opening cursor.

        The brief asks for the visible options, but a reset that leaves the cursor wherever the user
        dragged it reads as half-done, so this restores both — and nothing else.
        """
        del state
        return self.initial()

    # -- measuring -------------------------------------------------------------------------

    def clip(self, text: str, room: int) -> str:
        """Leading characters of ``text`` that fit in ``room`` columns, marked when cut.

        Delegates to the engine's own ``fit`` arithmetic whenever a terminal is attached, so a label
        clipped here and a line clipped at the frame edge agree about what ``…`` costs; the bare
        fallback only has to be consistent, since nothing is on screen to be inconsistent with.
        """
        if self.caps is not None:
            return L.cut(text, room, self.caps)
        if room <= 0 or len(text) <= room:
            return text[:room] if room else ""
        return text[: max(0, room - 1)] + "…"

    def _field(self) -> int:
        """Label column width, or zero when there is no hint column to align to."""
        if not any(choice.hint for choice in self.choices):
            return 0
        widest = max((self.cells(choice.label) for choice in self.choices), default=0)
        hint = max((self.cells(choice.hint) for choice in self.choices), default=0)
        # Fit real labels before padding the hint column. On narrow terminals a long hint
        # gets at most a third of the row, leaving the identifying label useful too.
        reserve = min(hint, max(0, self.room // 3))
        return min(widest, max(1, self.room - 4 - HINT_GAP - reserve))

    def _column_widths(self) -> list[int] | None:
        """One width per column, or ``None`` when the window cannot hold the grid.

        Each column is as wide as its widest cell or its own header, capped at
        :data:`MAX_COLUMN_FIELD`. When that total overshoots the room, the first column absorbs
        it: every other column holds a short fixed vocabulary - ``Ready``, ``Not Installed``, a
        provider label - where the text *is* the information, while a model name is the one cell
        that still reads after being cut. If squeezing it below :data:`MIN_LABEL_COLUMN` still
        does not fit, the answer is ``None`` and the rows go back to label-plus-hint, because a
        table too narrow to align is a wall of text with extra work taken.
        """
        if not self.table:
            return None
        widths = []
        for index, column in enumerate(self.columns):
            widest = self.cells(column.header)
            for choice in self.choices:
                if index < len(choice.cells):
                    widest = max(widest, self.cells(choice.cells[index]))
            widths.append(min(MAX_COLUMN_FIELD, widest))
        total = MARKER_FIELD + sum(widths) + COLUMN_GAP * (len(widths) - 1)
        if total > self.room:
            widths[0] = max(MIN_LABEL_COLUMN, widths[0] - (total - self.room))
            total = MARKER_FIELD + sum(widths) + COLUMN_GAP * (len(widths) - 1)
            if total > self.room:
                return None
        return widths

    def describe(self, choice: Choice) -> str:
        """One line saying which row this is, without the hint.

        The grid splits a row across columns; anything that reads the row as one string - the
        status line, the recorded summary - has to re-join them. Doing that from the same cells is
        what keeps the two able to disagree.
        """
        parts = choice.cells if self.table else (choice.label,)
        return "  ".join(part for part in parts if part)

    def _rendering_table(self) -> bool:
        """Whether the grid fits the window, which is the same question the rows already answered.

        The status line has to know it too: beside a table, repeating the row's hint underneath
        would print the provider a second time, while in the narrow fallback that hint is the only
        place the provider appears at all.
        """
        return self._column_widths() is not None

    def mark(self, choice: Choice, selected: bool) -> str:
        """The selection glyph — always three cells, so labels stay in one column.

        Brackets for a checkbox and parentheses for a radio, because two states that look alike are
        two ways to misread the same screen. ``-`` is neither on nor off, and appears only on a row
        no key can reach.
        """
        open_, close_ = ("[", "]") if self.mode == MULTI else ("(", ")")
        if not choice.enabled:
            return f"{open_}-{close_}"
        return f"{open_}{'x' if selected else ' '}{close_}"

    # -- rendering -------------------------------------------------------------------------

    def rows(self, state: object) -> list[Row]:
        assert isinstance(state, ListState)
        chosen = set(state.chosen)
        out = [Row.heading(Line(Segment(text, self.tone("dim")))) for text in self.head]
        if self.prompt:
            if out:
                out.append(Row.gap())
            out.append(Row.heading(Line(Segment(self.prompt, self.tone("title")))))
            out.append(Row.gap())
        field = self._field()
        widths = self._column_widths()
        if widths is not None:
            out.append(self._header_row(widths))
            out.extend(
                self._table_row(choice, choice.id in chosen, widths) for choice in self.choices
            )
            return out
        out.extend(self._row(choice, choice.id in chosen, field) for choice in self.choices)
        return out

    def _cell(self, text: str, width: int, attr: str, *, fill: bool) -> Line:
        """One grid cell: cut to ``width``, and filled out to it unless it is the last column.

        Display width is the engine's own arithmetic (:func:`~.layout.padded`), because a model
        name with a wide character in it is two columns per glyph and a ``ljust`` would move every
        header off its column. The last cell is not filled: nothing follows it, and trailing
        spaces are a difference nobody can see.
        """
        if self.caps is None:
            cut = self.clip(text, width)
            return Line(Segment(cut.ljust(width) if fill else cut, attr))
        line = Line(Segment(text, attr))
        return L.padded(line, width, self.caps) if fill else L.fit(line, width, self.caps)

    def _header_row(self, widths: Sequence[int]) -> Row:
        """The column labels, over the columns rather than beside them.

        The marker field belongs to the row and not to the header: every body row opens with
        ``( )``, so an unlabelled lead-in of the same width is what puts ``Model`` over the names.
        """
        dim = self.tone("dim")
        last = len(widths) - 1
        parts = [Segment(" " * MARKER_FIELD, dim)]
        for index, column in enumerate(self.columns):
            if index:
                parts.append(Segment(" " * COLUMN_GAP, dim))
            parts.extend(self._cell(column.header, widths[index], dim, fill=index != last).segments)
        return Row.heading(Line(*parts))

    def _table_row(self, choice: Choice, selected: bool, widths: Sequence[int]) -> Row:
        """One row of the grid. The marker column is shared with the two-column form on purpose."""
        dim = self.tone("dim")
        attr = dim if not choice.enabled else ""
        last = len(widths) - 1
        marker = self.tone("active") if selected else ""
        parts = [Segment(f"{self.mark(choice, selected)} ", marker)]
        for index, width in enumerate(widths):
            if index:
                parts.append(Segment(" " * COLUMN_GAP))
            text = choice.cells[index] if index < len(choice.cells) else ""
            parts.extend(self._cell(text, width, attr, fill=index != last).segments)
        if not choice.enabled:
            return Row(line=Line(*parts), attr=dim)
        return Row.item(Line(*parts), choice.id)

    def _row(self, choice: Choice, selected: bool, field: int) -> Row:
        dim = self.tone("dim")
        marker = self.mark(choice, selected)
        label = choice.label
        if field:
            label = self.clip(choice.label, field)
        parts = [Segment(f"{marker} ", self.tone("active") if selected else "")]
        parts.append(Segment(label, dim if not choice.enabled else ""))
        padding = field - self.cells(label)
        if padding > 0:
            parts.append(Segment(" " * padding))
        if choice.hint:
            parts.append(Segment(" " * HINT_GAP if field else " "))
            parts.append(Segment(choice.hint, dim))
        if not choice.enabled:
            return Row(line=Line(*parts), attr=dim)
        return Row.item(Line(*parts), choice.id)

    def status(self, state: object) -> Line:
        assert isinstance(state, ListState)
        if self.mode == SINGLE:
            option = self.picked(state)
            if option is None:
                return BLANK
            # Beside a grid the hint would repeat a column the user is already reading - except on
            # a row the cursor cannot reach, where the hint is the reason it cannot be reached and
            # the grid has no column for it.
            show_hint = not self._rendering_table() or not option.enabled
            hint = option.hint if show_hint else ""
            return Line(
                Segment(self.describe(option), self.tone("focus")),
                Segment(f"  {hint}" if hint else "", self.tone("dim")),
            )
        count = len(state.chosen)
        return Line(
            Segment(
                f"{count} of {len(self.selectable())} selected",
                self.tone("active") if count else self.tone("dim"),
            )
        )

    # -- answering -------------------------------------------------------------------------

    def focus(self, state: object) -> int:
        assert isinstance(state, ListState)
        options = len(self.selectable())
        if options <= 0:
            return 0
        return min(max(state.focus, 0), options - 1)

    def with_focus(self, state: object, index: int) -> ListState:
        assert isinstance(state, ListState)
        return replace(state, focus=index)

    def toggled(self, state: object, target: object) -> object:
        """``Space``: tick the row under the cursor, or move the mark onto it.

        The mark never leaves a radio list, not even when the key is pressed on the row that already
        carries it: one of those rows has to be the answer, and a key that could empty it would
        leave ``Enter`` with nothing to commit.
        """
        assert isinstance(state, ListState)
        if self.mode == SINGLE:
            return self._marked(state, target)
        chosen = set(state.chosen)
        if target in chosen:
            chosen.discard(target)
        else:
            chosen.add(target)
        return replace(state, chosen=self._in_order(chosen))

    def chosen(self, state: object, target: object) -> object:
        """``Enter`` landed on this row, which only answers for a radio with no mark on it yet."""
        assert isinstance(state, ListState)
        if self.mode == SINGLE and not state.chosen:
            return self._marked(state, target)
        return state

    def _marked(self, state: ListState, target: object) -> ListState:
        """The state with the mark on ``target``, unchanged for a row the cursor cannot reach."""
        if any(choice.id == target for choice in self.selectable()):
            return replace(state, chosen=(target,))
        return state

    def _in_order(self, chosen: set[str]) -> tuple[str, ...]:
        """The set as list order, so an answer never depends on the order it was clicked in."""
        return tuple(choice.id for choice in self.choices if choice.id in chosen)

    def commit(self, state: object) -> Result:
        assert isinstance(state, ListState)
        if self.mode == SINGLE:
            option = self.picked(state)
            if option is None:
                return Result()
            return Result(value=option.id, summary=self.describe(option))
        labels = {choice.id: choice.label for choice in self.choices}
        value = list(self._in_order(set(state.chosen)))
        return Result(value=value, summary=", ".join(labels.get(id_, id_) for id_ in value))
