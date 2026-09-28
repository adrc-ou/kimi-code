"""The workspace question: the directories this checkout has been pointed at, and one you type.

One radio list and one text field on the same screen, which is the whole reason this surface is not
a :class:`~.menu.ListStep`. The list answers the question most of the time, and answering it is two
keystrokes: the newest row is already marked and ``Enter`` takes it. The rest of the time the
operator means a directory that has never been used, and that is a path being typed — so the row
above the list *is* the field, and reaching it turns the row into what is being typed.

Two things about that arrangement decide the keys.

*A field and a list cannot both want the arrow keys.* While the field has them they move the
insertion point, so ``Escape`` hands them back — a blur, the same word the web uses and the same
shape ``dialog`` has always had. ``Tab`` goes and comes back the same way. The key table is rebuilt
from wherever the cursor is on every keystroke, so the legend can never advertise an arrow that
would do the other thing, which is the promise the whole interface makes.

*A path is not a secret and not a sentence.* It is masked by nothing and it is edited in place:
left and right step one character, up and down go to either end. Every other terminal field scrolls
a long value horizontally, which suits a token nobody re-reads. A path gets read back off the
screen before it is agreed to, so this one wraps — after a ``/`` where it can, wherever else it
must — and shows all of it.

Nothing here touches the filesystem except through :mod:`.paths`, and nothing here *changes* it
either: forgetting a remembered row goes through a callback the caller supplies, so this stays a
screen and the launcher keeps owning the disk.
"""

from __future__ import annotations

import os
import textwrap
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace

from . import layout as L
from .app import (
    ABORT,
    FIRST,
    FOCUS_DOWN,
    FOCUS_UP,
    LAST,
    PAGE_DOWN,
    PAGE_UP,
    RESET,
    Result,
    Step,
    View,
)
from .caps import Caps
from .input import CARET, CARET_ASCII, DELETE, DELETE_BINDING, strike
from .keys import Binding, bind
from .layout import BLANK, Line, Row, Segment
from .paths import Completion, Verdict, completions, usable

#: The target on the "New Workspace…" row. A sentinel rather than ``None``, which is what every
#: non-selectable row already carries and so could never be told apart from "nothing was chosen".
NEW = "__new__"

#: The unselected radio and its space, which is also what the field's wrapped rows align under.
MARK = "( ) "

#: The row's own label, and the heading the screen is named after.
NEW_LABEL = "New Workspace..."
HEADING = "Choose a workspace directory"

#: The value the field opens with, and where its caret starts. A path has to begin somewhere, and
#: the root is the one answer that is both correct and unusable: the operator sees a caret after a
#: ``/`` and knows the field is live, and the first ``Tab`` lists the whole machine.
SEEDED = "/"

#: The narrowest value column worth wrapping into. Below this a path is one character per row, and
#: the window is too small to be reading anything anyway.
MIN_FIELD_ROOM = 12

#: How wide the question box may get before it starts wrapping its own sentence.
MAX_ASK_WIDTH = 64


# -- actions this step answers that the shared navigation table has never heard of ------------

BLUR = "blur"
FOCUS_FIELD = "edit-the-path"
COMPLETE = "complete"
CARET_LEFT = "caret-left"
CARET_RIGHT = "caret-right"
CARET_HOME = "caret-home"
CARET_END = "caret-end"
CONFIRM = "confirm"
BUTTON_BACK = "button-back"
BUTTON_FORWARD = "button-forward"
BUTTON_CHOOSE = "button-choose"
BUTTON_CANCEL = "button-cancel"


@dataclass(frozen=True)
class Button:
    """One answer to a :class:`Confirmation`, and the word printed on it."""

    id: str
    label: str


