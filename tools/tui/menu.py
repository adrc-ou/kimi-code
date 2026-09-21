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

#: Columns of label before the hint column stops being a column. Past this a list is a wall of text
#: and an aligned hint is worth less than the space it costs.
MAX_LABEL_FIELD = 34

#: Blank cells between the label column and the hint column.
HINT_GAP = 3


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
    ) -> None:
        """
        ``prompt`` is the heading above the list and ``title`` is the frame's top-left label and the
        window title; they are separate because "Modules" and "Choose which modules to load" are
        both useful and neither repeats the other.

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
        self._opening = self._resolve(previous, initial)

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
        return min(widest, MAX_LABEL_FIELD)

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
        out.extend(self._row(choice, choice.id in chosen, field) for choice in self.choices)
        return out

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
            return Line(
                Segment(option.label, self.tone("focus")),
                Segment(f"  {option.hint}" if option.hint else "", self.tone("dim")),
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
            return Result(value=option.id, summary=option.label)
        labels = {choice.id: choice.label for choice in self.choices}
        value = list(self._in_order(set(state.chosen)))
        return Result(value=value, summary=", ".join(labels.get(id_, id_) for id_ in value))