@dataclass(frozen=True)
class Confirmation:
    """A modal question with buttons that can be focused.

    ``about`` carries the path the question was asked about rather than leaving the caller to
    remember it: an answer arrives two keypresses after the question was last painted, and by then
    it has to say what it is an answer to.

    ``focus`` is the index of the button that ``Enter`` would take, and :func:`question` puts it on
    the last one — see there for why that is the safe end.
    """

    prompt: str
    buttons: tuple[Button, ...]
    about: str = ""
    focus: int = 0

    @property
    def chosen(self) -> Button:
        return self.buttons[min(max(self.focus, 0), len(self.buttons) - 1)]


CANCEL = Button("cancel", "Cancel")
OK = Button("ok", "OK")
REMOVE = Button("remove", "Remove From List")


def question(prompt: str, buttons: tuple[Button, ...], about: str) -> Confirmation:
    """A question with its default answer on the button that ends the exchange.

    Which is the one that takes the operator forward when the answer is reversible and keeps them
    on the screen when it is not: creating a directory they asked for twice is not destructive,
    while forgetting a row they may still want is. The left-hand answer is always ``Cancel``, so
    the arrow keys have one thing to move between on every question this screen can ask.
    """
    return Confirmation(prompt=prompt, buttons=buttons, about=about, focus=len(buttons) - 1)


#: The two questions the remembered list can ask, in the words the operator is owed. A directory
#: that has simply gone is recoverable, so it is offered back; one that cannot be made is not, so
#: the row is offered up for removal. Neither answer leaves the screen, because both are
#: corrections to a list the screen is standing behind.
CREATE_ASK = "That directory does not exist. Create it?"
FORGET_ASK = (
    "That directory does not exist and cannot be created. "
    "Remove it from the Recent Workspaces list?"
)


@dataclass(frozen=True)
class PathState:
    """Everything the screen is holding: the list, the value, and where each cursor is.

    ``recent`` lives in the state and not on the step because forgetting a row changes the shape of
    the screen, and every index into it, the moment that answer is given.

    ``at`` indexes the focusable rows, whose *first* one is always the field: "New Workspace…" sits
    above the remembered list, where a reader looking for something that is not on the list already
    will look. ``editing`` says whether that field holds the arrow keys, and is a separate flag
    from ``at`` precisely so that a blurred field can stay the selected row while the arrows move
    the selection around it.
    """

    recent: tuple[str, ...] = ()
    at: int = 0
    text: str = SEEDED
    caret: int = len(SEEDED)
    editing: bool = True
    #: What the last ``Tab`` found. It stays until another ``Tab`` replaces it, including across
    #: keystrokes that make it stale: a panel that vanished the moment a character was typed would
    #: hide the very list the operator is typing in order to narrow down.
    panel: Completion | None = None
    #: Why ``Enter`` did not take the answer, or ``""`` when there is nothing to explain.
    error: str = ""
    confirmation: Confirmation | None = None

    @property
    def last(self) -> int:
        """The lowest row index the cursor may rest on: the oldest remembered workspace."""
        return len(self.recent)

    @property
    def on_field(self) -> bool:
        """Whether the cursor is on the field, which is row zero of the screen."""
        return self.at <= 0

    @property
    def live(self) -> bool:
        """Whether the field owns the arrow keys."""
        return self.on_field and self.editing


def wrap_path(text: str, room: int, caps: Caps | None = None) -> tuple[tuple[int, int], ...]:
    """``text`` cut into ``(start, stop)`` index pairs that each fit in ``room`` columns.

    A break after a ``/`` is taken whenever the row contains one, because a path split mid-name
    reads as two names, and the row it landed on as a second value. A name longer than the row is
    split anyway rather than clipped: the promise of wrapping here is that all of the value is on
    screen.

    Never returns an empty tuple. A value of no width is still a row — it is the row the caret is
    on.
    """
    room = max(1, int(room))

    def columns(value: str) -> int:
        return caps.width(value) if caps is not None else len(value)

    rows: list[tuple[int, int]] = []
    start = 0
    used = 0
    slash = -1
    for index, character in enumerate(text):
        taken = columns(character)
        if used + taken > room:
            cut = slash if start < slash <= index else index
            rows.append((start, cut))
            start = cut
            used = columns(text[cut : index + 1])
            slash = index + 1 if character == "/" else -1
            continue
        used += taken
        if character == "/":
            slash = index + 1
    rows.append((start, len(text)))
    return tuple(rows)


class WorkspaceStep(Step):
    """The remembered directories, plus the one being typed."""

    #: ``Space`` is a character of the answer wherever the field has it, and means nothing on a
    #: list whose selection *is* its cursor. The loop drops the binding on this flag rather than
    #: letting this step have to explain twice that it ignores the key.
    toggleable = False
    #: Every printable character belongs to the field, so letters leave the key table — see
    #: :func:`~.input.strike`, shared with the secret prompts for exactly that reason. Including
    #: ``?``, so the key reference is unreachable here and the legend carries the whole of the
    #: interface. That is the trade a field makes, and this one has eight keys to carry.
    accepts_text = True
    reset_note = "workspace choice reset"

    def __init__(
        self,
        *,
        recent: Sequence[str] = (),
        head: Sequence[str] = (),
        prompt: str = HEADING,
        title: str = "Workspace",
        on_remove: Callable[[str], None] | None = None,
        reserved: Sequence[str] = (),
    ) -> None:
        """
        ``recent`` is the remembered list, already ordered newest first, which is
        :mod:`workspace_registry`'s business and not this screen's.

        ``on_remove`` is called with a path the operator asked to forget. A callback rather than a
        file path, because a screen that rewrote the launcher's state as a side effect of painting
        would be a screen that could not be tested without a disk.
        """
        self.title = title
        self.rail = ""
        self.prompt = prompt
        self.head = tuple(head)
        self.opening = tuple(recent)
        self._on_remove = on_remove
        #: Directories a workspace may not contain, which the launcher passes down rather than
        #: having the screen guess: it is the checkout that mounts the operator's ``.env``, and
        #: only the launcher knows where that checkout is.
        self.reserved = tuple(reserved)

    # -- opening ----------------------------------------------------------------------------

    def initial(self) -> PathState:
        """The newest remembered directory, marked — or the field, when nothing is remembered.

        Landing on the most recent row is what makes an ordinary launch two keystrokes. The field
        opens with a ``/`` and the caret behind it, never with the answer already in it: an
        operator who means a new directory is one keystroke from typing it, and one who means the
        marked row cannot select a path by failing to look at it.
        """
        rows = self.opening
        # The field is row zero and the remembered list starts below it, so the newest workspace is
        # the row at index one — and with nothing remembered, the cursor has nowhere else to be.
        return PathState(recent=rows, at=1 if rows else 0, editing=not rows)

    def reset(self, state: object) -> PathState:
        """The screen as it opened, typed value included.

        ``Ctrl-R`` on a field conventionally means "empty it". Here it means "put the question
        back", which is what every other step does and what the legend promises; and what gets
        discarded is a value the operator has not yet agreed to.
        """
        del state
        return self.initial()

    # -- rows -------------------------------------------------------------------------------

    def rows(self, state: object) -> list[Row]:
        assert isinstance(state, PathState)
        if state.confirmation is not None:
            return self._asking(state.confirmation)
        out = [Row.heading(Line(Segment(text, self.tone("dim")))) for text in self.head]
        if out:
            out.append(Row.gap())
        out.append(Row.heading(Line(Segment(self.prompt, self.tone("title")))))
        out.append(Row.gap())
        out.extend(self._field(state))
        for index, entry in enumerate(state.recent):
            out.append(Row.item(self._remembered(entry, index + 1 == state.at), entry))
        return out

    @property
    def bold(self) -> str:
        """Weight rather than hue, because a list of paths is scanned by its last word.

        :meth:`~.caps.Caps.sgr` survives ``NO_COLOR`` and :meth:`tone` does not, which is the whole
        difference: the basename stays bold on a monochrome terminal, where the colour roles all
        collapse to nothing and an emphasis that rode on colour alone would vanish with them.
        """
        return self.caps.sgr("1") if self.caps is not None else ""

    def mark(self, selected: bool) -> str:
        """The radio glyph and its space, in the same register as :mod:`~.menu`'s.

        Parentheses rather than brackets because this list takes exactly one answer, and the shape
        is the only thing that says so on a terminal with the colour off.
        """
        return f"({'x' if selected else ' '}) "

    def _remembered(self, entry: str, selected: bool) -> Line:
        """One remembered path: its directory receding, its name bold enough to pick out."""
        parent, name = os.path.split(entry.rstrip("/"))
        parts = [Segment(self.mark(selected), self.tone("active") if selected else "")]
        if parent:
            parts.append(Segment(f"{parent}/", "" if selected else self.tone("dim")))
        parts.append(Segment(name or entry, self.bold))
        return Line(*parts)

    def _field(self, state: PathState) -> list[Row]:
        """The "New Workspace…" row, or the value it has become.

        The row turns into the field the moment the cursor reaches it, not the moment a key is
        pressed inside it — the first key has to be the character being typed. Its continuation
        rows are not focusable: the arrows move between *values*, and a field that took three arrow
        presses to step over is a field the list cannot get away from.
        """
        if not state.on_field:
            # The cursor is on a remembered row below it, so the row is still the label the
            # operator reads the field as coming from.
            return [Row.item(Line(Segment(MARK), Segment(NEW_LABEL)), NEW)]
        # The cursor *is* this list's selection, so the row it rests on carries the mark —
        # including once that row has become a field, where the mark is the only thing naming the
        # radio whose answer is being typed.
        prefix = self.mark(True) if state.recent else ""
        pad = " " * self._width(prefix)
        room = max(MIN_FIELD_ROOM, self._room() - self._width(prefix) - 1)
        slices = wrap_path(state.text, room, self.caps)
        # The caret is the field's *focus ring*: drawn only while the field holds the arrow keys,
        # so a blurred row shows the value without claiming that typing goes into it.
        where = self._caret_at(state.text, state.caret, slices) if state.live else (-1, 0)
        rows: list[Row] = []
        for position, (start, stop) in enumerate(slices):
            head = state.text[start:stop]
            parts = [Segment(prefix if position == 0 else pad)]
            if position == where[0]:
                cut = self._index_of_column(head, where[1])
                parts.append(Segment(head[:cut], self.tone("active")))
                parts.append(Segment(self._caret_glyph(), self.tone("focus")))
                parts.append(Segment(head[cut:], self.tone("active")))
            else:
                parts.append(Segment(head, self.tone("active")))
            line = Line(*parts)
            rows.append(Row.item(line, NEW) if position == 0 else Row(line=line))
        return rows

    def _caret_glyph(self) -> str:
        """The insertion point, drawn rather than delegated to the terminal.

        ``Terminal`` hides the real cursor for the whole frame and the diff painter owns where it
        is put, so a marker the front buffer carries is the only one that survives a repaint — and
        the only one a test can assert on. Same reasoning, same glyph, as :data:`~.input.CARET`.
        """
        return CARET if self.caps is None or self.caps.unicode else CARET_ASCII

    def _room(self) -> int:
        """Columns the field may use, taken from the body it is actually being drawn into.

        :attr:`~.app.Step.room` counts the gutter and the scrollbar out of the *window*, and takes
        no account of how wide that window's body really is. A wrapped value measured against the
        window is a value whose last row the frame then clips, which is the one failure the wrap
        exists to avoid.
        """
        if self.frame is not None and not self.frame.body.blank:
            return max(1, self.frame.body.width)
        return self.room

    def _width(self, value: str) -> int:
        return self.caps.width(value) if self.caps is not None else len(value)

    def _caret_at(
        self, text: str, caret: int, slices: tuple[tuple[int, int], ...]
    ) -> tuple[int, int]:
        """Which row the insertion point sits on, and how many columns along it.

        A caret exactly on a boundary belongs to the row it ends: it is *after* the character it
        follows, and putting it at the head of the next row would read as a caret before the wrong
        letter.
        """
        for position, (start, stop) in enumerate(slices):
            if start <= caret <= stop:
                return (position, self._width(text[start:caret]))
        return (len(slices) - 1, 0)

    def _index_of_column(self, head: str, column: int) -> int:
        """Where in ``head`` the caret's column falls, in characters.

        The count is in cells and the index is in characters, and they are only the same number for
        ASCII: a path with a CJK directory name in it has to be cut at the character whose
        *starting* column the caret asked for, or the caret is drawn a glyph away from where the
        keys think it is.
        """
        used = 0
        for index, character in enumerate(head):
            if used >= column:
                return index
            used += self._width(character)
        return len(head)

    # -- the confirmation --------------------------------------------------------------------

    def _asking(self, ask: Confirmation) -> list[Row]:
        """A boxed question, drawn where the list was.

        The list goes away rather than dimming behind it, which is what ``dialog`` and
        ``menuconfig`` both do at this width and all the frame can honestly offer: a box the body
        still shows through is a box the operator can still arrow around inside.
        """
        words = self._wrapped(ask.prompt, MAX_ASK_WIDTH - 8)
        buttons = self._button_text(ask)
        inner = max([self._width(line) for line in words] + [self._width(buttons)])
        edge = self._edge()
        out: list[Row] = [Row.gap()]
        out.append(Row.heading(self._rule(edge["tl"], edge["h"], edge["tr"], inner)))
        for line in words:
            out.append(Row.heading(self._boxed(edge["v"], line, inner)))
        out.append(Row.heading(self._boxed(edge["v"], "", inner)))
        out.append(Row.heading(self._boxed(edge["v"], buttons, inner, attr=self.tone("focus"))))
        out.append(Row.heading(self._rule(edge["bl"], edge["h"], edge["br"], inner)))
        return out

    def _edge(self) -> dict[str, str]:
        return L.box_glyphs(self.caps) if self.caps is not None else L.BOX_ASCII

    def _wrapped(self, text: str, room: int) -> list[str]:
        """``text`` in rows the box can hold, measured the way the frame measures everything else.

        :func:`~.layout.wrap` needs a cell table, and a step with no terminal attached has none —
        the only caller in that case is a test — so it falls back to the standard library's, which
        agrees with it on everything except what a wide name costs.
        """
        if self.caps is not None:
            return L.wrap(text, room, self.caps)
        return textwrap.wrap(text, room) or [""]

    def _rule(self, corner: str, dash: str, other: str, inner: int) -> Line:
        return Line(Segment(f" {corner}{dash * (inner + 2)}{other}"))

    def _boxed(self, vertical: str, text: str, inner: int, *, attr: str = "") -> Line:
        padding = " " * max(0, inner - self._width(text))
        return Line(
            Segment(f" {vertical} "),
            Segment(f"{text}{padding}", attr),
            Segment(f" {vertical}"),
        )

    def _button_text(self, ask: Confirmation) -> str:
        """The button line, with whichever button is focused in angle brackets.

        Both spellings are the same width whichever button is focused, so answering a question
        never moves it. Brackets are the cue that survives no colour *and* no bold, which is the
        only reason it is brackets and not something that needs a capability this terminal may not
        have.
        """
        cells = [
            f"< {button.label} >" if index == ask.focus else f"  {button.label}  "
            for index, button in enumerate(ask.buttons)
        ]
        return "   ".join(cells)

    def overlay(self, state: object) -> tuple[Line, ...] | None:
        """The completion panel: the directories one ``Tab`` found, kept on screen.

        Floating rather than a run of rows so that offering a list never moves the question or the
        value above it. It holds whatever the last ``Tab`` produced until a later one replaces it,
        and the loop keeps it inside the body — clear of the status line, and of the legend below
        it.
        """
        assert isinstance(state, PathState)
        panel = state.panel
        if panel is None:
            return None
        head = Line(Segment(f" {panel.parent}", self.tone("dim")))
        if not panel.candidates:
            # A lone match is inserted rather than listed, and then there is nothing to say: the
            # field already shows what Tab decided. Only a Tab that found nothing needs a panel,
            # because silence is indistinguishable from a keystroke that was never seen.
            if panel.completed:
                return None
            why = (
                f" nothing to list in {panel.parent}"
                if panel.unreadable
                else " no directory matches what you typed"
            )
            return (head, Line(Segment(why, self.tone("dim"))))
        return (
            head,
            *(Line(Segment(f"   {name}", self.tone("active"))) for name in panel.candidates),
        )

    # -- keys -------------------------------------------------------------------------------

    def keys(self, state: object, view: View) -> tuple[Binding, ...]:
        """The table for this keystroke, with every letter spelling struck out.

        A field cannot give ``k`` to two things, and a binding the operator cannot press because
        the value would eat it is worse than no binding at all — so the legend, the key reference
        and the dispatcher are all generated from the table that remains, exactly as the secret
        prompts do.
        """
        assert isinstance(state, PathState)
        out: list[Binding] = []
        for item in self._mode_keys(state):
            struck = strike(item)
            if struck:
                out.append(item if struck == item.keys else replace(item, keys=struck))
        return tuple(out)

    def _mode_keys(self, state: PathState) -> tuple[Binding, ...]:
        if state.confirmation is not None:
            # ``Enter`` is this step's own action while a question is up, not the loop's
            # ``ACCEPT``: the answer decides whether the screen closes, and the loop would have
            # committed the step before it got the chance.
            return (
                bind(BUTTON_CHOOSE, "choose", "Enter"),
                bind(BUTTON_CANCEL, "cancel", "Escape"),
                bind(BUTTON_BACK, "other button", "Left"),
                bind(BUTTON_FORWARD, "other button", "Right"),
                bind(RESET, "cancel", "Ctrl-R"),
                bind(ABORT, "abort", "Ctrl-C"),
            )
        scrolling = (
            bind(PAGE_UP, "page up", "PgUp", "Ctrl-U"),
            bind(PAGE_DOWN, "page down", "PgDn", "Ctrl-D"),
            bind(FIRST, "first", "Home"),
            bind(LAST, "last", "End"),
            bind(RESET, "reset choices", "Ctrl-R"),
            bind(ABORT, "abort", "Ctrl-C"),
        )
        moving = (
            bind(FOCUS_UP, "move up", "Up", "k"),
            bind(FOCUS_DOWN, "move down", "Down", "j"),
        )
        if state.live:
            # The field owns the arrows, so the two that would move the selection are not offered
            # at all: the legend drops them rather than advertising a key the field has eaten.
            # ``Escape`` is dropped the same way when there is no list to hand them back to, which
            # is the case on the first launch this checkout has ever had.
            leaving = (bind(BLUR, "leave the field", "Escape"),) if state.recent else ()
            return (
                bind(CONFIRM, "continue", "Enter"),
                bind(COMPLETE, "complete", "Tab"),
                DELETE_BINDING,
                bind(CARET_LEFT, "caret left", "Left"),
                bind(CARET_RIGHT, "caret right", "Right"),
                bind(CARET_HOME, "start of path", "Up"),
                bind(CARET_END, "end of path", "Down"),
                *leaving,
                *scrolling,
            )
        if state.on_field:
            # Blurred, on the field's own row. ``Tab`` goes back in — the same key that means
            # something else once inside, which is why it is printed with different words here.
            return (
                bind(CONFIRM, "continue", "Enter"),
                bind(FOCUS_FIELD, "edit this path", "Tab"),
                *moving,
                *scrolling,
            )
        return (bind(CONFIRM, "continue", "Enter"), *moving, *scrolling)

    def with_focus(self, state: object, index: int) -> PathState:
        """Move the cursor, and hand the arrows back to the field when it lands on it.

        Reaching the field row is the same fact as selecting it, which is why nothing extra is
        needed to re-enter a blurred field from the list: the operator arrows back up to the first
        row and the caret is there again.
        """
        assert isinstance(state, PathState)
        at = min(max(index, 0), state.last)
        return replace(state, at=at, editing=at == 0)

    # -- answering --------------------------------------------------------------------------

    def typed(self, state: object, char: str) -> tuple[PathState, Result | None]:
        """One character into the field, at the caret.

        A question that is up swallows everything: an answer typed into a confirmation is not an
        answer, and the bell this interface cannot ring is the only other thing it could say.
        """
        assert isinstance(state, PathState)
        if state.confirmation is not None or not state.live or not char.isprintable():
            return state, None
        text = f"{state.text[: state.caret]}{char}{state.text[state.caret :]}"
        return replace(state, text=text, caret=state.caret + 1, error=""), None

    def answer(self, state: object, action: str, target: object) -> tuple[object, Result | None]:
        assert isinstance(state, PathState)
        if state.confirmation is not None:
            return self._answered(state, action)
        if action == BLUR:
            return replace(state, editing=False, error=""), None
        if action == FOCUS_FIELD:
            return replace(state, editing=True), None
        if action == COMPLETE:
            found = completions(state.text, state.caret)
            return replace(state, panel=found, text=found.text, caret=found.caret, error=""), None
        if action == DELETE and state.live and state.caret:
            at = state.caret - 1
            text = f"{state.text[:at]}{state.text[state.caret :]}"
            return replace(state, text=text, caret=at, error=""), None
        if action == CARET_LEFT and state.live and state.caret:
            return replace(state, caret=state.caret - 1), None
        if action == CARET_RIGHT and state.live and state.caret < len(state.text):
            return replace(state, caret=state.caret + 1), None
        if action == CARET_HOME and state.live:
            return replace(state, caret=0), None
        if action == CARET_END and state.live:
            return replace(state, caret=len(state.text)), None
        if action == CONFIRM:
            return self._committed(state, target)
        return state, None

    def _committed(self, state: PathState, target: object) -> tuple[PathState, Result | None]:
        """``Enter``, which means two different things depending on where the cursor is.

        A remembered directory is *re-asked*: the disk may have moved since it was chosen, and the
        answer the launcher needs is the one about the directory as it is now. The typed value is
        *judged*, and a judgement that cannot be taken is a sentence rather than a failure — the
        screen stays up, because the operator is mid-edit and has earned the reason.
        """
        if state.on_field or target == NEW:
            return self._entered(state)
        return self._chosen(state, str(target or ""))

    def _entered(self, state: PathState) -> tuple[PathState, Result | None]:
        verdict = usable(state.text, reserved=self.reserved)
        if not verdict.usable:
            return replace(state, error=verdict.error), None
        return state, Result(value=verdict.path, summary=verdict.path)

    def _chosen(self, state: PathState, entry: str) -> tuple[PathState, Result | None]:
        if not entry:
            return state, None
        verdict = usable(entry, reserved=self.reserved)
        if verdict.usable and not verdict.creatable:
            return state, Result(value=verdict.path, summary=verdict.path)
        if verdict.creatable:
            asking = question(CREATE_ASK, (CANCEL, OK), entry)
            return replace(state, confirmation=asking), None
        if verdict.exists:
            # Still there, and still not something a session may be rooted in. That is not a stale
            # row, so it is not a question about the list: the row is honest and the disk is the
            # problem, and the only useful thing the screen can do is say so and wait.
            return replace(state, error=verdict.error), None
        asking = question(FORGET_ASK, (CANCEL, REMOVE), entry)
        return replace(state, confirmation=asking), None

    def _answered(self, state: PathState, action: str) -> tuple[PathState, Result | None]:
        ask = state.confirmation
        assert ask is not None
        if action in (BUTTON_CANCEL, RESET):
            # ``Ctrl-R`` on a question is the question's own cancel: there is nothing else here to
            # reset, and a key that quietly did nothing is the one thing the legend promised not to
            # be.
            return replace(state, confirmation=None), None
        if action == BUTTON_CHOOSE:
            return self._answered_with(state, ask.chosen)
        if action in (BUTTON_BACK, BUTTON_FORWARD) and len(ask.buttons) > 1:
            step = -1 if action == BUTTON_BACK else 1
            focus = (ask.focus + step) % len(ask.buttons)
            return replace(state, confirmation=replace(ask, focus=focus)), None
        return state, None

    def _answered_with(self, state: PathState, button: Button) -> tuple[PathState, Result | None]:
        ask = state.confirmation
        assert ask is not None
        closed = replace(state, confirmation=None, error="")
        if button.id == OK.id:
            # The launcher's own ``mkdir -p`` is what creates it, one step later, exactly as it
            # does for a typed path. A screen that made directories while you were still reading it
            # would leave one behind for every question answered Cancel.
            return closed, Result(value=ask.about, summary=ask.about)
        if button.id == REMOVE.id:
            if self._on_remove is not None:
                self._on_remove(ask.about)
            rest = tuple(entry for entry in state.recent if entry != ask.about)
            # The cursor follows the row that moved: one up the list, or onto the field if the
            # removed row was the only one. Leaving it where it was would park it past the end.
            at = min(state.at, len(rest))
            return replace(closed, recent=rest, at=at, editing=not rest), None
        return closed, None

    # -- the rest of the frame --------------------------------------------------------------

    def focus(self, state: object) -> int:
        assert isinstance(state, PathState)
        return min(max(state.at, 0), state.last)

    def status(self, state: object) -> Line:
        """The answer as the launcher would take it, or the reason it would not.

        Everything the value itself cannot say comes here: that the path resolves to somewhere
        else, that it does not exist yet, that it cannot be used. The alternative is an operator
        reading one path off the screen while the launcher mounts another.
        """
        assert isinstance(state, PathState)
        if state.confirmation is not None:
            return BLANK
        if state.error:
            return Line(Segment(state.error, self.tone("warn")))
        if not state.on_field:
            entry = state.recent[min(state.at, len(state.recent)) - 1]
            return self._about(usable(entry, reserved=self.reserved))
        if not state.text.strip():
            return Line(Segment("type a directory path", self.tone("dim")))
        if state.text == SEEDED:
            # The field opens on the root because a path has to begin somewhere, so refusing the
            # root back at the operator before they have typed a character is a complaint about the
            # seed rather than about their answer.
            return Line(Segment("type a path, or press Tab to list the root", self.tone("dim")))
        return self._about(usable(state.text, reserved=self.reserved))

    def _about(self, verdict: Verdict) -> Line:
        if not verdict.usable:
            # A path that is missing is the ordinary state of something being typed, so it is said
            # quietly; a path that is there and cannot be used is a fact about the machine, and
            # gets the colour that means look at this.
            tone = "warn" if verdict.exists else "dim"
            return Line(Segment(verdict.error, self.tone(tone)))
        facts = [fact for fact in (_state_of(verdict), _resolves(verdict)) if fact]
        joined = f"  {' · '.join(facts)}" if facts else ""
        return Line(Segment(verdict.path, self.tone("focus")), Segment(joined, self.tone("dim")))

    def commit(self, state: object) -> Result:
        """Unreachable, and kept as the contract rather than as dead code.

        ``Enter`` is answered here as :data:`CONFIRM` rather than as the loop's ``ACCEPT``
        precisely so a validation failure can keep the screen open *and* keep the status row
        honest: the loop's own path must either close the step or complain in the note row, and
        this step needs neither.
        """
        assert isinstance(state, PathState)
        verdict = usable(state.text)
        return Result(value=verdict.path, summary=verdict.path)


def _state_of(verdict: Verdict) -> str:
    """Two words about where this path currently is, for the status row."""
    if verdict.creatable:
        return "will be created"
    if verdict.exists:
        return "exists"
    return ""


def _resolves(verdict: Verdict) -> str:
    """The sentence for an alias, which the screen must not swallow."""
    return f"resolves to {verdict.path}" if verdict.resolved else ""
